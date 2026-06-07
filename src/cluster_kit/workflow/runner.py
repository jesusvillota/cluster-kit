"""YAML/TOML-defined SLURM workflow submission.

Workflows submit all jobs up front and rely on SLURM dependencies rather than
local polling. ``chain`` mode runs one job after the previous job succeeds.
``stages`` mode runs stages sequentially and can run jobs within each stage in
parallel.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from rich import box
from rich.console import Console
from rich.table import Table

from cluster_kit.launch.launcher import PARTITION_DEFAULTS, submit_command
from cluster_kit.sync.code import CodeDeployer

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

_console = Console()

_DEFAULT_PARTITION = "cpu_shared"
_DEFAULT_DEPENDENCY = "afterok"
_DEPENDENCY_MODES = {"afterok", "afterany"}
_WORKFLOW_MODES = {"chain", "stages"}
_LAUNCHER_FLAGS_WITH_VALUE = {
    "--run-from",
    "--partition",
    "--qos",
    "--slurm-cpus",
    "--slurm-mem",
    "--slurm-time",
    "--mode",
}
_UNSUPPORTED_SHELL_TOKENS = {"&&", "||", "|", ";", ">", ">>", "<"}


class WorkflowError(ValueError):
    """Raised when a workflow file or command is invalid."""


@dataclass(frozen=True)
class WorkflowJob:
    """One parsed workflow job."""

    name: str
    command: str
    submit_command: str
    partition: str
    cpus: int
    mem: str
    time: str
    qos: str | None
    texlive: bool


@dataclass(frozen=True)
class WorkflowStage:
    """One workflow stage."""

    name: str
    jobs: tuple[WorkflowJob, ...]
    parallel: bool = True


@dataclass(frozen=True)
class WorkflowPlan:
    """Parsed workflow plan."""

    name: str
    mode: str
    dependency: str
    project_root: Path
    sync: bool
    stages: tuple[WorkflowStage, ...]


def parse_workflow_file(path: Path | str) -> WorkflowPlan:
    """Parse and validate a workflow definition file."""
    workflow_path = Path(path).resolve()
    if not workflow_path.exists():
        raise WorkflowError(f"Workflow file not found: {workflow_path}")

    raw = _load_workflow_definition(workflow_path)
    if not isinstance(raw, dict):
        raise WorkflowError("workflow root must be a mapping")

    name = _as_string(raw.get("name"), workflow_path.stem)
    mode = _resolve_mode(raw)
    if mode not in _WORKFLOW_MODES:
        raise WorkflowError(f"mode must be one of {sorted(_WORKFLOW_MODES)}")

    dependency = _as_string(raw.get("dependency"), _DEFAULT_DEPENDENCY)
    if dependency not in _DEPENDENCY_MODES:
        raise WorkflowError(f"dependency must be one of {sorted(_DEPENDENCY_MODES)}")

    project_root = Path(_as_string(raw.get("project_root"), ".")).expanduser()
    if not project_root.is_absolute():
        project_root = (workflow_path.parent / project_root).resolve()
    else:
        project_root = project_root.resolve()

    sync = _as_bool(raw.get("sync"), True)
    defaults = raw.get("defaults", {})
    if defaults is not None and not isinstance(defaults, dict):
        raise WorkflowError("defaults must be a mapping")

    stages = _parse_stages(raw, mode, defaults or {})
    if not stages:
        raise WorkflowError("workflow must contain at least one job")

    return WorkflowPlan(
        name=name,
        mode=mode,
        dependency=dependency,
        project_root=project_root,
        sync=sync,
        stages=tuple(stages),
    )


def submit_workflow(
    path: Path | str,
    *,
    dry_run: bool = False,
    project_root: Path | str | None = None,
    sync: bool | None = None,
    dependency: str | None = None,
) -> list[str]:
    """Submit a workflow and return submitted job IDs.

    In ``dry_run`` mode no SSH calls are made; synthetic job IDs are returned.
    """
    plan = parse_workflow_file(path)
    if project_root is not None:
        plan = _replace_plan_project_root(
            plan,
            Path(project_root).expanduser().resolve(),
        )
    if sync is not None:
        plan = _replace_plan_sync(plan, sync)
    if dependency is not None:
        if dependency not in _DEPENDENCY_MODES:
            raise WorkflowError(
                f"dependency must be one of {sorted(_DEPENDENCY_MODES)}"
            )
        plan = _replace_plan_dependency(plan, dependency)

    _render_plan(plan, dry_run=dry_run)
    if plan.sync and not dry_run:
        deployer = CodeDeployer(dry_run=False, verbose=False)
        deployer._local_base = plan.project_root
        if not deployer.deploy():
            raise WorkflowError("code sync failed; workflow submission aborted")

    job_ids: list[str] = []
    previous_stage_ids: list[str] = []

    for stage_index, stage in enumerate(plan.stages, start=1):
        stage_ids: list[str] = []
        previous_job_id: str | None = None
        for job_index, job in enumerate(stage.jobs, start=1):
            dependency_expr = _dependency_expression(
                plan.dependency,
                previous_stage_ids,
                previous_job_id if not stage.parallel else None,
            )
            job_id = _submit_or_preview_job(
                plan,
                stage,
                job,
                stage_index,
                job_index,
                dependency_expr,
                dry_run,
            )
            job_ids.append(job_id)
            stage_ids.append(job_id)
            previous_job_id = job_id
        previous_stage_ids = stage_ids

    _console.print(
        f"[green][OK][/green] Submitted workflow [bold]{plan.name}[/bold] "
        f"with [bold]{len(job_ids)}[/bold] jobs"
    )
    return job_ids


def _parse_stages(
    raw: dict[str, Any], mode: str, defaults: dict[str, Any]
) -> list[WorkflowStage]:
    if mode == "chain":
        raw_jobs = raw.get("jobs", [])
        if not isinstance(raw_jobs, list):
            raise WorkflowError("jobs must be a list")
        jobs = tuple(
            _parse_job(job, index, defaults)
            for index, job in enumerate(raw_jobs, start=1)
        )
        return [WorkflowStage(name="chain", jobs=jobs, parallel=False)]

    raw_stages = raw.get("stages", [])
    if not isinstance(raw_stages, list):
        raise WorkflowError("stages must be a list")

    stages: list[WorkflowStage] = []
    for stage_index, raw_stage in enumerate(raw_stages, start=1):
        if not isinstance(raw_stage, dict):
            raise WorkflowError("each stage must be a mapping")
        stage_name = _as_string(raw_stage.get("name"), f"stage-{stage_index}")
        parallel = _as_bool(raw_stage.get("parallel"), True)
        stage_defaults = dict(defaults)
        raw_stage_defaults = raw_stage.get("defaults", {})
        if raw_stage_defaults is not None and not isinstance(raw_stage_defaults, dict):
            raise WorkflowError(f"defaults in stage {stage_name} must be a mapping")
        stage_defaults.update(raw_stage_defaults or {})
        raw_jobs = raw_stage.get("jobs", [])
        if not isinstance(raw_jobs, list):
            raise WorkflowError(f"jobs in stage {stage_name} must be a list")
        jobs = tuple(
            _parse_job(job, index, stage_defaults)
            for index, job in enumerate(raw_jobs, start=1)
        )
        if not jobs:
            raise WorkflowError(f"stage {stage_name} must contain at least one job")
        stages.append(WorkflowStage(stage_name, jobs, parallel=parallel))
    return stages


def _parse_job(raw_job: Any, index: int, defaults: dict[str, Any]) -> WorkflowJob:
    if not isinstance(raw_job, dict):
        raise WorkflowError("each job must be a mapping")
    command = _as_string(raw_job.get("command"), "").strip()
    if not command:
        raise WorkflowError(f"job {index} is missing command")

    command = _normalize_command(command)
    tokens = shlex.split(command)
    if len(tokens) < 3 or tokens[:2] != ["uv", "run"]:
        raise WorkflowError(f"job {index} command must start with 'uv run'")
    if any(token in _UNSUPPORTED_SHELL_TOKENS for token in tokens):
        raise WorkflowError(f"job {index} uses unsupported shell operators")

    metadata, stripped_tokens = _extract_launcher_metadata(tokens)
    script_token = _script_token(stripped_tokens)
    partition = _as_string(
        raw_job.get("partition"),
        metadata.get("partition")
        or defaults.get("partition")
        or _DEFAULT_PARTITION,
    )
    partition_defaults = PARTITION_DEFAULTS.get(
        partition, PARTITION_DEFAULTS[_DEFAULT_PARTITION]
    )
    cpus = _as_int(
        raw_job.get("cpus"),
        metadata.get("slurm_cpus"),
        defaults.get("cpus"),
        partition_defaults[0],
    )
    mem = _as_string(
        raw_job.get("mem"),
        metadata.get("slurm_mem") or defaults.get("mem") or partition_defaults[1],
    )
    time = _as_string(
        raw_job.get("time"),
        metadata.get("slurm_time") or defaults.get("time") or partition_defaults[2],
    )
    qos = raw_job.get("qos") or metadata.get("qos") or defaults.get("qos")
    name = _as_string(raw_job.get("name"), _derive_job_name(script_token, index))
    texlive = _as_bool(raw_job.get("texlive"), _needs_texlive(script_token))

    return WorkflowJob(
        name=_sanitize_job_name(name),
        command=command,
        submit_command=shlex.join(stripped_tokens),
        partition=partition,
        cpus=cpus,
        mem=mem,
        time=time,
        qos=str(qos) if qos else None,
        texlive=texlive,
    )


def _extract_launcher_metadata(tokens: list[str]) -> tuple[dict[str, str], list[str]]:
    metadata: dict[str, str] = {}
    stripped: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        flag = token.split("=", 1)[0] if "=" in token else token
        if flag in _LAUNCHER_FLAGS_WITH_VALUE:
            key = flag.lstrip("-").replace("-", "_")
            if "=" in token:
                metadata[key] = token.split("=", 1)[1]
                i += 1
            else:
                if i + 1 >= len(tokens):
                    raise WorkflowError(f"{token} requires a value")
                metadata[key] = tokens[i + 1]
                i += 2
            continue
        stripped.append(token)
        i += 1
    return metadata, stripped


def _load_workflow_definition(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        raw = yaml.safe_load(path.read_text())
        return {} if raw is None else raw

    if suffix == ".toml":
        with path.open("rb") as handle:
            return tomllib.load(handle)

    raise WorkflowError(
        "workflow file must have a .yaml, .yml, or .toml extension"
    )


def _resolve_mode(raw: dict[str, Any]) -> str:
    explicit_mode = raw.get("mode")
    if explicit_mode is not None:
        return _as_string(explicit_mode, "")

    if raw.get("stages") is not None:
        return "stages"

    if raw.get("jobs") is not None:
        return "chain"

    raise WorkflowError("workflow must define either jobs or stages")


def _script_token(tokens: list[str]) -> str:
    if len(tokens) >= 4 and tokens[2] == "python":
        return tokens[3]
    return tokens[2]


def _normalize_command(command: str) -> str:
    return re.sub(r"\\\s*\n\s*", " ", command).strip()


def _dependency_expression(
    mode: str,
    previous_stage_ids: list[str],
    previous_job_id: str | None,
) -> str | None:
    dependency_ids = previous_stage_ids[:]
    if previous_job_id:
        dependency_ids.append(previous_job_id)
    if not dependency_ids:
        return None
    return f"{mode}:{':'.join(dependency_ids)}"


def _submit_or_preview_job(
    plan: WorkflowPlan,
    stage: WorkflowStage,
    job: WorkflowJob,
    stage_index: int,
    job_index: int,
    dependency: str | None,
    dry_run: bool,
) -> str:
    if dry_run:
        preview_id = f"dry-{stage_index}-{job_index}"
        _console.print(
            f"  [cyan]DRY[/cyan] {preview_id}: {job.name}"
            + (f" depends on {dependency}" if dependency else "")
        )
        return preview_id

    job_id = submit_command(
        job.submit_command,
        project_root=plan.project_root,
        partition=job.partition,
        cpus=job.cpus,
        mem=job.mem,
        time=job.time,
        qos=job.qos,
        job_name=job.name,
        log_dir=f"_logs/workflows/{plan.name}/{stage.name}",
        texlive=job.texlive,
        sync=False,
        dependency=dependency,
    )
    if not job_id:
        raise WorkflowError(f"failed to submit job {job.name}")
    _console.print(
        f"  [green][OK][/green] {job.name}: [bold]{job_id}[/bold]"
        + (f" depends on {dependency}" if dependency else "")
    )
    return job_id


def _render_plan(plan: WorkflowPlan, *, dry_run: bool) -> None:
    table = Table(title=f"Workflow: {plan.name}", box=box.ROUNDED)
    table.add_column("Stage")
    table.add_column("Job")
    table.add_column("Partition")
    table.add_column("Resources")
    for stage in plan.stages:
        for job in stage.jobs:
            table.add_row(
                stage.name,
                job.name,
                job.partition,
                f"{job.cpus} CPU, {job.mem}, {job.time}",
            )
    _console.print(table)
    _console.print(
        f"[cyan]Mode:[/cyan] {plan.mode}  "
        f"[cyan]Dependency:[/cyan] {plan.dependency}  "
        f"[cyan]Sync:[/cyan] {'no' if dry_run else plan.sync}  "
        f"[cyan]Project:[/cyan] {plan.project_root}"
    )


def _derive_job_name(script_token: str, index: int) -> str:
    stem = Path(script_token).stem or f"workflow_job_{index}"
    return stem


def _sanitize_job_name(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
    return normalized[:128] or "workflow_job"


def _needs_texlive(script_token: str) -> bool:
    stem = Path(script_token).stem.lower()
    return any(
        keyword in stem
        for keyword in ("visualize", "render", "plot", "figure", "table")
    )


def _as_string(value: Any, default: Any) -> str:
    if value is None:
        value = default
    if value is None:
        return ""
    return str(value)


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise WorkflowError(f"expected boolean value, got {value!r}")


def _as_int(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise WorkflowError(f"expected integer value, got {value!r}") from exc
    raise WorkflowError("missing integer value")


def _replace_plan_project_root(plan: WorkflowPlan, project_root: Path) -> WorkflowPlan:
    return WorkflowPlan(
        plan.name, plan.mode, plan.dependency, project_root, plan.sync, plan.stages
    )


def _replace_plan_sync(plan: WorkflowPlan, sync: bool) -> WorkflowPlan:
    return WorkflowPlan(
        plan.name, plan.mode, plan.dependency, plan.project_root, sync, plan.stages
    )


def _replace_plan_dependency(plan: WorkflowPlan, dependency: str) -> WorkflowPlan:
    return WorkflowPlan(
        plan.name, plan.mode, dependency, plan.project_root, plan.sync, plan.stages
    )
