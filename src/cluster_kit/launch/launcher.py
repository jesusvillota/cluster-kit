"""SLURM-integrated launcher for Python scripts.

Provides a clean interface for running scripts locally or submitting them to a
SLURM cluster, with support for custom environment variables.

Public API
----------
``add_launcher_args(parser, ...)``
    Adds --run-from and SLURM resource arguments to an argparse parser.
    Supports optional --mode flag for array mode when array_mode=True.

``maybe_launch(script_path, args, env_vars=None)``
    Gate function called at the top of main(). Returns True if execution was
    handled (script should exit), False if the script should proceed with
    normal processing.

Integration pattern (add to every main script):
    from cluster_kit.launch.launcher import add_launcher_args, maybe_launch

    def parse_args():
        parser = argparse.ArgumentParser(...)
        add_launcher_args(parser, partition="cpu_shared")
        return parser.parse_args()

    def main():
        args = parse_args()
        if maybe_launch(__file__, args, env_vars={"MY_VAR": "value"}):
            return
        # ... normal processing below ...
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel

from cluster_kit.config import get_cluster_host, get_remote_base, get_texlive_root
from cluster_kit.sync.lock import deploy_lock_path

__all__ = [
    "add_launcher_args",
    "maybe_launch",
    "render_sbatch_argv",
    "resolve_slurm_resources",
    "submit_command",
    "submit_job",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Launcher-specific CLI flags (stripped from sys.argv when rebuilding commands)
_LAUNCHER_FLAGS_WITH_VALUE = frozenset(
    {
        "--run-from",
        "--partition",
        "--qos",
        "--slurm-cpus",
        "--slurm-mem",
        "--slurm-time",
    }
)
_LAUNCHER_MODE_FLAG = "--mode"
_LAUNCHER_FLAGS_BOOLEAN: frozenset[str] = frozenset()

PARTITION_DEFAULTS: dict[str, tuple[int, str, str]] = {
    "cpu_express": (16, "96G", "02:00:00"),
    "cpu_shared": (32, "240G", "24:00:00"),
    "cpu_large": (64, "240G", "48:00:00"),
    "cpu_long": (32, "160G", "168:00:00"),
    "cpu_long_unlimited": (16, "32G", "UNLIMITED"),
    "gpu_express": (16, "44G", "02:00:00"),
    "gpu_compute": (32, "88G", "72:00:00"),
    "gpu_long": (16, "77G", "168:00:00"),
    "gpu_long_unlimited": (16, "8G", "UNLIMITED"),
}

# Legacy per-repo worker location. Still honoured so repos migrate by deleting
# it, but cluster-kit now ships the worker itself.
_DEFAULT_WORKER_SCRIPT = Path("runnables/slurm/worker.slurm")

# Where `sync code` deploys the packaged worker, relative to remote_base.
# Lives under .cluster_kit so each git worktree gets its own copy (that
# directory is never symlinked between worktree deployments).
REMOTE_WORKER_RELPATH = ".cluster_kit/worker.slurm"

# Rich console for launcher output
_console = Console()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_launcher_args(
    parser: argparse.ArgumentParser,
    *,
    partition: str = "cpu_express",
    cpus: int | None = None,
    mem: str | None = None,
    time: str | None = None,
    array_mode: bool = False,
) -> None:
    """Add launcher CLI arguments (--run-from, optional --mode, SLURM resources).

    Args:
        parser: The argparse parser to extend.
        partition: Default SLURM partition (script-specific).
        cpus: Default CPUs per task. If None, uses partition default.
        mem: Default memory allocation. If None, uses partition default.
        time: Default wall-clock time limit. If None, uses partition default.
        array_mode: Whether to expose ``--mode`` for array job workflows.
    """
    _defaults = PARTITION_DEFAULTS.get(partition, PARTITION_DEFAULTS["cpu_express"])
    _cpus_default = cpus if cpus is not None else _defaults[0]
    _mem_default = mem if mem is not None else _defaults[1]
    _time_default = time if time is not None else _defaults[2]

    group = parser.add_argument_group("Launcher (SLURM integration)")
    group.add_argument(
        "--run-from",
        choices=["local", "cluster", "pc"],
        default="local",
        help=(
            "Run locally, submit to the SLURM cluster, or run as a detached "
            "job on a plain remote machine via the CLUSTER_PC_* profile "
            "(default: local)."
        ),
    )
    if array_mode:
        group.add_argument(
            "--mode",
            choices=["sequential", "array"],
            default="sequential",
            help=(
                "How to handle multi-job submissions: sequential (one by one) "
                "or array (parallel). Default: sequential."
            ),
        )
    group.add_argument(
        "--partition",
        default=partition,
        help=f"SLURM partition (default: {partition}).",
    )
    group.add_argument(
        "--qos",
        default=None,
        help="SLURM QoS (default: same as partition).",
    )
    group.add_argument(
        "--slurm-cpus",
        type=int,
        default=None,
        help=(f"CPUs per SLURM task (default: {_cpus_default} from {partition})."),
    )
    group.add_argument(
        "--slurm-mem",
        default=None,
        help=(f"SLURM memory allocation (default: {_mem_default} from {partition})."),
    )
    group.add_argument(
        "--slurm-time",
        default=None,
        help=(
            f"SLURM wall-clock time limit (default: {_time_default} from {partition})."
        ),
    )


def resolve_slurm_resources(args: argparse.Namespace) -> None:
    """Resolve None SLURM resources from partition defaults. Mutates args in-place."""
    partition = getattr(args, "partition", "cpu_express")
    defaults = PARTITION_DEFAULTS.get(partition, PARTITION_DEFAULTS["cpu_express"])

    if getattr(args, "slurm_cpus", None) is None:
        args.slurm_cpus = defaults[0]
    if getattr(args, "slurm_mem", None) is None:
        args.slurm_mem = defaults[1]
    if getattr(args, "slurm_time", None) is None:
        args.slurm_time = defaults[2]


def maybe_launch(
    script_path: str,
    args: argparse.Namespace,
    *,
    env_vars: dict[str, str] | None = None,
    fan_out: Sequence[str] | None = None,
    fan_out_flag: str | None = None,
) -> bool:
    """Gate function: handle execution if needed, return True if handled.

    Call this at the top of ``main()``, before any heavy processing.
    When it returns ``True``, the script should ``return`` immediately —
    the launcher has taken care of everything.

    Args:
        script_path: The script's ``__file__``.
        args: Parsed CLI namespace (must include launcher args).
        env_vars: Optional dictionary of environment variables to export
            to the SLURM job.
        fan_out: Optional values to spread across independent jobs — one job
            per value, all running concurrently.  With a single value (or
            None) exactly one job is submitted, so callers do not need to
            special-case the common path.
        fan_out_flag: CLI flag used to pass each *fan_out* value to the script,
            e.g. ``"--whale-definitions"``.  Required when *fan_out* is given.

    Returns:
        True if execution was handled (caller should exit), False otherwise.

    Raises:
        ValueError: If *fan_out* is given without *fan_out_flag*.
    """
    if fan_out and not fan_out_flag:
        raise ValueError("fan_out requires fan_out_flag")

    # A script that exposes --mode lets the user choose at run time; sequential
    # means "one job, script loops over the values itself", which is what the
    # values already in argv give us. Scripts without --mode always fan out
    # when the caller passes fan_out, so asking for it is never a silent no-op.
    if fan_out and getattr(args, "mode", None) == "sequential":
        fan_out = None

    run_from: str = getattr(args, "run_from", "local")

    # -- Cluster submission (always handled by the launcher) --
    if run_from == "cluster":
        project_root = _find_project_root(script_path)
        if _confirm_and_prepare_cluster_submission(project_root):
            return True
        _handle_cluster_submission(
            script_path, args, env_vars, fan_out=fan_out, fan_out_flag=fan_out_flag
        )
        return True

    # -- Detached submission on a plain remote machine (no SLURM) --
    if run_from == "pc":
        _handle_pc_submission(
            script_path, args, fan_out=fan_out, fan_out_flag=fan_out_flag
        )
        return True

    # -- Local fan-out: one subprocess per value, run concurrently --
    # A single value runs in-process so local debugging keeps a normal stack.
    if fan_out and len(fan_out) > 1:
        _run_local_fan_out(script_path, fan_out, fan_out_flag)
        return True

    # -- Local execution: let the script handle it normally --
    return False


# ---------------------------------------------------------------------------
# Internal: project root detection
# ---------------------------------------------------------------------------


def _find_project_root(script_path: str) -> Path:
    """Find project root by looking for pyproject.toml or .git.

    Searches upwards from the script path.
    """
    abs_script = Path(script_path).resolve()

    for parent in abs_script.parents:
        if (parent / "pyproject.toml").exists():
            return parent
        if (parent / ".git").is_dir():
            return parent

    # Fallback: use the script's parent directory
    return abs_script.parent


# ---------------------------------------------------------------------------
# Internal: cluster submission preparation
# ---------------------------------------------------------------------------


def _is_interactive_terminal() -> bool:
    """Return True when stdin and stdout are both interactive terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _run_cluster_sync(project_root: Path) -> bool:
    """Run the local-to-cluster sync command before submission."""
    try:
        result = subprocess.run(
            ["uv", "run", "cluster-kit", "sync", "code"],
            cwd=project_root,
            check=False,
            capture_output=True,
        )
    except Exception:
        _console.print("  [red]Cluster sync failed; aborting submission[/red]")
        return False

    if result.returncode != 0:
        _console.print("  [red]Cluster sync failed; aborting submission[/red]")
        return False

    return True


