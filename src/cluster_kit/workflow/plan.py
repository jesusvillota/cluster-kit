"""Execution-plan construction for orchestrated workflow runs.

Translates a parsed :class:`WorkflowPlan` into a JSON-serializable execution
plan: every job carries its fully pre-rendered ``sbatch`` argv plus the indices
of the jobs it depends on. The plan is uploaded to the cluster together with
``orchestrator.py``, which executes it on the login node without needing
cluster-kit (or any third-party package) installed there.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any

from cluster_kit.config import (
    ConfigError,
    get_cluster_user,
    get_executor,
    get_remote_base,
)
from cluster_kit.launch.launcher import _resolve_worker_script, render_sbatch_argv
from cluster_kit.workflow.runner import WorkflowError, WorkflowJob, WorkflowPlan

SCHEMA_VERSION = 2
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_POLL_INTERVAL = 30
MAX_CONCURRENT_ENV_VAR = "CLUSTER_MAX_JOBS"
RUN_DIR_ROOT = ".cluster_kit/workflows"

# Placeholders used when cluster config is unavailable (dry-run without .env).
UNRESOLVED_REMOTE_BASE = "<remote_base>"
UNRESOLVED_USER = "<user>"


def resolve_max_concurrent(plan: WorkflowPlan, cli_value: int | None) -> int:
    """Resolve max concurrent jobs: CLI > YAML > $CLUSTER_MAX_JOBS > default."""
    for candidate in (cli_value, plan.max_concurrent):
        if candidate is not None:
            return _validate_positive(candidate, "max_concurrent")
    env_value = os.getenv(MAX_CONCURRENT_ENV_VAR)
    if env_value:
        return _validate_positive(env_value, MAX_CONCURRENT_ENV_VAR)
    return DEFAULT_MAX_CONCURRENT


def resolve_poll_interval(plan: WorkflowPlan, cli_value: int | None) -> int:
    """Resolve the orchestrator poll interval: CLI > YAML > default."""
    for candidate in (cli_value, plan.poll_interval):
        if candidate is not None:
            return _validate_positive(candidate, "poll_interval")
    return DEFAULT_POLL_INTERVAL


def compute_dependency_graph(
    plan: WorkflowPlan,
) -> list[tuple[WorkflowJob, str, list[int]]]:
    """Flatten stages into (job, stage_name, dependency_indices) triples.

    Replicates the stage semantics previously enforced via SLURM dependencies:
    jobs in a parallel stage each depend on every job of the previous stage;
    jobs in a sequential stage chain one after another, with the first job
    depending on the previous stage.
    """
    flattened: list[tuple[WorkflowJob, str, list[int]]] = []
    previous_stage_indices: list[int] = []
    index = 0
    for stage in plan.stages:
        stage_indices: list[int] = []
        previous_job_index: int | None = None
        for job in stage.jobs:
            if stage.parallel or previous_job_index is None:
                deps = list(previous_stage_indices)
            else:
                deps = [previous_job_index]
            flattened.append((job, stage.name, deps))
            stage_indices.append(index)
            previous_job_index = index
            index += 1
        previous_stage_indices = stage_indices
    return flattened


def build_execution_plan(
    plan: WorkflowPlan,
    *,
    max_concurrent: int | None = None,
    poll_interval: int | None = None,
    allow_unresolved_config: bool = False,
) -> dict[str, Any]:
    """Build the JSON-serializable execution plan for the orchestrator.

    With ``allow_unresolved_config=True`` (dry-run), missing cluster config is
    tolerated and placeholders are used for remote paths.
    """
    try:
        remote_base = str(get_remote_base())
        user = get_cluster_user()
        executor = get_executor()
    except ConfigError:
        if not allow_unresolved_config:
            raise
        remote_base = UNRESOLVED_REMOTE_BASE
        user = UNRESOLVED_USER
        executor = "slurm"

    worker_remote_path = None
    mail_user = os.getenv("CLUSTER_EMAIL", "")
    if executor == "slurm":
        resolved_worker = _resolve_worker_script(plan.project_root, plan.worker_script)
        if resolved_worker is None:
            raise WorkflowError(f"worker script not found: {plan.worker_script}")
        worker_remote_path = (
            f"{remote_base}/"
            f"{resolved_worker.relative_to(plan.project_root).as_posix()}"
        )

    workflow_slug = _slugify(plan.name)
    run_id = f"{workflow_slug}_{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"

    jobs: list[dict[str, Any]] = []
    for index, (job, stage_name, deps) in enumerate(compute_dependency_graph(plan)):
        log_dir = f"_logs_/workflows/{plan.name}/{stage_name}"
        entry: dict[str, Any] = {
            "index": index,
            "name": job.name,
            "stage": stage_name,
            "deps": deps,
            "log_dir": log_dir,
        }
        if executor == "slurm":
            entry["sbatch_argv"] = render_sbatch_argv(
                job.submit_command,
                remote_base=remote_base,
                partition=job.partition,
                cpus=job.cpus,
                mem=job.mem,
                time=job.time,
                qos=job.qos,
                job_name=job.name,
                log_dir=log_dir,
                texlive=job.texlive,
                env_vars=None,
                mail_user=mail_user,
                worker_remote_path=worker_remote_path,
            )
        else:
            # ssh executor: the orchestrator spawns the command directly on
            # the machine; keep `uv run` so the project venv is used.
            entry["command"] = job.submit_command
        jobs.append(entry)

    return {
        "schema_version": SCHEMA_VERSION,
        "workflow_name": plan.name,
        "run_id": run_id,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "user": user,
        "remote_base": remote_base,
        "executor": executor,
        "dependency_mode": plan.dependency,
        "max_concurrent": resolve_max_concurrent(plan, max_concurrent),
        "poll_interval": resolve_poll_interval(plan, poll_interval),
        "jobs": jobs,
    }


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
    return slug or "workflow"


def _validate_positive(value: Any, key: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowError(f"{key} must be a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise WorkflowError(f"{key} must be a positive integer, got {value!r}")
    return parsed
