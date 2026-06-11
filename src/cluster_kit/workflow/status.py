"""`workflow status` / `workflow cancel` for orchestrated runs."""

from __future__ import annotations

import datetime as dt

from rich import box
from rich.console import Console
from rich.table import Table

from cluster_kit.config import get_remote_base
from cluster_kit.workflow.remote import (
    RemoteError,
    kill_orchestrator,
    mark_cancelled,
    read_log,
    read_state,
    resolve_run_id,
    run_dir_for,
    scancel_jobs,
)

_console = Console()

_STATE_STYLES = {
    "WAITING": "dim",
    "SUBMITTED": "cyan",
    "COMPLETED": "green",
    "FAILED": "red",
    "SKIPPED": "yellow",
    "SUBMIT_FAILED": "red",
}
_NON_TERMINAL_JOB_STATES = {"WAITING", "SUBMITTED"}


def show_status(run_id: str | None = None, *, show_log: bool = False) -> None:
    """Render the state of an orchestrated workflow run."""
    remote_base = str(get_remote_base())
    run_id = resolve_run_id(remote_base, run_id)
    state = read_state(remote_base, run_id)

    table = Table(
        title=f"Workflow run: {state['run_id']} [{state['status']}]",
        box=box.ROUNDED,
    )
    table.add_column("Stage")
    table.add_column("Job")
    table.add_column("State")
    table.add_column("SLURM ID")
    table.add_column("SLURM state")
    table.add_column("Exit")
    counts: dict[str, int] = {}
    for job in state["jobs"]:
        job_state = job["state"]
        counts[job_state] = counts.get(job_state, 0) + 1
        style = _STATE_STYLES.get(job_state, "")
        table.add_row(
            job["stage"],
            job["name"],
            f"[{style}]{job_state}[/{style}]" if style else job_state,
            job.get("slurm_job_id") or "-",
            job.get("slurm_state") or "-",
            job.get("exit_code") or "-",
        )
    _console.print(table)
    summary = "  ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    _console.print(
        f"[cyan]Jobs:[/cyan] {summary}\n"
        f"[cyan]Started:[/cyan] {state.get('started_at', '-')}  "
        f"[cyan]Heartbeat:[/cyan] {state.get('heartbeat', '-')}  "
        f"[cyan]Run dir:[/cyan] {run_dir_for(remote_base, run_id)}"
    )

    if state["status"] == "running" and _heartbeat_stale(state):
        _console.print(
            "[yellow]Warning:[/yellow] heartbeat is stale — the orchestrator may "
            "have crashed. Resume it with:\n"
            f"  ssh cluster 'cd {remote_base} && nohup python3 "
            f"{run_dir_for(remote_base, run_id)}/orchestrator.py "
            f"{run_dir_for(remote_base, run_id)}/plan.json >> "
            f"{run_dir_for(remote_base, run_id)}/orchestrator.log 2>&1 &'"
        )

    if show_log:
        _console.rule("orchestrator.log (tail)")
        _console.print(read_log(remote_base, run_id), highlight=False, markup=False)


def cancel_run(run_id: str | None = None) -> None:
    """Kill the orchestrator and scancel its non-terminal jobs."""
    remote_base = str(get_remote_base())
    run_id = resolve_run_id(remote_base, run_id)

    killed = kill_orchestrator(remote_base, run_id)
    _console.print(
        "[green][OK][/green] Orchestrator process killed"
        if killed
        else "[yellow]Orchestrator process not running (already finished?)[/yellow]"
    )

    try:
        state = read_state(remote_base, run_id)
    except RemoteError:
        _console.print("[yellow]No state.json found; nothing to scancel[/yellow]")
        mark_cancelled(remote_base, run_id)
        return

    active_ids = [
        job["slurm_job_id"]
        for job in state["jobs"]
        if job["state"] == "SUBMITTED" and job.get("slurm_job_id")
    ]
    if active_ids:
        scancel_jobs(active_ids)
        _console.print(
            f"[green][OK][/green] Cancelled {len(active_ids)} SLURM job(s): "
            f"{', '.join(active_ids)}"
        )
    else:
        _console.print("No active SLURM jobs to cancel")

    pending = sum(
        1 for job in state["jobs"] if job["state"] in _NON_TERMINAL_JOB_STATES
    )
    mark_cancelled(remote_base, run_id)
    _console.print(
        f"[green][OK][/green] Run [bold]{run_id}[/bold] cancelled "
        f"({pending} job(s) never ran)"
    )


def _heartbeat_stale(state: dict) -> bool:
    heartbeat = state.get("heartbeat")
    if not heartbeat:
        return True
    try:
        beat = dt.datetime.fromisoformat(heartbeat)
    except ValueError:
        return True
    now = dt.datetime.now(beat.tzinfo) if beat.tzinfo else dt.datetime.now()
    return (now - beat).total_seconds() > 3 * state.get("poll_interval", 30)