def _confirm_and_prepare_cluster_submission(project_root: Path) -> bool:
    """Scaffold for cluster-preflight confirmation.

    Returns True to abort submission, False to proceed.
    """
    if not _is_interactive_terminal():
        _console.print(
            "  [dim]Non-interactive cluster submission;"
            " skipping pre-submit prompt[/dim]"
        )
        return False

    try:
        from rich.prompt import Prompt

        answer = Prompt.ask(
            (
                f"[cyan]Sync local codebase to cluster before submission?[/cyan] "
                f"{project_root}\n"
                "[green]yes[/green] = run sync, then submit; "
                "[yellow]no[/yellow] = submit without syncing"
            ),
            choices=["yes", "no"],
            show_choices=True,
        )
    except (KeyboardInterrupt, EOFError):
        _console.print("  [yellow]Cluster sync cancelled; aborting submission[/yellow]")
        return True

    if answer == "no":
        _console.print(
            "  [yellow]Cluster sync declined; continuing with submission[/yellow]"
        )
        return False

    if not _run_cluster_sync(project_root):
        return True

    return False


# ---------------------------------------------------------------------------
# Internal: sys.argv manipulation
# ---------------------------------------------------------------------------


def _strip_launcher_flags_from_argv() -> list[str]:
    """Return sys.argv[1:] with launcher flags removed.

    Handles both ``--flag value`` and ``--flag=value`` forms.
    """
    argv = sys.argv[1:]
    result: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        eq_flag = arg.split("=", 1)[0] if "=" in arg else None

        # Launcher flags with a value
        if arg in _LAUNCHER_FLAGS_WITH_VALUE:
            i += 2  # skip flag + value
        elif eq_flag and eq_flag in _LAUNCHER_FLAGS_WITH_VALUE:
            i += 1  # skip --flag=value

        # Launcher boolean flags
        elif arg in _LAUNCHER_FLAGS_BOOLEAN:
            i += 1

        # Keep everything else
        else:
            result.append(argv[i])
            i += 1

    return result


