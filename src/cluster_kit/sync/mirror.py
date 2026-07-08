"""Data mirror between the cluster and an ssh-executor host (the PC).

Keeps dataset directories identical on both machines with a two-pass union
rsync (``--update``, never ``--delete``): newest mtime wins, nothing is
deleted, both sides converge to the union. Safe for append-mostly data.

The rsync runs ON the PC against the cluster, so data flows directly over
the CEMFI LAN and never routes through this machine; we only orchestrate
via ``ssh pc '...'``.

Datasets come from a manifest in the consuming repo:

    # mirror.yaml
    cluster_from_pc: j-vill36@192.168.1.61   # how the PC addresses the cluster
    datasets:
      whale_outputs:
        cluster: /mnt/slurm-beegfs/Users/j-vill36/scripts_whales/output/processed
        pc: /home/j-vill36/GitHub/whales/output/processed
        exclude: []                          # optional rsync --exclude patterns

CLI:
    $ cluster-kit sync mirror [--manifest mirror.yaml] [--dataset NAME]
                              [--dry-run] [--verbose]
"""

from __future__ import annotations

import datetime as dt
import json
import shlex
from pathlib import Path
from typing import Optional

import yaml
from rich.console import Console

from cluster_kit.config import load_config
from cluster_kit.utils.ssh import RemoteUnreachableError, ensure_reachable, run_remote

console = Console()

MIRROR_STATE_PATH = Path.home() / ".cache" / "cluster-kit" / "mirror_state.json"

# rsync on GB-scale first runs needs far more than the ssh default (30s).
_RSYNC_TIMEOUT = 3600


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


def mirror_dataset(
    name: str,
    spec: dict,
    *,
    cluster_from_pc: str,
    pc_config,
    dry_run: bool = False,
    verbose: bool = False,
) -> bool:
    """Union-sync one dataset: cluster→pc, then pc→cluster. Runs on the PC."""
    excludes = spec.get("exclude") or []
    pc_dir = str(spec["pc"]).rstrip("/")
    cluster_dir = f"{cluster_from_pc}:{str(spec['cluster']).rstrip('/')}"
    passes = [
        ("cluster→pc", f"{cluster_dir}/", f"{pc_dir}/"),
        ("pc→cluster", f"{pc_dir}/", f"{cluster_dir}/"),
    ]
    for label, src, dst in passes:
        cmd = _rsync_cmd(src, dst, excludes, dry_run)
        if label == "cluster→pc":
            cmd = f"mkdir -p {shlex.quote(pc_dir)} && {cmd}"
        if verbose or dry_run:
            console.print(f"[dim]{name} {label}: {cmd}[/dim]")
        result = run_remote(cmd, config=pc_config, timeout=_RSYNC_TIMEOUT)
        if (verbose or dry_run) and result.stdout.strip():
            console.print(result.stdout.rstrip())
        if result.returncode != 0:
            detail = f"{label} failed: {result.stderr.strip()}"
            console.print(f"[red]✗ {name}: {detail}[/red]")
            if not dry_run:
                _write_state(name, False, detail)
            return False
    console.print(f"[green]✓ {name} mirrored[/green]")
    if not dry_run:
        _write_state(name, True, "")
    return True


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
