"""Data mirror between the cluster and an ssh-executor host (the PC).

Keeps dataset directories identical on both machines with a two-pass union
rsync (``--update``, never ``--delete``): newest mtime wins, nothing is
deleted, both sides converge to the union. Safe for append-mostly data.

The rsync runs ON the PC against the cluster, so data flows directly over
the CEMFI LAN and never routes through this machine; we only orchestrate
via ``ssh pc '...'``.

For real (non-dry-run) transfers the two-pass rsync is submitted as a
*detached job* on the PC (the same setsid+nohup mechanism as
``cluster-kit job submit``) rather than run synchronously over one SSH
call: GB-scale first syncs can take hours, and a single held-open SSH
connection has been observed to drop with OpenSSH's own "Timeout, server
not responding" during a long, mostly one-directional transfer. Detaching
means a dropped connection only interrupts our polling loop — the rsync
keeps running on the PC regardless, and the next poll picks it back up.

Datasets come from a manifest in the consuming repo:

    # mirror.yaml
    cluster_from_pc: j-vill36@192.168.1.61   # how the PC addresses the cluster
    datasets:
      whale_outputs:
        cluster: /mnt/slurm-beegfs/Users/j-vill36/scripts_whales/output
        pc: /home/j-vill36/GitHub/whales/output
        exclude: []                          # optional rsync --exclude patterns

CLI:
    $ cluster-kit sync mirror [--manifest mirror.yaml] [--dataset NAME]
                              [--dry-run] [--verbose]
"""

from __future__ import annotations

import datetime as dt
import json
import shlex
import time
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console

from cluster_kit.config import load_config
from cluster_kit.jobs.manager import JobError, job_status, list_jobs, read_log, submit
from cluster_kit.utils.ssh import RemoteUnreachableError, ensure_reachable, run_remote

console = Console()

MIRROR_STATE_PATH = Path.home() / ".cache" / "cluster-kit" / "mirror_state.json"

# Dry runs only stat/compare files (proportional to file count, not data
# volume) so a single blocking SSH call is fine; give it generous headroom.
_DRY_RUN_TIMEOUT = 3600

# How often to poll the detached mirror job, and how long to keep polling
# before giving up supervision (the remote job keeps running either way).
_POLL_INTERVAL = 10
_MAX_WAIT = 6 * 3600


class MirrorError(RuntimeError):
    """Raised when the mirror manifest is invalid."""