# ---------------------------------------------------------------------------
# Internal: naming helpers
# ---------------------------------------------------------------------------


def _derive_job_name(script_path: str) -> str:
    """Derive a SLURM job name from the script filename.

    ``process_event_study.py`` → ``process_event_study``
    """
    name = Path(script_path).stem
    # Strip common suffixes
    name = name.replace("_SLURM-INTEGRATED", "")
    return name


def _slug(value: str) -> str:
    """Make a fan-out value safe to embed in a SLURM job name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "x"


def _strip_flag(argv: list[str], flag: str) -> list[str]:
    """Remove ``flag`` and its value from a forwarded argument list.

    The fan-out flag is normally already present in ``sys.argv`` (that is how
    the caller learned the values in the first place). Each fan-out job appends
    its own single value, so the original must come out or the script would see
    the flag twice and silently use whichever argparse kept.

    Handles both ``--flag value`` and ``--flag=value`` forms.
    """
    out: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token == flag:
            skip_next = True
            continue
        if token.startswith(f"{flag}="):
            continue
        out.append(token)
    return out


def _derive_log_dir(script_path: str, project_root: Path) -> str:
    """Derive the SLURM log directory from the script path.

    Returns a path relative to project root in the format:
    ``_logs_/{relative_path_to_script_parent}``
    """
    abs_script = Path(script_path).resolve()

    try:
        rel_parts = abs_script.parent.relative_to(project_root).parts
    except ValueError:
        # Fallback: use parent directory name
        rel_parts = (
            abs_script.parent.parts[-2:]
            if len(abs_script.parent.parts) > 1
            else ["scripts"]
        )

    return f"_logs_/{'/'.join(rel_parts)}"


def _needs_texlive(script_path: str) -> bool:
    """Auto-detect whether a script needs TeX Live."""
    stem = Path(script_path).stem.lower()
    return any(kw in stem for kw in ("visualize", "render", "plot", "figure"))


def _resolve_worker_script(
    project_root: Path,
    worker_script: Path | str | None,
) -> Path | None:
    """Resolve the worker script path within the project root."""
    candidate = (
        Path(worker_script)
        if worker_script is not None
        else _DEFAULT_WORKER_SCRIPT
    )
    if not candidate.is_absolute():
        candidate = (project_root / candidate).resolve()
    else:
        candidate = candidate.expanduser().resolve()

    try:
        candidate.relative_to(project_root)
    except ValueError:
        _console.print(
            "[red]Worker script must live under the project root so it can be "
            f"synced:[/red] {candidate}"
        )
        return None

    if not candidate.exists():
        _console.print(f"[red]Worker script not found:[/red] {candidate}")
        return None

    return candidate


def resolve_remote_worker(
    project_root: Path,
    worker_script: Path | str | None,
    remote_base: str,
) -> str | None:
    """Return the remote path of the worker script to submit.

    cluster-kit owns the worker: ``sync code`` deploys the packaged template to
    ``{remote_base}/.cluster_kit/worker.slurm``, so repos need no copy of their
    own.  A repo-local ``runnables/slurm/worker.slurm`` still wins if present,
    which makes deleting that file the per-repo migration switch.

    Args:
        project_root: Local project root.
        worker_script: Explicit override, or None for the default.
        remote_base: Remote deployment root.

    Returns:
        The remote worker path, or None if an explicit override was invalid.
    """
    if worker_script is None and not (project_root / _DEFAULT_WORKER_SCRIPT).exists():
        return f"{remote_base}/{REMOTE_WORKER_RELPATH}"

    resolved = _resolve_worker_script(project_root, worker_script)
    if resolved is None:
        return None

    if worker_script is None:
        _console.print(
            "[yellow]Using repo-local "
            f"{_DEFAULT_WORKER_SCRIPT}[/yellow] — cluster-kit now ships the "
            "worker itself; delete that file to use the centralized one."
        )

    return f"{remote_base}/{resolved.relative_to(project_root).as_posix()}"


# ---------------------------------------------------------------------------
# Internal: cluster submission
# ---------------------------------------------------------------------------


def _handle_cluster_submission(
    script_path: str,
    args: argparse.Namespace,
    env_vars: dict[str, str] | None,
    *,
    fan_out: Sequence[str] | None = None,
    fan_out_flag: str | None = None,
) -> None:
    """Submit job(s) to the SLURM cluster via SSH."""
    resolve_slurm_resources(args)
    script_args = _strip_launcher_flags_from_argv()

    # Resolve script path relative to project root
    abs_script = Path(script_path).resolve()
    project_root = _find_project_root(script_path)

    try:
        rel_script = str(abs_script.relative_to(project_root))
    except ValueError:
        rel_script = str(abs_script)

    remote_base = get_remote_base()

    # SLURM parameters
    partition: str = getattr(args, "partition", "cpu_express")
    qos: str = getattr(args, "qos", None) or partition
    cpus: int = getattr(args, "slurm_cpus")
    mem: str = getattr(args, "slurm_mem")
    slurm_time: str = getattr(args, "slurm_time")
    job_name = _derive_job_name(script_path)
    log_dir = _derive_log_dir(script_path, project_root)
    texlive = _needs_texlive(script_path)

    # Get email from environment or use default
    mail_user = os.getenv("CLUSTER_EMAIL", "")

    # Ensure log directory exists on cluster
    remote_log_dir = f"{remote_base}/{log_dir}"
    _ssh_run(f"mkdir -p '{remote_log_dir}'")

    _console.print(
        Panel(
            (
                f"[cyan]Partition:[/cyan]  {partition}\n"
                f"[cyan]QoS:[/cyan]       {qos}\n"
                f"[cyan]CPUs:[/cyan]      {cpus}\n"
                f"[cyan]Memory:[/cyan]    {mem}\n"
                f"[cyan]Time:[/cyan]      {slurm_time}\n"
                f"[cyan]Job name:[/cyan]  {job_name}\n"
                f"[cyan]Log dir:[/cyan]   {remote_log_dir}\n"
                f"[cyan]TeX Live:[/cyan]  {'yes' if texlive else 'no'}"
            ),
            title="[bold]SLURM Submission",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )

    # One independent job per fan-out value; no fan-out means a single job.
    # Deliberately not a SLURM array: arrays force every task to share one
    # resource request and one log file pattern, and the worker would have to
    # know how to index into the value list. N plain jobs give the same
    # parallelism with none of that coupling.
    values = list(fan_out) if fan_out else [None]
    base_args = _strip_flag(script_args, fan_out_flag) if fan_out else script_args
    for value in values:
        job_args = list(base_args)
        suffix = ""
        if value is not None:
            job_args += [fan_out_flag, value]
            suffix = f"_{_slug(value)}"

        _submit_single(
            rel_script,
            args,
            job_args,
            partition,
            qos,
            cpus,
            mem,
            slurm_time,
            f"{job_name}{suffix}",
            remote_log_dir,
            texlive,
            env_vars,
            mail_user,
            project_root,
        )


# ---------------------------------------------------------------------------
# Internal: local fan-out
# ---------------------------------------------------------------------------


def _run_local_fan_out(
    script_path: str,
    fan_out: Sequence[str],
    fan_out_flag: str,
) -> None:
    """Run one subprocess per fan-out value locally, concurrently.

    The local counterpart of the cluster/PC fan-out: same one-job-per-value
    shape, without a scheduler. Each child is re-invoked with a single value and
    ``--mode sequential`` so it does not fan out again.

    Raises:
        SystemExit: With code 1 if any subprocess fails, so callers and CI see
            the failure rather than a zero exit with errors buried in the log.
    """
    script_args = _strip_flag(_strip_launcher_flags_from_argv(), fan_out_flag)

    _console.print(
        Panel(
            f"Spawning [bold]{len(fan_out)}[/bold] parallel subprocesses: "
            f"[cyan]{', '.join(fan_out)}[/cyan]",
            title="[bold]Local Parallel Execution",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )

    processes: list[tuple[str, subprocess.Popen]] = []
    for value in fan_out:
        cmd = [
            sys.executable,
            script_path,
            fan_out_flag,
            value,
            "--run-from",
            "local",
            "--mode",
            "sequential",
            *script_args,
        ]
        _console.print(f"  Spawning subprocess for [cyan]{value}[/cyan]")
        processes.append((value, subprocess.Popen(cmd)))

    failed = []
    for value, proc in processes:
        if proc.wait() != 0:
            failed.append(value)
            _console.print(
                f"  [red][FAIL][/red] {value} exited with code {proc.returncode}"
            )
        else:
            _console.print(f"  [green][OK][/green] {value} completed")

    if failed:
        _console.print(
            f"\n[red]Failed ({len(failed)}/{len(fan_out)}):[/red] {', '.join(failed)}"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Internal: PC (plain remote machine) submission
# ---------------------------------------------------------------------------


def _load_pc_config(project_root: Path):
    """Load and validate the ``CLUSTER_PC_*`` profile.

    Returns:
        The validated :class:`~cluster_kit.config.ClusterConfig` for the profile.

    Raises:
        SystemExit: If the profile is missing, invalid, or not ssh-backed.
    """
    from cluster_kit.config import ConfigError, load_config, validate_config_strict

    try:
        config = load_config(env_file=project_root / ".env", env_profile="pc")
        validate_config_strict(config)
    except ConfigError as exc:
        _console.print(
            f"[red]PC profile config error:[/red] {exc}\n"
            "Add a CLUSTER_PC_* block to .env (see docs/pc-ssh-setup.md)."
        )
        sys.exit(1)

    if config.executor != "ssh":
        _console.print(
            "[red]--run-from pc requires CLUSTER_PC_EXECUTOR=ssh in .env[/red]"
        )
        sys.exit(1)
    return config


def _confirm_and_prepare_pc_submission(config, project_root: Path) -> bool:
    """Git-sync the remote checkout before submitting.

    Returns:
        True if submission should be aborted.
    """
    from rich.prompt import Prompt

    from cluster_kit.sync.git_sync import GitSyncer

    if not _is_interactive_terminal():
        _console.print("  [dim]Non-interactive PC submission; skipping git sync[/dim]")
        return False

    try:
        answer = Prompt.ask(
            (
                "[cyan]Git-sync the PC checkout before submission?[/cyan]\n"
                "[green]yes[/green] = commit/push prompt + PC pull, then submit; "
                "[yellow]no[/yellow] = submit against the PC's current checkout"
            ),
            choices=["yes", "no"],
            show_choices=True,
        )
    except (KeyboardInterrupt, EOFError):
        _console.print("  [yellow]PC sync cancelled; aborting submission[/yellow]")
        return True

    if answer == "no":
        _console.print(
            "  [yellow]PC sync declined; submitting against the PC's "
            "current checkout[/yellow]"
        )
        return False

    if not GitSyncer(config=config, project_root=project_root).sync():
        _console.print("  [red]PC git sync failed; aborting submission[/red]")
        return True
    return False


def _submit_pc_job(command: str, name: str, config) -> bool:
    """Submit one detached job on the PC."""
    from cluster_kit.jobs import JobError, submit

    try:
        handle = submit(command, name=name, config=config)
    except JobError as exc:
        _console.print(f"  [red]PC submission failed:[/red] {exc}")
        return False

    _console.print(
        f"  [green][OK][/green] Detached job [bold]{handle.job_id}[/bold] submitted\n"
        f"  [cyan]Log:[/cyan] {handle.log_path}\n"
        f"  Check with: [bold]uv run cluster-kit -p pc job status "
        f"{handle.job_id}[/bold]"
    )
    return True


def _handle_pc_submission(
    script_path: str,
    args: argparse.Namespace,
    *,
    fan_out: Sequence[str] | None = None,
    fan_out_flag: str | None = None,
) -> None:
    """Submit detached job(s) on the PC via the ssh executor.

    SLURM resource flags are meaningless here and are ignored.
    """
    from cluster_kit.utils.ssh import RemoteUnreachableError, ensure_reachable

    project_root = _find_project_root(script_path)
    config = _load_pc_config(project_root)

    try:
        ensure_reachable(config=config)
    except RemoteUnreachableError as exc:
        _console.print(f"  [red]{exc}[/red]")
        sys.exit(1)

    if _confirm_and_prepare_pc_submission(config, project_root):
        return

    abs_script = Path(script_path).resolve()
    try:
        rel_script = str(abs_script.relative_to(project_root))
    except ValueError:
        rel_script = str(abs_script)

    job_name = _derive_job_name(script_path)
    script_args = _strip_launcher_flags_from_argv()

    _console.print(
        Panel(
            (
                f"[cyan]Host:[/cyan]      {config.host}\n"
                f"[cyan]Remote:[/cyan]    {config.remote_base}\n"
                f"[cyan]Job name:[/cyan]  {job_name}\n"
                f"[cyan]Fan-out:[/cyan]   "
                f"{', '.join(fan_out) if fan_out else '-'}\n"
                "[dim]SLURM resource flags are ignored on the PC[/dim]"
            ),
            title="[bold]PC Submission (detached)",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )

    def _command(extra: list[str]) -> str:
        tokens = ["uv", "run", "python", rel_script, *extra]
        return " ".join(shlex.quote(token) for token in tokens)

    # Same shape as the cluster path: one detached job per fan-out value.
    base_args = _strip_flag(script_args, fan_out_flag) if fan_out else script_args
    for value in list(fan_out) if fan_out else [None]:
        extra = list(base_args)
        suffix = ""
        if value is not None:
            extra += [fan_out_flag, value]
            suffix = f"_{_slug(value)}"
        _submit_pc_job(_command(extra), f"{job_name}{suffix}", config)


def _build_sbatch_base(
    partition: str,
    qos: str,
    cpus: int,
    mem: str,
    slurm_time: str,
    job_name: str,
    log_dir: str,
    log_pattern: str,
    mail_user: str,
    dependency: str | None = None,
) -> list[str]:
    """Build base sbatch command with resource flags."""
    cmd = [
        "sbatch",
        f"--partition={partition}",
        f"--qos={qos}",
        f"--cpus-per-task={cpus}",
        f"--mem={mem}",
        f"--time={slurm_time}",
        f"--job-name={job_name}",
        f"--output={log_dir}/{log_pattern}.out",
        f"--error={log_dir}/{log_pattern}.err",
        "--ntasks=1",
    ]

    if mail_user:
        cmd.extend(
            [
                "--mail-type=BEGIN,END,FAIL",
                f"--mail-user={mail_user}",
            ]
        )

    if dependency:
        cmd.append(f"--dependency={dependency}")

    return cmd


def _submit_single(
    rel_script: str,
    args: argparse.Namespace,
    script_args: list[str],
    partition: str,
    qos: str,
    cpus: int,
    mem: str,
    slurm_time: str,
    job_name: str,
    log_dir: str,
    texlive: bool,
    env_vars: dict[str, str] | None,
    mail_user: str,
    project_root: Path | None = None,
) -> None:
    """Submit a single SLURM job through the shared worker script.

    Goes through :func:`render_sbatch_argv` so this path behaves identically to
    workflow submission: same worker, same exports, same conda/uv autodetection.
    It previously inlined an ``sbatch --wrap`` script that activated a hardcoded
    ``conda_envs/cluster-kit`` prefix and exported no ``PROJECT_DIR``, which
    meant it worked for no real project and never used the worker at all.
    """
    remote_base = get_remote_base()
    root = project_root or Path.cwd()

    worker_remote_path = resolve_remote_worker(root, None, str(remote_base))
    if worker_remote_path is None:
        return

    command = " ".join(shlex.quote(s) for s in ["python", rel_script, *script_args])

    try:
        sbatch = render_sbatch_argv(
            command,
            remote_base=str(remote_base),
            partition=partition,
            cpus=cpus,
            mem=mem,
            time=slurm_time,
            job_name=job_name,
            log_dir=log_dir,
            worker_remote_path=worker_remote_path,
            mail_user=mail_user,
            qos=qos,
            texlive=texlive,
            env_vars=env_vars,
        )
    except ValueError as exc:
        _console.print(f"[red]Cannot submit:[/red] {exc}")
        return

    full_cmd = f"cd {remote_base} && {' '.join(shlex.quote(s) for s in sbatch)}"
    job_id = _ssh_submit(full_cmd)
    if job_id:
        _console.print(f"  [green][OK][/green] Job [bold]{job_id}[/bold] submitted")


# ---------------------------------------------------------------------------
# Internal: SSH helpers
# ---------------------------------------------------------------------------


def _ssh_run(remote_cmd: str) -> subprocess.CompletedProcess:
    """Run a command on the cluster via SSH."""
    host = get_cluster_host()
    return subprocess.run(
        ["ssh", host, remote_cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _ssh_submit(full_cmd: str) -> str | None:
    """Submit a job via SSH and return the job ID, or None on failure."""
    host = get_cluster_host()
    try:
        result = subprocess.run(
            ["ssh", host, full_cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            _console.print(f"  [red]sbatch error:[/red] {result.stderr.strip()}")
            return None

        match = re.search(r"Submitted batch job (\d+)", result.stdout)
        if match:
            return match.group(1)

        _console.print(
            f"  [red]Could not parse job ID from:[/red] {result.stdout.strip()}"
        )
        return None
    except subprocess.TimeoutExpired:
        _console.print("  [red]SSH connection timed out[/red]")
        return None
    except Exception as e:
        _console.print(f"  [red]SSH error:[/red] {e}")
        return None


def submit_job(
    script_path: str,
    *,
    partition: str = "cpu_shared",
    cpus: int = 16,
    mem: str = "64G",
    time: str = "04:00:00",
    qos: str | None = None,
    job_name: str | None = None,
    env_vars: dict[str, str] | None = None,
    script_args: list[str] | None = None,
    sync: bool = True,
    dependency: str | None = None,
    worker_script: Path | str | None = None,
) -> str | None:
    """Submit a Python script as a SLURM job.

    This is a convenience function for programmatic job submission without
    going through the CLI argument parsing flow.

    Args:
        script_path: Path to the Python script to run.
        partition: SLURM partition (default: cpu_shared).
        cpus: CPUs per task (default: 16).
        mem: Memory allocation (default: 64G).
        time: Wall-clock time limit (default: 04:00:00).
        qos: SLURM QoS (default: same as partition).
        job_name: Job name (default: derived from script filename).
        env_vars: Environment variables to export to the job.
        script_args: Arguments to pass to the script.
        sync: Whether to sync code before submission.
        dependency: Optional SLURM dependency expression (for example,
            ``afterok:12345``).
        worker_script: Optional worker script path relative to the project root.

    Returns:
        Job ID if submission succeeded, None otherwise.
    """
    abs_script = Path(script_path).resolve()
    if not abs_script.exists():
        _console.print(f"[red]Script not found:[/red] {abs_script}")
        return None

    project_root = _find_project_root(script_path)
    # Optional sync
    if sync:
        if not _run_cluster_sync(project_root):
            _console.print("[yellow]Sync failed; attempting submission anyway[/yellow]")

    # Resolve relative script path
    try:
        rel_script = str(abs_script.relative_to(project_root))
    except ValueError:
        rel_script = str(abs_script)

    python_cmd = ["python", rel_script]
    if script_args:
        python_cmd.extend(script_args)

    return submit_command(
        " ".join(shlex.quote(s) for s in python_cmd),
        project_root=project_root,
        partition=partition,
        cpus=cpus,
        mem=mem,
        time=time,
        qos=qos,
        job_name=job_name or _derive_job_name(script_path),
        log_dir=_derive_log_dir(script_path, project_root),
        texlive=_needs_texlive(script_path),
        env_vars=env_vars,
        sync=False,
        dependency=dependency,
        worker_script=worker_script,
    )


def submit_command(
    command: str,
    *,
    project_root: Path | str | None = None,
    partition: str = "cpu_shared",
    cpus: int = 16,
    mem: str = "64G",
    time: str = "04:00:00",
    qos: str | None = None,
    job_name: str = "cluster_workflow",
    log_dir: str = "_logs_/workflows",
    texlive: bool = False,
    env_vars: dict[str, str] | None = None,
    sync: bool = False,
    dependency: str | None = None,
    worker_script: Path | str | None = None,
) -> str | None:
    """Submit a shell command as a SLURM job.

    Args:
        command: Shell command to run from the remote project root.
        project_root: Local project root used for optional sync. Defaults to cwd.
        partition: SLURM partition.
        cpus: CPUs per task.
        mem: Memory allocation.
        time: Wall-clock time limit.
        qos: SLURM QoS (default: same as partition).
        job_name: SLURM job name.
        log_dir: Remote log directory relative to project root.
        texlive: Whether to export ``TEXLIVE=1``.
        env_vars: Extra environment variables to export.
        sync: Whether to sync before submission.
        dependency: Optional SLURM dependency expression.
        worker_script: Optional worker script path relative to the project root.

    Returns:
        Job ID if submission succeeded, None otherwise.
    """
    local_project_root = Path(project_root).resolve() if project_root else Path.cwd()
    remote_base = get_remote_base()

    if sync:
        if not _run_cluster_sync(local_project_root):
            _console.print("[yellow]Sync failed; attempting submission anyway[/yellow]")

    remote_log_dir = f"{remote_base}/{log_dir}"
    _ssh_run(f"mkdir -p '{remote_log_dir}'")

    remote_worker = resolve_remote_worker(
        local_project_root, worker_script, str(remote_base)
    )
    if remote_worker is None:
        return None

    try:
        sbatch = render_sbatch_argv(
            command,
            remote_base=remote_base,
            partition=partition,
            cpus=cpus,
            mem=mem,
            time=time,
            qos=qos,
            job_name=job_name,
            log_dir=log_dir,
            texlive=texlive,
            env_vars=env_vars,
            mail_user=os.getenv("CLUSTER_EMAIL", ""),
            worker_remote_path=remote_worker,
            dependency=dependency,
        )
    except ValueError as exc:
        _console.print(f"[red]{exc}[/red]")
        return None

    full_cmd = f"cd {remote_base} && {' '.join(shlex.quote(s) for s in sbatch)}"
    return _ssh_submit(full_cmd)


def render_sbatch_argv(
    command: str,
    *,
    remote_base: str,
    partition: str,
    cpus: int,
    mem: str,
    time: str,
    qos: str | None,
    job_name: str,
    log_dir: str,
    texlive: bool,
    env_vars: dict[str, str] | None,
    mail_user: str,
    worker_remote_path: str,
    dependency: str | None = None,
) -> list[str]:
    """Render the full sbatch argv for a command, without submitting it.

    The returned list is the exact argv to execute from ``remote_base`` on the
    cluster (``log_dir`` and the worker path are interpreted relative to it by
    SLURM). Raises ``ValueError`` for empty commands or shell operators.
    """
    command_tokens = shlex.split(command.strip())
    if not command_tokens:
        raise ValueError("Command is empty")
    if any(token in {"&&", "||", "|", ";", ">", ">>", "<"} for token in command_tokens):
        raise ValueError("Unsupported shell operators are not allowed")

    if len(command_tokens) >= 2 and command_tokens[:2] == ["uv", "run"]:
        if len(command_tokens) >= 3 and command_tokens[2] == "python":
            command_tokens = command_tokens[2:]
        else:
            command_tokens = ["python"] + command_tokens[2:]

    sbatch = _build_sbatch_base(
        partition,
        qos or partition,
        cpus,
        mem,
        time,
        job_name,
        log_dir,
        "%x_%j",
        mail_user,
        dependency,
    )

    env_parts: dict[str, str] = {}
    if texlive:
        env_parts["TEXLIVE"] = "1"
        texlive_root = get_texlive_root()
        if texlive_root:
            env_parts["CLUSTER_TEXLIVE_ROOT"] = texlive_root
    env_parts["PROJECT_DIR"] = remote_base
    # Tell the job which deployment it belongs to, so cluster_kit inside the job
    # does not fall back to the submitting shell or to a shared .env symlink.
    env_parts["CLUSTER_REMOTE_BASE"] = remote_base
    env_parts["CLUSTER_DEPLOY_LOCK_PATH"] = deploy_lock_path(remote_base)
    if env_vars:
        env_parts.update(env_vars)
    exports = ",".join(f"{key}={value}" for key, value in env_parts.items())
    sbatch.append(f"--export=ALL,{exports}")

    sbatch.append(worker_remote_path)
    sbatch.extend(command_tokens)
    return sbatch
