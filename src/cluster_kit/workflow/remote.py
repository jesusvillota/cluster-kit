"""SSH helpers for orchestrated workflow runs.

Uploads the execution plan + orchestrator to the cluster, launches the
detached orchestrator on the login node, and reads back run state for the
``workflow status`` / ``workflow cancel`` commands.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from cluster_kit.config import get_cluster_host
from cluster_kit.workflow.plan import RUN_DIR_ROOT

_SSH_TIMEOUT = 30
_ORCHESTRATOR_SOURCE = Path(__file__).parent / "orchestrator.py"


class RemoteError(RuntimeError):
    """Raised when a remote workflow operation fails."""


def _ssh(command: str, *, timeout: int = _SSH_TIMEOUT) -> subprocess.CompletedProcess:
    host = get_cluster_host()
    try:
        return subprocess.run(
            ["ssh", host, command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteError(f"SSH command timed out: {command}") from exc


def _scp(local_paths: list[Path], remote_dir: str) -> None:
    host = get_cluster_host()
    result = subprocess.run(
        ["scp", "-q", *[str(path) for path in local_paths], f"{host}:{remote_dir}/"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RemoteError(f"upload failed: {result.stderr.strip()}")


def run_dir_for(remote_base: str, run_id: str) -> str:
    return f"{remote_base}/{RUN_DIR_ROOT}/{run_id}"


def check_remote_python() -> None:
    """Abort early if the login node lacks a usable python3."""
    result = _ssh(
        "python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'"
    )
    if result.returncode != 0:
        raise RemoteError(
            "login node python3 missing or older than 3.8; "
            "cannot run the workflow orchestrator"
        )


def launch_orchestrator(exec_plan: dict[str, Any]) -> str:
    """Upload plan + orchestrator and start the detached daemon.

    Returns the remote run directory.
    """
    remote_base = exec_plan["remote_base"]
    run_id = exec_plan["run_id"]
    run_dir = run_dir_for(remote_base, run_id)

    check_remote_python()

    result = _ssh(f"mkdir -p {shlex.quote(run_dir)}")
    if result.returncode != 0:
        raise RemoteError(f"could not create run dir: {result.stderr.strip()}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        plan_path = Path(tmp_dir) / "plan.json"
        plan_path.write_text(json.dumps(exec_plan, indent=2))
        _scp([plan_path, _ORCHESTRATOR_SOURCE], run_dir)

    # The brace group keeps `&` scoped to the nohup command: without it,
    # `cd && nohup ... &` backgrounds the whole list and the wrapper subshell
    # holds the SSH session open (and $! captures the wrapper, not the daemon).
    quoted_run_dir = shlex.quote(run_dir)
    launch_cmd = (
        f"cd {shlex.quote(remote_base)} && "
        f"{{ nohup python3 {quoted_run_dir}/orchestrator.py "
        f"{quoted_run_dir}/plan.json "
        f">> {quoted_run_dir}/orchestrator.log 2>&1 < /dev/null & "
        f"echo $! > {quoted_run_dir}/orchestrator.pid; }}"
    )
    result = _ssh(launch_cmd)
    if result.returncode != 0:
        raise RemoteError(f"could not launch orchestrator: {result.stderr.strip()}")

    latest_path = f"{remote_base}/{RUN_DIR_ROOT}/LATEST"
    _ssh(f"printf '%s' {shlex.quote(run_id)} > {shlex.quote(latest_path)}")
    return run_dir


def resolve_run_id(remote_base: str, run_id: str | None) -> str:
    """Resolve an explicit run id, or read the LATEST pointer."""
    if run_id:
        return run_id
    latest_path = f"{remote_base}/{RUN_DIR_ROOT}/LATEST"
    result = _ssh(f"cat {shlex.quote(latest_path)}")
    if result.returncode != 0 or not result.stdout.strip():
        raise RemoteError(
            "no workflow runs found (missing LATEST pointer); "
            "pass an explicit run id"
        )
    return result.stdout.strip()


def read_state(remote_base: str, run_id: str) -> dict[str, Any]:
    state_path = f"{run_dir_for(remote_base, run_id)}/state.json"
    result = _ssh(f"cat {shlex.quote(state_path)}")
    if result.returncode != 0:
        raise RemoteError(
            f"could not read state for run {run_id}: {result.stderr.strip()}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteError(f"corrupt state.json for run {run_id}") from exc


def read_log(remote_base: str, run_id: str, lines: int = 50) -> str:
    log_path = f"{run_dir_for(remote_base, run_id)}/orchestrator.log"
    result = _ssh(f"tail -n {int(lines)} {shlex.quote(log_path)}")
    if result.returncode != 0:
        raise RemoteError(
            f"could not read log for run {run_id}: {result.stderr.strip()}"
        )
    return result.stdout


def kill_orchestrator(remote_base: str, run_id: str) -> bool:
    """Kill the orchestrator process; returns True if a kill was delivered."""
    run_dir = shlex.quote(run_dir_for(remote_base, run_id))
    result = _ssh(f"kill $(cat {run_dir}/orchestrator.pid) 2>/dev/null")
    return result.returncode == 0


def scancel_jobs(job_ids: list[str]) -> None:
    if not job_ids:
        return
    result = _ssh(f"scancel {' '.join(shlex.quote(job_id) for job_id in job_ids)}")
    if result.returncode != 0:
        raise RemoteError(f"scancel failed: {result.stderr.strip()}")


def kill_job_groups(pids: list[str]) -> None:
    """TERM the given process groups on the remote (ssh executor cancel)."""
    if not pids:
        return
    kills = "; ".join(
        f'kill -TERM -- "-{int(pid)}" 2>/dev/null' for pid in pids
    )
    _ssh(f"bash -c {shlex.quote(kills + '; true')}")


def mark_cancelled(remote_base: str, run_id: str) -> None:
    run_dir = shlex.quote(run_dir_for(remote_base, run_id))
    _ssh(f"touch {run_dir}/CANCELLED")
