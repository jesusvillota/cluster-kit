"""Environment builder — generate environment.yml + conda_env.slurm from pyproject.toml.

Two modes:
  1. create  — write files locally (env create)
  2. launch  — upload files + submit slurm job to cluster (env launch)
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel

from cluster_kit.config import get_cluster_host, get_remote_base, get_slurm_partition

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_console = Console()

# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------


def parse_pyproject(path: Path) -> dict:
    """Parse pyproject.toml and return project metadata.

    Returns:
        dict with keys: name, python_version, dependencies, dev_dependencies
    """
    if not path.exists():
        raise FileNotFoundError(f"pyproject.toml not found at: {path}")

    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    project = data.get("project", {})
    name = project.get("name", "project")
    requires_python = project.get("requires-python", ">=3.12")
    dependencies = list(project.get("dependencies", []))

    optional_deps = project.get("optional-dependencies", {})
    dev_dependencies = list(optional_deps.get("dev", []))

    # uv keeps git/path dependencies out of [project].dependencies — the bare
    # name goes there and the real location in [tool.uv.sources].  pip cannot
    # resolve the bare name (it is not on PyPI), and a failed resolution aborts
    # the whole `pip:` block in the generated environment.yml, silently taking
    # unrelated packages down with it.  Rewrite them to PEP 508 direct
    # references so the environment builds.
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    if sources:
        dependencies = [_apply_uv_source(d, sources) for d in dependencies]
        dev_dependencies = [_apply_uv_source(d, sources) for d in dev_dependencies]

    python_version = _extract_python_version(requires_python)

    return {
        "name": name,
        "python_version": python_version,
        "dependencies": dependencies,
        "dev_dependencies": dev_dependencies,
    }


def _apply_uv_source(dependency: str, sources: dict) -> str:
    """Rewrite a bare dependency name using its ``[tool.uv.sources]`` entry.

    Args:
        dependency: Dependency string from ``[project].dependencies``.
        sources: The ``[tool.uv.sources]`` table.

    Returns:
        A PEP 508 direct reference (``name @ git+URL``) when the dependency has
        a git source, otherwise the dependency unchanged.
    """
    # Only bare names can be remapped; anything with a version/extra/marker
    # already says where it comes from.
    name = dependency.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return dependency

    source = sources.get(name)
    if not isinstance(source, dict):
        return dependency

    git_url = source.get("git")
    if not git_url:
        return dependency

    ref = source.get("rev") or source.get("tag") or source.get("branch")
    return f"{name} @ git+{git_url}@{ref}" if ref else f"{name} @ git+{git_url}"


def _extract_python_version(requires_python: str) -> str:
    """Extract minimum Python version from a PEP 440 version specifier."""
    match = re.search(r">=\s*(\d+\.\d+)", requires_python)
    if match:
        return match.group(1)
    match = re.search(r"~=\s*(\d+\.\d+)", requires_python)
    if match:
        return match.group(1)
    match = re.search(r"==\s*(\d+\.\d+)", requires_python)
    if match:
        return match.group(1)
    match = re.search(r"(\d+\.\d+)", requires_python)
    if match:
        return match.group(1)
    return "3.12"


# ---------------------------------------------------------------------------
# environment.yml generation
# ---------------------------------------------------------------------------

_ENV_YML_HEADER = """\
name: {name}
channels:
  - conda-forge
  - defaults
dependencies:
  - python={python_version}
  - pip"""


def generate_environment_yml(
    project_data: dict,
    include_dev: bool = False,
) -> str:
    """Generate environment.yml content from project metadata.

    All pip dependencies go under ``pip:`` to avoid fragile conda package mapping.
    """
    lines: list[str] = []
    lines.append(
        _ENV_YML_HEADER.format(
            name=project_data["name"],
            python_version=project_data["python_version"],
        )
    )

    all_deps = list(project_data.get("dependencies", []))
    if include_dev:
        all_deps.extend(project_data.get("dev_dependencies", []))

    if all_deps:
        lines.append("  - pip:")
        for dep in all_deps:
            lines.append(f"    - {dep}")
    else:
        lines.append("  - pip: []")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# conda_env.slurm generation
# ---------------------------------------------------------------------------

_CONDA_ENV_SLURM = """\
#!/bin/bash
#SBATCH --job-name=env_{project_name}
#SBATCH --output={remote_base}/_logs_/build_env/build_env.out
#SBATCH --error={remote_base}/_logs_/build_env/build_env.err
#SBATCH --partition={partition}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --time=01:00:00
#SBATCH --mem=10G

# load conda module (adjust if necessary)
module load conda/3

