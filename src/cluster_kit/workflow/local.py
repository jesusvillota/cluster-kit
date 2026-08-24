"""Foreground local workflow execution."""

from __future__ import annotations

import dataclasses
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table

from cluster_kit.workflow.runner import (
    WorkflowError,
    _as_string,
    _load_workflow_definition,
    _normalize_command,
    _resolve_mode,
    _sanitize_job_name,
)

_console = Console()


@dataclass(frozen=True)
class LocalWorkflowJob:
    """One foreground local workflow job."""

    name: str
    command: str


@dataclass(frozen=True)
class LocalWorkflowStage:
    """One foreground local workflow stage."""

    name: str
    job: LocalWorkflowJob


@dataclass(frozen=True)
class LocalWorkflowPlan:
    """Parsed foreground local workflow plan."""

    name: str
    project_root: Path
    stages: tuple[LocalWorkflowStage, ...]


def parse_local_workflow_file(
    path: Path | str,
    *,
    project_root: Path | str | None = None,
) -> LocalWorkflowPlan:
    """Parse a workflow file for foreground local execution."""
    workflow_path = Path(path).expanduser().resolve()
    if not workflow_path.exists():
        raise WorkflowError(f"Workflow file not found: {workflow_path}")

    raw = _load_workflow_definition(workflow_path)
    if not isinstance(raw, dict):
        raise WorkflowError("workflow root must be a mapping")

    root = _resolve_project_root(raw, workflow_path, project_root)
    name = _as_string(raw.get("name"), workflow_path.stem)
    mode = _resolve_mode(raw)
    stages = _parse_local_stages(raw, mode)
    if not stages:
        raise WorkflowError("workflow must contain at least one job")

    return LocalWorkflowPlan(name=name, project_root=root, stages=tuple(stages))


def run_local_workflow(
    path: Path | str,
    *,
    dry_run: bool = False,
    project_root: Path | str | None = None,
) -> int:
    """Run a workflow foreground on this machine."""
    plan = parse_local_workflow_file(path, project_root=project_root)
    _render_local_plan(plan, dry_run=dry_run)
    if dry_run:
        return 0

    overall_start = time.time()
    for index, stage in enumerate(plan.stages, start=1):
        exit_code = _run_stage(plan, stage, index, len(plan.stages))
        if exit_code != 0:
            elapsed = time.time() - overall_start
            _console.print(
                f"\n[bold red]Workflow failed at stage:[/bold red] {stage.name}"
            )
            _console.print(f"[bold]Total elapsed:[/bold] {elapsed:.1f}s")
            return exit_code

    elapsed = time.time() - overall_start
    _console.print("\n[bold green]Workflow completed successfully[/bold green]")
    _console.print(f"[bold]Stages run:[/bold] {len(plan.stages)}")
    _console.print(f"[bold]Total elapsed:[/bold] {elapsed:.1f}s")
    return 0


def _resolve_project_root(
    raw: dict[str, Any],
    workflow_path: Path,
    cli_project_root: Path | str | None,
) -> Path:
    if cli_project_root is not None:
        return Path(cli_project_root).expanduser().resolve()

    configured = raw.get("project_root")
    if configured is None:
        return Path.cwd().resolve()

    root = Path(_as_string(configured, ".")).expanduser()
    if not root.is_absolute():
        root = workflow_path.parent / root
    return root.resolve()


def _parse_local_stages(
    raw: dict[str, Any],
    mode: str,
) -> list[LocalWorkflowStage]:
    if mode == "chain":
        raw_jobs = raw.get("jobs", [])
        if not isinstance(raw_jobs, list):
            raise WorkflowError("jobs must be a list")
        return [
            LocalWorkflowStage(
                name=f"job-{index}",
                job=_parse_local_job(job, index),
            )
            for index, job in enumerate(raw_jobs, start=1)
        ]

    if mode != "stages":
        raise WorkflowError("mode must be one of ['chain', 'stages']")

    raw_stages = raw.get("stages", [])
    if not isinstance(raw_stages, list):
        raise WorkflowError("stages must be a list")

    stages: list[LocalWorkflowStage] = []
    for index, raw_stage in enumerate(raw_stages, start=1):
        if not isinstance(raw_stage, dict):
            raise WorkflowError("each stage must be a mapping")
        stage_name = _as_string(raw_stage.get("name"), f"stage-{index}")
        raw_jobs = raw_stage.get("jobs", [])
        if not isinstance(raw_jobs, list):
            raise WorkflowError(f"jobs in stage {stage_name} must be a list")
        if len(raw_jobs) != 1:
            raise WorkflowError(
                f"stage {stage_name} has {len(raw_jobs)} jobs; "
                "local workflows require exactly 1 job per stage"
            )
        stages.append(
            LocalWorkflowStage(
                name=stage_name,
                job=_parse_local_job(raw_jobs[0], index),
            )
        )
    return stages


def _parse_local_job(raw_job: Any, index: int) -> LocalWorkflowJob:
    if not isinstance(raw_job, dict):
        raise WorkflowError("each job must be a mapping")
    command = _normalize_command(_as_string(raw_job.get("command"), "").strip())
    if not command:
        raise WorkflowError(f"job {index} is missing command")
    name = _sanitize_job_name(_as_string(raw_job.get("name"), f"job-{index}"))
    return LocalWorkflowJob(name=name, command=command)


def _render_local_plan(plan: LocalWorkflowPlan, *, dry_run: bool) -> None:
    table = Table(title=f"Local workflow: {plan.name}", box=box.ROUNDED)
    table.add_column("Stage")
    table.add_column("Job")
    table.add_column("Command")
    for stage in plan.stages:
        table.add_row(stage.name, stage.job.name, stage.job.command)
    _console.print(table)
    _console.print(
        f"[cyan]Executor:[/cyan] local  "
        f"[cyan]Mode:[/cyan] foreground  "
        f"[cyan]Dry run:[/cyan] {dry_run}  "
        f"[cyan]Project:[/cyan] {plan.project_root}"
    )


def _run_stage(
    plan: LocalWorkflowPlan,
    stage: LocalWorkflowStage,
    stage_num: int,
    total_stages: int,
) -> int:
    _console.print(
        f"\n[bold]Stage {stage_num}/{total_stages}:[/bold] [cyan]{stage.name}[/cyan]"
    )
    _console.print(f"[dim]Running:[/dim] {stage.job.command}")
    start = time.time()
    result = subprocess.run(stage.job.command, shell=True, cwd=plan.project_root)
    elapsed = time.time() - start
    if result.returncode != 0:
        _console.print(
            f"[bold red]FAILED[/bold red] "
            f"(exit code {result.returncode}, {elapsed:.1f}s)"
        )
        return result.returncode
    _console.print(f"[bold green]SUCCESS[/bold green] ({elapsed:.1f}s)")
    return 0


def with_project_root(
    plan: LocalWorkflowPlan,
    project_root: Path | str,
) -> LocalWorkflowPlan:
    """Return a copy of *plan* rooted at *project_root*."""
    return dataclasses.replace(plan, project_root=Path(project_root).resolve())