def load_manifest(path: Path) -> dict:
    """Parse and validate a mirror.yaml manifest."""
    if not path.exists():
        raise MirrorError(f"manifest not found: {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise MirrorError(f"manifest is not a mapping: {path}")
    if not isinstance(data.get("cluster_from_pc"), str):
        raise MirrorError("manifest missing 'cluster_from_pc' (user@host string)")
    datasets = data.get("datasets")
    if not isinstance(datasets, dict) or not datasets:
        raise MirrorError("manifest missing non-empty 'datasets' mapping")
    for name, spec in datasets.items():
        if not isinstance(spec, dict) or "cluster" not in spec or "pc" not in spec:
            raise MirrorError(f"dataset '{name}' needs 'cluster' and 'pc' paths")
    return data


def read_mirror_state() -> dict:
    """Return the per-dataset mirror state; {} when missing or corrupt."""
    try:
        return json.loads(MIRROR_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _prune_state(dataset_names: set[str]) -> None:
    """Drop state for datasets removed from the active manifest."""
    state = read_mirror_state()
    stale = set(state) - dataset_names
    if not stale:
        return
    for name in stale:
        del state[name]
    MIRROR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MIRROR_STATE_PATH.write_text(json.dumps(state, indent=2))


def _write_state(name: str, ok: bool, detail: str) -> None:
    state = read_mirror_state()
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    entry = state.get(name, {})
    entry.update({"last_run": now, "ok": ok, "detail": detail})
    if ok:
        entry["last_success"] = now
    state[name] = entry
    MIRROR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MIRROR_STATE_PATH.write_text(json.dumps(state, indent=2))


def _rsync_cmd(src: str, dst: str, excludes: list[str], dry_run: bool) -> str:
    parts = ["rsync", "-az", "--update", "-e", "'ssh -o BatchMode=yes'"]
    if dry_run:
        parts += ["--dry-run", "-v"]
    for pattern in excludes:
        parts.append(f"--exclude {shlex.quote(pattern)}")
    parts += [src, dst]
    return " ".join(parts)


def _find_running_job(name: str, pc_config) -> Optional[str]:
    """Return the job_id of an already-RUNNING mirror job for *name*, if any.

    Without this, an hourly-scheduled mirror would submit a brand-new
    redundant transfer every tick while a prior multi-hour first-sync is
    still in flight, stacking up concurrent rsyncs against the same dirs.
    """
    try:
        jobs = list_jobs(config=pc_config)
    except JobError:
        return None
    target = f"mirror-{name}"
    for job in jobs:
        if job.get("name") == target and job.get("state") == "RUNNING":
            return job.get("job_id")
    return None


def _dry_run_dataset(
    name: str, pc_dir: str, cluster_dir: str, excludes: list[str], *, pc_config
) -> bool:
    """Preview both passes synchronously; never writes state."""
    passes = [
        ("cluster→pc", f"{cluster_dir}/", f"{pc_dir}/"),
        ("pc→cluster", f"{pc_dir}/", f"{cluster_dir}/"),
    ]
    for label, src, dst in passes:
        cmd = _rsync_cmd(src, dst, excludes, dry_run=True)
        if label == "cluster→pc":
            cmd = f"mkdir -p {shlex.quote(pc_dir)} && {cmd}"
        console.print(f"[dim]{name} {label}: {cmd}[/dim]")
        result = run_remote(cmd, config=pc_config, timeout=_DRY_RUN_TIMEOUT)
        if result.stdout.strip():
            console.print(result.stdout.rstrip())
        if result.returncode != 0:
            detail = f"{label} failed: {result.stderr.strip()}"
            console.print(f"[red]✗ {name}: {detail}[/red]")
            return False
    console.print(f"[green]✓ {name} (dry run)[/green]")
    return True


def mirror_dataset(
    name: str,
    spec: dict,
    *,
    cluster_from_pc: str,
    pc_config,
    dry_run: bool = False,
    verbose: bool = False,
) -> bool:
    """Union-sync one dataset: cluster→pc, then pc→cluster.

    The real transfer is submitted as a detached job on the PC and polled
    to completion, so a dropped Mac↔PC SSH connection can't abort an
    in-progress multi-hour transfer (see module docstring).
    """
    excludes = spec.get("exclude") or []
    pc_dir = str(spec["pc"]).rstrip("/")
    cluster_dir = f"{cluster_from_pc}:{str(spec['cluster']).rstrip('/')}"

    if dry_run:
        return _dry_run_dataset(
            name, pc_dir, cluster_dir, excludes, pc_config=pc_config
        )

    job_id = _find_running_job(name, pc_config)
    if job_id is not None:
        console.print(f"[dim]{name}: reusing in-flight PC job {job_id}[/dim]")
    else:
        pass1 = _rsync_cmd(f"{cluster_dir}/", f"{pc_dir}/", excludes, dry_run=False)
        pass2 = _rsync_cmd(f"{pc_dir}/", f"{cluster_dir}/", excludes, dry_run=False)
        command = f"mkdir -p {shlex.quote(pc_dir)} && {pass1} && {pass2}"
        handle = submit(command, name=f"mirror-{name}", config=pc_config)
        job_id = handle.job_id
        console.print(f"[dim]{name}: submitted as PC job {job_id}[/dim]")

    deadline = time.monotonic() + _MAX_WAIT
    while time.monotonic() < deadline:
        try:
            status = job_status(job_id, config=pc_config)
        except JobError as exc:
            if verbose:
                console.print(f"[dim]{name}: poll failed, retrying: {exc}[/dim]")
            time.sleep(_POLL_INTERVAL)
            continue

        state = status.get("state")
        if state == "RUNNING":
            time.sleep(_POLL_INTERVAL)
            continue
        if state == "COMPLETED":
            console.print(f"[green]✓ {name} mirrored ({job_id})[/green]")
            _write_state(name, True, "")
            return True

        detail = f"job {job_id} ended {state}"
        try:
            detail += f":\n{read_log(job_id, lines=20, config=pc_config)}"
        except JobError:
            pass
        console.print(f"[red]✗ {name}: {detail}[/red]")
        _write_state(name, False, detail)
        return False

    # We genuinely don't know the outcome here (every poll failed to reach
    # a terminal state before the deadline, e.g. the Mac was asleep/offline
    # for hours) — the job itself may already have completed successfully
    # on the PC. Don't overwrite prior state with a false failure; leave it
    # for the next run (or a manual `job status`) to resolve.
    detail = (
        f"gave up polling job {job_id} after {_MAX_WAIT}s without reaching a "
        f"terminal state; check `cluster-kit -p pc job status {job_id}`"
    )
    console.print(f"[yellow]⚠ {name}: {detail}[/yellow]")
    return False


def run_mirror(
    manifest_path: Path,
    *,
    dataset: Optional[str] = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> bool:
    """Mirror all datasets in the manifest (or just *dataset*)."""
    manifest = load_manifest(manifest_path)
    datasets = manifest["datasets"]
    _prune_state(set(datasets))
    if dataset is not None:
        if dataset not in datasets:
            raise MirrorError(
                f"dataset '{dataset}' not in manifest (have: {', '.join(datasets)})"
            )
        datasets = {dataset: datasets[dataset]}

    pc_config = load_config(env_profile="pc")
    try:
        ensure_reachable(config=pc_config)
    except RemoteUnreachableError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        return False

    ok = True
    for name, spec in datasets.items():
        ok &= mirror_dataset(
            name,
            spec,
            cluster_from_pc=manifest["cluster_from_pc"],
            pc_config=pc_config,
            dry_run=dry_run,
            verbose=verbose,
        )
    return ok
