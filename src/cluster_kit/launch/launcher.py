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
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel

from cluster_kit.config import get_cluster_host, get_remote_base

__all__ = [
    "add_launcher_args",
    "maybe_launch",
    "resolve_slurm_resources",
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
        choices=["local", "cluster"],
        default="local",
        help="Run locally or submit to SLURM cluster (default: local).",
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

    Returns:
        True if execution was handled (caller should exit), False otherwise.
    """
    run_from: str = getattr(args, "run_from", "local")

    # -- Cluster submission (always handled by the launcher) --
    if run_from == "cluster":
        project_root = _find_project_root(script_path)
        if _confirm_and_prepare_cluster_submission(project_root):
            return True
        _handle_cluster_submission(script_path, args, env_vars)
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

    return f"_logs/{'/'.join(rel_parts)}"


def _needs_texlive(script_path: str) -> bool:
    """Auto-detect whether a script needs TeX Live."""
    stem = Path(script_path).stem.lower()
    return any(kw in stem for kw in ("visualize", "render", "plot", "figure"))


# ---------------------------------------------------------------------------
# Internal: cluster submission
# ---------------------------------------------------------------------------


def _handle_cluster_submission(
    script_path: str,
    args: argparse.Namespace,
    env_vars: dict[str, str] | None,
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

    _submit_single(
        rel_script,
        args,
        script_args,
        partition,
        qos,
        cpus,
        mem,
        slurm_time,
        job_name,
        remote_log_dir,
        texlive,
        env_vars,
        mail_user,
    )


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
) -> None:
    """Submit a single SLURM job."""
    remote_base = get_remote_base()

    sbatch = _build_sbatch_base(
        partition, qos, cpus, mem, slurm_time, job_name, log_dir, "%x_%j", mail_user
    )

    # Build environment variables for export
    env_parts: list[str] = []
    if texlive:
        env_parts.append("TEXLIVE=1")
    if env_vars:
        for key, value in env_vars.items():
            env_parts.append(f"{key}={value}")

    if env_parts:
        sbatch.append(f"--export=ALL,{','.join(env_parts)}")

    # Build Python command
    python_cmd = ["python", rel_script]
    python_cmd.extend(script_args)

    # Create a wrapper script inline
    wrapper = f"""#!/bin/bash
eval "$(conda shell.bash hook)"
conda activate "{remote_base}/conda_envs/cluster-kit"
cd "{remote_base}"
{" ".join(shlex.quote(s) for s in python_cmd)}
"""

    sbatch.append("--wrap")
    sbatch.append(wrapper)

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

    Returns:
        Job ID if submission succeeded, None otherwise.
    """
    abs_script = Path(script_path).resolve()
    if not abs_script.exists():
        _console.print(f"[red]Script not found:[/red] {abs_script}")
        return None

    project_root = _find_project_root(script_path)
    remote_base = get_remote_base()

    # Optional sync
    if sync:
        if not _run_cluster_sync(project_root):
            _console.print("[yellow]Sync failed; attempting submission anyway[/yellow]")

    # Resolve relative script path
    try:
        rel_script = str(abs_script.relative_to(project_root))
    except ValueError:
        rel_script = str(abs_script)

    # Derive parameters
    qos = qos or partition
    job_name = job_name or _derive_job_name(script_path)
    log_dir = _derive_log_dir(script_path, project_root)
    texlive = _needs_texlive(script_path)
    mail_user = os.getenv("CLUSTER_EMAIL", "")

    # Ensure log directory exists
    remote_log_dir = f"{remote_base}/{log_dir}"
    _ssh_run(f"mkdir -p '{remote_log_dir}'")

    # Build sbatch command
    sbatch = _build_sbatch_base(
        partition, qos, cpus, mem, time, job_name, log_dir, "%x_%j", mail_user
    )

    # Environment variables
    env_parts: list[str] = []
    if texlive:
        env_parts.append("TEXLIVE=1")
    if env_vars:
        for key, value in env_vars.items():
            env_parts.append(f"{key}={value}")
    if env_parts:
        sbatch.append(f"--export=ALL,{','.join(env_parts)}")

    # Python command
    python_cmd = ["python", rel_script]
    if script_args:
        python_cmd.extend(script_args)

    # Wrapper script
    wrapper = f"""#!/bin/bash
eval "$(conda shell.bash hook)"
conda activate "{remote_base}/conda_envs/cluster-kit"
cd "{remote_base}"
{" ".join(shlex.quote(s) for s in python_cmd)}
"""

    sbatch.append("--wrap")
    sbatch.append(wrapper)

    full_cmd = f"cd {remote_base} && {' '.join(shlex.quote(s) for s in sbatch)}"
    return _ssh_submit(full_cmd)