# Set conda cache and config directories to writable locations
export CONDA_PKGS_DIRS="{conda_base}/.conda/pkgs"
export CONDA_ENVS_DIRS="{conda_base}/.conda/envs"
export CONDA_ROOT="{conda_base}/.conda"
export XDG_CACHE_HOME="{conda_base}/.cache"

# Create necessary directories
mkdir -p "$CONDA_PKGS_DIRS"
mkdir -p "$CONDA_ENVS_DIRS"
mkdir -p "$CONDA_ROOT"
mkdir -p "$XDG_CACHE_HOME"

# activate conda environment in bash shell
eval "$(conda shell.bash hook)"

# path to the conda environment
ENV_PATH="{remote_base}/conda_envs/{project_name}/"
ENV_FILE="{remote_base}/environment.yml"

# eliminate environment if it already exists and is not valid
if [ -d "$ENV_PATH" ]; then
    echo "Removing previous environment..."
    rm -rf "$ENV_PATH"
fi

echo "Creating environment at $ENV_PATH from $ENV_FILE ..."
# A pip resolution failure aborts the whole `pip:` block while still leaving a
# usable bin/python behind, so the exit code is the only honest signal that the
# environment is complete. Checking bin/python alone reports success for an env
# that is silently missing packages.
if ! conda env create --prefix "$ENV_PATH" --file "$ENV_FILE" --yes; then
    echo "Error: conda env create failed — the environment is incomplete." >&2
    echo "       Fix the failure above and rerun; do not use this env." >&2
    exit 1
fi

# Check if environment was created successfully before trying to activate
if [ -d "$ENV_PATH" ] && [ -f "$ENV_PATH/bin/python" ]; then
    echo "Environment created successfully"
    # activate environment
    conda activate "$ENV_PATH"
    echo "Packages in freshly-created env:"
    conda list | head -n 20
    echo "Environment successfully built in $ENV_PATH"
else
    echo "Error: Environment was not created successfully"
    exit 1
