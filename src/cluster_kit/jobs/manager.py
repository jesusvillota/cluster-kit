"""Detached-job management for ssh-executor profiles.

Provides an sbatch-like UX against a plain remote machine: a submitted
command keeps running after the SSH session closes, and its state is
reconstructed from a job directory on the remote:

    <remote_base>/.cluster_kit/jobs/<job_id>/
        meta.json   # name, command, created_at, git sha at submission
        job.sh      # wrapper: writes pid, runs the command, writes rc
        pid         # wrapper's own $$ (process-group leader via setsid)
        log         # combined stdout+stderr
        rc          # exit code, written on completion
        CANCELLED   # marker written by cancel()

Status is evaluated remotely by ``jobctl.py`` (uploaded once per version),
which prints JSON the client parses directly.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from cluster_kit.config import ClusterConfig, load_config
from cluster_kit.jobs.jobctl import JOBCTL_VERSION
from cluster_kit.utils.ssh import ensure_reachable, run_remote, stream_remote

JOBS_DIR_ROOT = ".cluster_kit/jobs"
_JOBCTL_REMOTE_REL = ".cluster_kit/jobctl.py"
_JOBCTL_SOURCE = Path(__file__).parent / "jobctl.py"


class JobError(RuntimeError):
    """Raised when a detached-job operation fails."""


@dataclass(frozen=True)
class JobHandle:
    """Reference to a submitted detached job."""

    job_id: str
    job_dir: str
    log_path: str
    command: str


def _cfg(config: Optional[ClusterConfig]) -> ClusterConfig:
    return config if config is not None else load_config()


def _jobs_root(config: ClusterConfig) -> str:
    return f"{config.remote_base}/{JOBS_DIR_ROOT}"


def _job_dir(config: ClusterConfig, job_id: str) -> str:
    return f"{_jobs_root(config)}/{job_id}"


def generate_job_id(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip()) or "job"
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{slug[:64]}_{timestamp}_{secrets.token_hex(2)}"


def render_job_script(job_dir: str, remote_base: str, command: str) -> str:
    """Render the wrapper that makes the job survive disconnects.

    The wrapper writes its own ``$$`` (setsid forks, so the launcher cannot
    trust ``$!``) and exports ``~/.local/bin`` because uv is not on the
    non-interactive SSH PATH.
    """
    return (
        "#!/bin/bash\n"
        f"echo $$ > {shlex.quote(job_dir)}/pid\n"
        'export PATH="$HOME/.local/bin:$PATH"\n'
        f"cd {shlex.quote(remote_base)}\n"
        f"{command}\n"
        f"echo $? > {shlex.quote(job_dir)}/rc\n"
    )


def _scp(local_paths: list[Path], remote_dir: str, config: ClusterConfig) -> None:
    result = subprocess.run(
        [
            "scp",
            "-q",
            *[str(path) for path in local_paths],
            f"{config.host}:{remote_dir}/",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise JobError(f"upload failed: {result.stderr.strip()}")


# Hosts whose jobctl.py was already verified this process; saves an SSH
# round-trip per call, which matters for TUI polling.
_jobctl_verified: set[tuple[str, str]] = set()


def _ensure_jobctl(config: ClusterConfig) -> None:
    """Upload jobctl.py unless the remote copy already matches our version."""
    key = (config.host, str(config.remote_base))
    if key in _jobctl_verified:
        return
    remote_path = f"{config.remote_base}/{_JOBCTL_REMOTE_REL}"
    result = run_remote(f"python3 {shlex.quote(remote_path)} --version", config=config)
    if result.returncode == 0 and result.stdout.strip() == str(JOBCTL_VERSION):
        _jobctl_verified.add(key)
        return
    parent = str(Path(remote_path).parent)
    mkdir = run_remote(f"mkdir -p {shlex.quote(parent)}", config=config)
    if mkdir.returncode != 0:
        raise JobError(f"could not create {parent}: {mkdir.stderr.strip()}")
    _scp([_JOBCTL_SOURCE], parent, config)
    _jobctl_verified.add(key)


def _require_ssh_executor(config: ClusterConfig) -> None:
    if config.executor != "ssh":
        raise JobError(
            "detached jobs require an ssh-executor profile "
            f"(this profile uses executor='{config.executor}'); "
            "select one with CLUSTER_ENV or cluster-kit --profile"
        )


def submit(
    command: str,
    *,
    name: Optional[str] = None,
    config: Optional[ClusterConfig] = None,
) -> JobHandle:
    """Submit *command* as a detached job on the remote machine.

    The command runs through remote bash from ``remote_base``, so pipes and
    shell syntax are allowed (unlike workflow YAML commands).
    """
    config = _cfg(config)
    _require_ssh_executor(config)
    command = command.strip()
    if not command:
        raise JobError("command is empty")

    ensure_reachable(config=config)

    job_name = name or Path(shlex.split(command)[-1]).stem or "job"
    if name is None:
        # Derive a friendlier default from the script token, if any.
        for token in shlex.split(command):
            if token.endswith(".py"):
                job_name = Path(token).stem
                break
    job_id = generate_job_id(job_name)
    job_dir = _job_dir(config, job_id)
    log_path = f"{job_dir}/log"

    mkdir = run_remote(f"mkdir -p {shlex.quote(job_dir)}", config=config)
    if mkdir.returncode != 0:
        raise JobError(f"could not create job dir: {mkdir.stderr.strip()}")

    git_sha_result = run_remote(
        f"cd {shlex.quote(str(config.remote_base))} && git rev-parse HEAD",
        config=config,
    )
    git_sha = git_sha_result.stdout.strip() if git_sha_result.returncode == 0 else None

    meta = {
        "job_id": job_id,
        "name": job_name,
        "command": command,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "remote_base": str(config.remote_base),
        "git_sha": git_sha,
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        script_path = Path(tmp_dir) / "job.sh"
        meta_path = Path(tmp_dir) / "meta.json"
        script_path.write_text(
            render_job_script(job_dir, str(config.remote_base), command)
        )
        meta_path.write_text(json.dumps(meta, indent=2))
        _scp([script_path, meta_path], job_dir, config)

    quoted_dir = shlex.quote(job_dir)
    launch = (
        f"setsid nohup bash {quoted_dir}/job.sh >> {quoted_dir}/log 2>&1 < /dev/null &"
    )
    # bash -c keeps the launch line shell-agnostic on the remote side.
    result = run_remote(f"bash -c {shlex.quote(launch)}", config=config)
    if result.returncode != 0:
        raise JobError(f"could not launch job: {result.stderr.strip()}")

    _ensure_jobctl(config)
    return JobHandle(job_id=job_id, job_dir=job_dir, log_path=log_path, command=command)


def _jobctl(args: list[str], config: ClusterConfig) -> Any:
    _ensure_jobctl(config)
    remote_path = f"{config.remote_base}/{_JOBCTL_REMOTE_REL}"
    argv = " ".join(shlex.quote(arg) for arg in args)
    result = run_remote(f"python3 {shlex.quote(remote_path)} {argv}", config=config)
    if result.returncode != 0:
        raise JobError(f"jobctl failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise JobError(f"corrupt jobctl output: {result.stdout!r}") from exc


def list_jobs(*, config: Optional[ClusterConfig] = None) -> list[dict]:
    """Return the state of every job directory on the remote."""
    config = _cfg(config)
    return _jobctl(["list", _jobs_root(config)], config)


def job_status(job_id: str, *, config: Optional[ClusterConfig] = None) -> dict:
    """Return the state of one job."""
    config = _cfg(config)
    return _jobctl(["status", _jobs_root(config), job_id], config)


def read_log(
    job_id: str,
    *,
    lines: int = 50,
    follow: bool = False,
    config: Optional[ClusterConfig] = None,
) -> int | str:
    """Tail a job log. With ``follow=True``, streams to the terminal and
    returns the exit code; otherwise returns the tail as a string."""
    config = _cfg(config)
    log_path = f"{_job_dir(config, job_id)}/log"
    if follow:
        return stream_remote(
            f"tail -n {int(lines)} -f {shlex.quote(log_path)}", config=config
        )
    result = run_remote(f"tail -n {int(lines)} {shlex.quote(log_path)}", config=config)
    if result.returncode != 0:
        raise JobError(f"could not read log: {result.stderr.strip()}")
    return result.stdout


def cancel(
    job_id: str,
    *,
    force: bool = False,
    config: Optional[ClusterConfig] = None,
) -> bool:
    """TERM (and optionally KILL) the job's whole process group.

    The wrapper is a session/process-group leader, so the negative-pid kill
    takes down `uv run`/python children too. Returns True when a signal was
    delivered.
    """
    config = _cfg(config)
    quoted_dir = shlex.quote(_job_dir(config, job_id))
    kill_part = 'kill -TERM -- "-$pid" 2>/dev/null; killed=$?'
    if force:
        kill_part += '; sleep 1; kill -KILL -- "-$pid" 2>/dev/null'
    script = (
        f"pid=$(cat {quoted_dir}/pid 2>/dev/null) && "
        f"{kill_part}; touch {quoted_dir}/CANCELLED; exit ${{killed:-1}}"
    )
    result = run_remote(f"bash -c {shlex.quote(script)}", config=config)
    return result.returncode == 0