fi
"""


def generate_slurm_script(
    project_name: str,
    remote_base: str,
    partition: str = "cpu_express",
) -> str:
    """Generate conda_env.slurm content with resolved cluster paths."""
    remote_base_path = PurePosixPath(str(remote_base).rstrip("/"))
    remote_base_str = remote_base_path.as_posix()
    conda_base = remote_base_path.parent.as_posix()

    return _CONDA_ENV_SLURM.format(
        project_name=project_name,
        remote_base=remote_base_str,
        conda_base=conda_base,
        partition=partition,
    )


# ---------------------------------------------------------------------------
# Mode 1: create files locally
# ---------------------------------------------------------------------------


def create_environment_files(
    pyproject_path: Path,
    output_dir: Path,
    *,
    python_version: Optional[str] = None,
    include_dev: bool = False,
    dry_run: bool = False,
    partition: Optional[str] = None,
) -> None:
    """Generate environment.yml and conda_env.slurm from pyproject.toml.

    Args:
        pyproject_path: Path to pyproject.toml.
        output_dir: Directory to write generated files.
        python_version: Override detected Python version (e.g. ``"3.10"``).
        include_dev: Include dev optional-dependencies.
        dry_run: Print generated files to stdout instead of writing.
        partition: SLURM partition for the env build job. Falls back to
            ``CLUSTER_SLURM_PARTITION`` env var, then ``"cpu_express"``.
    """
    if not pyproject_path.exists():
        _console.print(
            Panel(
                f"[red]pyproject.toml not found at:[/red] {pyproject_path}",
                title="• Error",
                border_style="red",
                box=box.ROUNDED,
            )
        )
        sys.exit(1)

    project_data = parse_pyproject(pyproject_path)
    name = project_data["name"]

    if python_version:
        project_data["python_version"] = python_version

    yml_content = generate_environment_yml(project_data, include_dev=include_dev)

    try:
        remote_base = str(get_remote_base())
    except Exception:
        remote_base = "{REMOTE_BASE}"

    if partition is None:
        try:
            partition = get_slurm_partition()
        except Exception:
            partition = "cpu_express"

    slurm_content = generate_slurm_script(name, remote_base, partition=partition)

    if dry_run:
        _console.print("[bold cyan]--- environment.yml ---[/bold cyan]")
        _console.print(yml_content)
        _console.print("[bold cyan]--- conda_env.slurm ---[/bold cyan]")
        _console.print(slurm_content)
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    env_yml_path = output_dir / "environment.yml"
    slurm_path = output_dir / "conda_env.slurm"

    env_yml_path.write_text(yml_content)
    slurm_path.write_text(slurm_content)

    _console.print()
    _console.print(
        Panel(
            f"[green]Generated environment files for[/green] [bold]{name}[/bold]",
            title="[OK] Environment Files Created",
            border_style="green",
            box=box.ROUNDED,
        )
    )
    detail = (
        f"  [cyan]environment.yml:[/cyan]  {env_yml_path}\n"
        f"  [cyan]conda_env.slurm:[/cyan]  {slurm_path}"
    )
    _console.print(detail)
    _console.print()


# ---------------------------------------------------------------------------
# Mode 2: launch (upload + submit)
# ---------------------------------------------------------------------------


def _ssh_run(remote_cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command on the cluster via SSH."""
    host = get_cluster_host()
    return subprocess.run(
        ["ssh", host, remote_cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _test_connection() -> bool:
    """Test SSH connection to the cluster."""
    try:
        result = _ssh_run("echo 'SSH connection successful'")
        return result.returncode == 0
    except Exception:
        return False


def _upload_file(local_path: Path, remote_path: str) -> bool:
    """Upload a file to the cluster via rsync."""
    host = get_cluster_host()

    try:
        result = subprocess.run(
            [
                "rsync",
                "-az",
                str(local_path),
                f"{host}:{remote_path}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception as exc:
        _console.print(f"[red]Upload failed:[/red] {exc}")
        return False


def _wait_for_job(job_id: str, check_interval: int) -> bool:
    """Poll until the slurm job completes. Returns True on success."""
    start = time.monotonic()
    last_state: Optional[str] = None
    dots = 0

    _console.print(f"\n[cyan]Waiting for job {job_id} to complete...[/cyan]")
    _console.print(
        "  (Ctrl+C to stop waiting — the job will continue on the cluster)\n"
    )

    while True:
        try:
            result = _ssh_run(
                f"squeue -j {job_id} -o '%T' --noheader 2>/dev/null",
                timeout=15,
            )
            state = result.stdout.strip()
        except (subprocess.TimeoutExpired, Exception):
            state = ""

        elapsed = int(time.monotonic() - start)

        if not state:
            break

        if state != last_state:
            _console.print(
                f"  [dim]{elapsed:>5}s  {state}[/dim]"
            )
            last_state = state
            dots = 0
        else:
            dots = (dots + 1) % 4
            _console.out(
                f"\r  [dim]{elapsed:>5}s  {state}{'.' * dots}   [/dim]",
                end="",
            )

        time.sleep(check_interval)

    _console.print(f"\n  [dim]{elapsed:>5}s  DONE[/dim]")

    try:
        result = _ssh_run(
            f"sacct -j {job_id} --format State,ExitCode --noheader "
            "--parsable2 2>/dev/null",
            timeout=15,
        )
        output = result.stdout.strip()
    except Exception:
        output = ""

    if not output:
        _console.print("  [yellow]Could not retrieve job status.[/yellow]")
        return False

    lines = output.split("\n")
    if not lines:
        return False

    main_line = lines[0]
    parts = main_line.split("|")
    state = parts[0] if len(parts) > 0 else "UNKNOWN"
    exit_code = parts[1] if len(parts) > 1 else ""

    job_ok = state == "COMPLETED" and exit_code in ("0:0", "0")
    if job_ok:
        _console.print(
            f"\n  [green][OK][/green] Job [bold]{job_id}[/bold] completed successfully"
        )
        return True
    else:
        _console.print(
            f"\n  [red][FAIL][/red] Job [bold]{job_id}[/bold] ended with state: "
            f"[yellow]{state}[/yellow]  (exit: {exit_code})"
        )
        return False


def launch_environment(
    env_file: Path,
    slurm_file: Path,
    pyproject_path: Path,
    *,
    wait: bool = False,
    check_interval: int = 30,
    python_version: Optional[str] = None,
    partition: Optional[str] = None,
) -> None:
    """Upload environment files to cluster and submit the env build job.

    Args:
        env_file: Local path to environment.yml.
        slurm_file: Local path to conda_env.slurm.
        pyproject_path: Path to pyproject.toml (used to auto-generate
            files if they don't exist).
        wait: Whether to poll until the job completes.
        check_interval: Seconds between status checks when waiting.
        python_version: Override Python version for auto-generation.
        partition: SLURM partition for the env build job. Falls back to
            ``CLUSTER_SLURM_PARTITION`` env var, then ``"cpu_express"``.
    """
    # 1. Auto-generate files if missing
    if not env_file.exists() or not slurm_file.exists():
        _console.print(
            "\n[yellow]Environment files not found — generating from "
            f"{pyproject_path}...[/yellow]"
        )
        create_environment_files(
            pyproject_path=pyproject_path,
            output_dir=env_file.parent,
            python_version=python_version,
            dry_run=False,
            partition=partition,
        )

    # 2. Load config
    try:
        remote_base = get_remote_base()
    except Exception:
        _console.print(
            Panel(
                "[red]Cluster config not loaded.[/red]\n\n"
                "Please set CLUSTER_REMOTE_BASE (and related vars) in your .env file.\n"
                "Run [bold]cluster-kit --config[/bold] to verify.",
                title="• Error",
                border_style="red",
                box=box.ROUNDED,
            )
        )
        sys.exit(1)

    remote_base = str(remote_base).rstrip("/")

    # 3. Test connection
    _console.print("\n[cyan]Testing cluster connection...[/cyan]")
    if not _test_connection():
        _console.print(
            Panel(
                "[red]Cannot connect to cluster.[/red]\n\n"
                "Please ensure:\n"
                "  \u2022 VPN is connected\n"
                "  \u2022 SSH key is configured\n"
                "  \u2022 SSH alias is configured correctly",
                title="• Connection Error",
                border_style="red",
                box=box.ROUNDED,
            )
        )
        sys.exit(1)
    _console.print("  [green][OK][/green] Connection successful")

    # 4. Create remote log directory
    log_dir = f"{remote_base}/_logs_/build_env"
    _console.print("\n[cyan]Creating log directory...[/cyan]")
    _ssh_run(f"mkdir -p '{log_dir}'")
    _console.print(f"  [green][OK][/green] {log_dir}")

    # 5. Upload files
    _console.print("\n[cyan]Uploading environment files...[/cyan]")

    remote_yml = f"{remote_base}/environment.yml"
    remote_slurm = f"{remote_base}/conda_env.slurm"

    if not _upload_file(env_file, remote_yml):
        _console.print("[red]Failed to upload environment.yml[/red]")
        sys.exit(1)
    _console.print("  [green][OK][/green] environment.yml")

    if not _upload_file(slurm_file, remote_slurm):
        _console.print("[red]Failed to upload conda_env.slurm[/red]")
        sys.exit(1)
    _console.print("  [green][OK][/green] conda_env.slurm")

    # 6. Submit job
    _console.print("\n[cyan]Submitting slurm job...[/cyan]")
    try:
        result = _ssh_run(f"cd '{remote_base}' && sbatch conda_env.slurm")
        if result.returncode != 0:
            _console.print(
                Panel(
                    f"[red]sbatch error:[/red]\n{result.stderr.strip()}",
                    title="• Submission Error",
                    border_style="red",
                    box=box.ROUNDED,
                )
            )
            sys.exit(1)

        match = re.search(r"Submitted batch job (\d+)", result.stdout)
        if not match:
            _console.print(
                f"[red]Could not parse job ID from:[/red] {result.stdout.strip()}"
            )
            sys.exit(1)

        job_id = match.group(1)
    except subprocess.TimeoutExpired:
        _console.print("[red]SSH connection timed out during submission[/red]")
        sys.exit(1)
    except Exception as exc:
        _console.print(f"[red]Submission error:[/red] {exc}")
        sys.exit(1)

    _console.print(f"  [green][OK][/green] Job [bold]{job_id}[/bold] submitted")

    # 7. Summary
    _console.print()
    _console.print(
        Panel(
            f"[bold]Job ID:[/bold]       {job_id}\n"
            f"[bold]Log dir:[/bold]     {log_dir}/\n"
            f"[bold]Env path:[/bold]    {remote_base}/conda_envs/",
            title="[OK] Environment Build Submitted",
            border_style="green",
            box=box.ROUNDED,
        )
    )

    if wait:
        _console.print()
        success = _wait_for_job(job_id, check_interval)

        if success:
            project_name = get_project_name(pyproject_path)
            _console.print()
            _console.print(
                Panel(
                    f"[green]Environment ready at:[/green]\n"
                    f"  [cyan]{remote_base}/conda_envs/{project_name}/[/cyan]\n\n"
                    f"[dim]Activate on cluster:[/dim]\n"
                    f"  conda activate {remote_base}/conda_envs/{project_name}/",
                    title="[OK] Environment Ready",
                    border_style="green",
                    box=box.ROUNDED,
                )
            )
    else:
        _console.print(
            f"\n[dim]Run [bold]cluster-kit env launch --wait[/bold] to monitor "
            f"or check logs at:[/dim]\n"
            f"  [dim]{log_dir}/build_env.out[/dim]"
        )


def get_project_name(pyproject_path: Path) -> str:
    """Extract project name from pyproject.toml."""
    try:
        data = parse_pyproject(pyproject_path)
        return data["name"]
    except Exception:
        return "project"
