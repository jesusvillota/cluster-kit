"""Code synchronization to cluster.

Deploys local code directories to the cluster, replacing existing directories.
Cleans local Python cache before syncing; remote cache cleaning is skipped
because the remote directories are fully removed and re-synced.

Example:
    >>> from cluster_kit.sync.code import CodeDeployer
    >>> deployer = CodeDeployer(dry_run=True)
    >>> deployer.deploy()

CLI:
    $ cluster-kit sync code --dry-run
    $ cluster-kit sync code --verbose
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cluster_kit.config import (
    get_canonical_remote_base,
    get_cluster_host,
    get_remote_base,
)
from cluster_kit.sync.lock import RemoteDeployLock, RemoteDeployLockError
from cluster_kit.utils import (
    ClusterConnection,
    PythonCacheCleaner,
    RsyncRunner,
    ScpRunner,
    show_config_panel,
    show_error_panel,
    show_step_header,
    show_success_panel,
)

# ---------------------------------------------------------------------------
# Console and Path Utilities
# ---------------------------------------------------------------------------


def _get_console() -> Console:
    """Create a Rich Console that works on both macOS and Windows.

    On Windows the default stdout codec is cp1252 which cannot encode Unicode
    characters used by Rich (checkmarks, box-drawing, etc.). Wrapping stdout
    in a UTF-8 TextIOWrapper makes Rich skip the broken legacy Windows
    renderer and write UTF-8 directly.
    """
    import io

    if sys.platform == "win32":
        utf8_stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        return Console(file=utf8_stdout)
    return Console()


console = _get_console()


def _find_project_root(start_path: Optional[Path] = None) -> Path:
    """Find the project root by looking for pyproject.toml or .git directory.

    Searches upward from the current directory or the provided start path
    until it finds a marker file/directory indicating the project root.

    Args:
        start_path: Optional path to start searching from (default: cwd)

    Returns:
        Path to the project root directory

    Raises:
        RuntimeError: If no project root marker is found
    """
    if start_path is None:
        start_path = Path.cwd()

    current = start_path.resolve()

    # Search upward for project markers
    for path in [current] + list(current.parents):
        # Check for pyproject.toml (modern Python projects)
        if (path / "pyproject.toml").exists():
            return path
        # Check for setup.py (legacy Python projects)
        if (path / "setup.py").exists():
            return path
        # Check for .git directory
        if (path / ".git").is_dir():
            return path

    # Fallback: return cwd if no marker found
    # This allows the deployer to work in any directory
    return current


# ---------------------------------------------------------------------------
# CodeDeployer Class
# ---------------------------------------------------------------------------


class CodeDeployer:
    """Handle deployment of local code to cluster."""

    def __init__(
        self,
        dry_run: bool = False,
        verbose: bool = False,
        directories: Optional[list[str]] = None,
    ):
        """Initialize CodeDeployer.

        Args:
            dry_run: If True, preview actions without executing
            verbose: If True, show detailed output
            directories: List of directory names to sync (default: ["src", "runnables"])
        """
        self.dry_run = dry_run
        self.verbose = verbose
        self.directories = directories or ["src", "runnables"]
        self._local_base = _find_project_root()
        self._remote_base = get_remote_base()
        self._ssh_host = get_cluster_host()

    def show_configuration(self):
        """Display deployment configuration."""
        console.print(
            Panel(
                "[bold cyan]Cluster Code Deployment[/bold cyan]",
                border_style="cyan",
                box=box.DOUBLE,
            )
        )

        # Detect if we're on Windows
        is_windows = sys.platform == "win32"

        config = {
            "Local base": str(self._local_base),
            "Remote base": str(self._remote_base),
            "SSH host": self._ssh_host,
            "Directories": ", ".join(self.directories),
            "Transfer method": "scp (Windows)" if is_windows else "rsync",
            "Clean local cache": "[OK] Always enabled",
        }

        canonical = str(get_canonical_remote_base())
        if str(self._remote_base) != canonical:
            config["Worktree isolation"] = (
                f"[green]active[/green] — owns "
                f"{', '.join(self.directories)}, shares the rest of {canonical}"
            )

        if self.dry_run:
            config["Mode"] = "[yellow]DRY RUN - No changes will be made[/yellow]"

        show_config_panel("Deployment Configuration", config)

    def verify_local_directories(self) -> bool:
        """Verify that local directories exist.

        Returns:
            bool: True if all directories exist, False otherwise
        """
        show_step_header(1, 6, "Verifying Local Directories")

        missing_dirs = []
        for dir_name in self.directories:
            dir_path = self._local_base / dir_name
            if not dir_path.exists():
                missing_dirs.append(str(dir_path))
            else:
                console.print(f"[green][OK][/green] Found {dir_name}/")

        if missing_dirs:
            show_error_panel(
                "Missing local directories",
                "The following directories do not exist:\n"
                + "\n".join(f"  • {d}" for d in missing_dirs),
            )
            return False

        console.print("\n[green][OK][/green] All local directories verified")
        return True

    def show_dry_run_summary(self):
        """Display dry run summary."""
        console.print("\n[yellow]--- DRY RUN SUMMARY ---[/yellow]\n")

        table = Table(
            title="Planned Operations",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Step", style="cyan", width=8)
        table.add_column("Operation", style="white")
        table.add_column("Details", style="dim")

        step = 1
        table.add_row(str(step), "Test Connection", f"SSH to {self._ssh_host}")
        step += 1

        table.add_row(str(step), "Verify Directories", ", ".join(self.directories))
        step += 1

        table.add_row(
            str(step),
            "Clean Local Cache",
            "__pycache__, *.pyc, *.pyo",
        )
        step += 1

        if str(self._remote_base) != str(get_canonical_remote_base()):
            table.add_row(
                str(step),
                "Provision Remote Base",
                f"mkdir {self._remote_base} + symlink shared state",
            )
            step += 1

        table.add_row(
            str(step),
            "Remove Remote Dirs",
            f"{self._remote_base}/{{{','.join(self.directories)}}}",
        )
        step += 1

        table.add_row(
            str(step),
            "Sync Directories",
            " + ".join([f"{d}/" for d in self.directories]),
        )
        step += 1

        table.add_row(
            str(step),
            "Deploy worker.slurm",
            f"{self._remote_base}/.cluster_kit/worker.slurm",
        )
        step += 1

        table.add_row(
            str(step),
            "Provision remote environment",
            "uv sync --frozen (when uv.lock is present)",
        )
        step += 1

        table.add_row(
            str(step),
            "Provision cluster_kit",
            "Mirror local package into remote .venv (uv projects)",
        )
        step += 1

        table.add_row(str(step), "Verify Deployment", "List remote directories")

        console.print(table)

        console.print(
            "\n[cyan]To perform actual deployment, run without --dry-run flag[/cyan]\n"
        )

    def clean_local_cache_step(self) -> int:
        """Clean local Python cache files.

        Returns:
            int: Total number of cache items removed
        """
        show_step_header(2, 6, "Cleaning Local Python Cache")

        dirs_to_clean = [self._local_base / d for d in self.directories]
        stats = PythonCacheCleaner.clean_local(dirs_to_clean, verbose=True)
        return stats["pycache_dirs"] + stats["pyc_files"] + stats["pyo_files"]

    # A worktree deployment owns only what it syncs; everything else at the
    # canonical remote root is symlinked back — conda envs are expensive to
    # rebuild, output must stay in one place for the data mirror, and
    # machine-local files (.env, THIS_IS.py, data/) are provisioned by hand and
    # never synced.  Logs and run state stay per-worktree so it is obvious
    # whose job is whose.
    # Never symlinked from the canonical base into a worktree deployment.
    # pyproject.toml/uv.lock are synced per worktree (a branch may change
    # dependencies), and .venv is built from them, so sharing any of the three
    # would let one worktree's rsync write through a symlink onto the shared
    # deployment.
    UNSHARED = (
        "_logs_",
        ".cluster_kit",
        ".git",
        ".venv",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
    )

    # Transient droppings at the canonical root that should not be mirrored into
    # every worktree deployment.  Without these a real base accumulated 22 junk
    # symlinks against 10 useful ones.
    UNSHARED_GLOBS = ("slurm-*.out", "slurm-*.err", "__pycache__", "*.pyc")

    def provision_remote_base(self) -> bool:
        """Create the worktree deployment directory and its shared symlinks.

        Symlinks every entry of the canonical remote base into the worktree
        base, except the directories this deployer syncs and :attr:`UNSHARED`.
        No-op unless the remote base carries a worktree suffix (see
        :func:`cluster_kit.config._worktree_name`).  Idempotent: existing paths
        are left untouched.

        Returns:
            bool: True if successful, False otherwise
        """
        canonical = str(get_canonical_remote_base())
        remote_base_str = str(self._remote_base)
        if remote_base_str == canonical:
            return True

        console.print(
            f"[cyan]Worktree deployment[/cyan] {remote_base_str}\n"
            f"[dim]owns {', '.join(self.directories)}; "
            f"shares everything else with {canonical}[/dim]"
        )

        # `case` patterns, so the globs work as-is alongside the literal names.
        skip = "|".join((*self.directories, *self.UNSHARED, *self.UNSHARED_GLOBS))
        # `* .[!.]*` covers dotfiles; unmatched globs stay literal, so the
        # -e guard on the source is what filters them out.
        script = (
            f'mkdir -p "{remote_base_str}" && cd "{canonical}" && '
            f'for l in * .[!.]*; do '
            f'case "$l" in {skip}) continue;; esac; '
            f'[ -e "{canonical}/$l" ] || continue; '
            f'[ -e "{remote_base_str}/$l" ] || '
            f'ln -s "{canonical}/$l" "{remote_base_str}/$l"; '
            f"done"
        )

        try:
            result = subprocess.run(
                ["ssh", self._ssh_host, script],
                capture_output=True,
                text=True,
            )
        except Exception as e:
            show_error_panel("Error provisioning worktree remote base", str(e))
            return False

        if result.returncode != 0:
            show_error_panel(
                f"Failed to provision worktree remote base: {remote_base_str}",
                result.stderr,
            )
            return False

        console.print("[green][OK][/green] Worktree remote base ready\n")
        return True

    def remove_remote_directories(self) -> bool:
        """Remove old directories from cluster.

        Returns:
            bool: True if successful, False otherwise
        """
        show_step_header(3, 6, "Removing Remote Directories")

        remote_base_str = str(self._remote_base)

        for dir_name in self.directories:
            console.print(f"[cyan]Removing[/cyan] {remote_base_str}/{dir_name}...")

            try:
                result = subprocess.run(
                    ["ssh", self._ssh_host, f"rm -rf {remote_base_str}/{dir_name}"],
                    capture_output=True,
                    text=True,
                )

                if result.returncode != 0:
                    show_error_panel(
                        f"Failed to remove remote directory: {dir_name}",
                        result.stderr,
                    )
                    return False

                console.print(f"[green][OK][/green] Removed {dir_name}/")

            except Exception as e:
                show_error_panel(f"Error removing {dir_name}", str(e))
                return False

        console.print("\n[green][OK][/green] Remote directories cleaned\n")
        return True

    # Project metadata a uv-based deployment needs in order to resolve its own
    # environment. Hand-placing these is how remote venvs drift out of step
    # with the lockfile they were built from.
    PROJECT_FILES = ("pyproject.toml", "uv.lock", ".python-version")

    def sync_project_files(self) -> bool:
        """Copy project metadata files to the remote base.

        Skips any that do not exist locally. Only files, so this stays outside
        the rsync --delete of :attr:`directories`.

        Returns:
            bool: True if successful, False otherwise
        """
        present = [f for f in self.PROJECT_FILES if (self._local_base / f).is_file()]
        if not present:
            return True

        console.print(f"[cyan]Syncing project files[/cyan] {', '.join(present)}")
        for name in present:
            try:
                result = subprocess.run(
                    [
                        "scp",
                        "-q",
                        str(self._local_base / name),
                        f"{self._ssh_host}:{self._remote_base}/{name}",
                    ],
                    capture_output=True,
                    text=True,
                )
            except Exception as e:
                show_error_panel(f"Error syncing {name}", str(e))
                return False
            if result.returncode != 0:
                show_error_panel(f"Failed to sync {name}", result.stderr)
                return False

        console.print("[green][OK][/green] Project files synced\n")
        return True

    def provision_remote_uv_environment(self) -> bool:
        """Build the remote uv environment when this project has a lockfile.

        ``cluster_kit`` is intentionally excluded because the cluster cannot
        resolve its git dependency; :meth:`provision_remote_cluster_kit`
        mirrors the local installed package after this succeeds.
        """
        if not (self._local_base / "uv.lock").is_file():
            return True

        show_step_header(5, 7, "Provisioning Remote uv Environment")
        command = (
            f"cd {shlex.quote(str(self._remote_base))} && "
            'export PATH="$HOME/.local/bin:$PATH" && '
            "uv sync --frozen --no-install-package cluster-kit"
        )
        try:
            result = subprocess.run(
                ["ssh", self._ssh_host, command], capture_output=True, text=True
            )
        except Exception as e:
            show_error_panel("Error provisioning remote uv environment", str(e))
            return False

        if result.returncode != 0:
            show_error_panel(
                "Failed to provision remote uv environment",
                result.stderr or result.stdout,
            )
            return False

        console.print("[green][OK][/green] Remote uv environment ready\n")
        return True

    def provision_remote_worker(self) -> bool:
        """Deploy cluster-kit's packaged worker.slurm to the remote base.

        Consuming repos do not keep a copy of the worker; it is shipped with
        cluster-kit and refreshed on every sync, so the worker on the cluster
        always matches the cluster-kit that put it there.

        Returns:
            bool: True if successful, False otherwise
        """
        from cluster_kit.launch import get_worker_template

        template = get_worker_template()
        if not template.is_file():
            show_error_panel(
                "Packaged worker.slurm is missing",
                f"Expected it at {template}. This is a cluster-kit packaging "
                "problem, not a project one.",
            )
            return False

        remote_dir = f"{self._remote_base}/.cluster_kit"
        remote_path = f"{remote_dir}/worker.slurm"
        console.print(f"[cyan]Deploying worker[/cyan] -> {remote_path}")

        try:
            mk = subprocess.run(
                ["ssh", self._ssh_host, f'mkdir -p "{remote_dir}"'],
                capture_output=True,
                text=True,
            )
            if mk.returncode != 0:
                show_error_panel("Failed to create remote .cluster_kit", mk.stderr)
                return False

            result = subprocess.run(
                ["scp", "-q", str(template), f"{self._ssh_host}:{remote_path}"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                show_error_panel("Failed to deploy worker.slurm", result.stderr)
                return False

            # sbatch needs it executable.
            subprocess.run(
                ["ssh", self._ssh_host, f'chmod +x "{remote_path}"'],
                capture_output=True,
                text=True,
            )
        except Exception as e:
            show_error_panel("Error deploying worker.slurm", str(e))
            return False

        console.print("[green][OK][/green] Worker deployed\n")
        return True

    def sync_directories(self) -> bool:
        """Sync local directories to cluster using rsync or scp.

        Returns:
            bool: True if successful, False otherwise
        """
        show_step_header(4, 6, "Syncing Directories to Cluster")

        # Use scp on Windows, rsync otherwise
        is_windows = sys.platform == "win32"
        runner = (
            ScpRunner(verbose=self.verbose)
            if is_windows
            else RsyncRunner(
                dry_run=False,  # Already handled by main dry_run logic
                verbose=self.verbose,
                delete=True,
            )
        )

        local_base_str = str(self._local_base)

        for dir_name in self.directories:
            console.print(f"\n[cyan]Syncing[/cyan] {dir_name}/...")

            source = f"{local_base_str}/{dir_name}/"
            if is_windows:
                # scp on Windows (SFTP protocol) requires the destination parent
                # to exist.  Point scp at the remote base and let it create the
                # leaf directory from the source name.
                dest = f"{self._ssh_host}:{self._remote_base}/"
            else:
                dest = f"{self._ssh_host}:{self._remote_base}/{dir_name}/"

            success = runner.sync(source, dest, show_progress=self.verbose)

            if not success:
                show_error_panel(f"Failed to sync {dir_name}", None)
                return False

            console.print(f"[green][OK][/green] Synced {dir_name}/")

        console.print("\n[green][OK][/green] All directories synced\n")
        return True

    def provision_remote_cluster_kit(self) -> bool:
        """Mirror the locally-installed cluster_kit package into the remote uv venv.

        The cluster has no git, so ``uv sync`` cannot resolve the git-based
        cluster-kit dependency there and remote venvs are built with
        ``--no-install-package cluster-kit``. Project code imports cluster_kit
        at runtime (email notifications), so every deploy copies the local
        installed package — version-matched to the consuming repo's uv.lock —
        straight into the remote venv's site-packages. Projects without a
        remote ``.venv`` (conda-flow, e.g. whales) are skipped.

        Returns:
            bool: True if provisioning succeeded or was skipped, False on error
        """
        show_step_header(6, 7, "Provisioning cluster-kit in Remote venv")

        remote_base_str = str(self._remote_base)
        probe = (
            f"ls -d {remote_base_str}/.venv/lib/python*/site-packages "
            "2>/dev/null | head -1"
        )
        try:
            result = subprocess.run(
                ["ssh", self._ssh_host, probe],
                capture_output=True,
                text=True,
            )
        except Exception as e:
            show_error_panel("Error probing remote venv", str(e))
            return False

        remote_site = result.stdout.strip()
        if result.returncode != 0 or not remote_site:
            console.print(
                "[dim]No remote .venv found - skipping (conda-flow project)[/dim]\n"
            )
            return True

        import cluster_kit as _cluster_kit_pkg

        pkg_dir = Path(_cluster_kit_pkg.__file__).resolve().parent

        # Drop any stale copy (package dir and wheel/pip dist-info of any
        # version) so remote metadata never lies about what is installed.
        cleanup = (
            f"rm -rf {remote_site}/cluster_kit "
            f"{remote_site}/cluster_kit-*.dist-info"
        )
        result = subprocess.run(
            ["ssh", self._ssh_host, cleanup], capture_output=True, text=True
        )
        if result.returncode != 0:
            show_error_panel("Failed to clean stale remote cluster_kit", result.stderr)
            return False

        is_windows = sys.platform == "win32"
        runner = (
            ScpRunner(verbose=self.verbose)
            if is_windows
            else RsyncRunner(dry_run=False, verbose=self.verbose, delete=True)
        )
        # No trailing slash on source: both rsync and scp then create the
        # cluster_kit/ leaf under site-packages.
        success = runner.sync(
            str(pkg_dir),
            f"{self._ssh_host}:{remote_site}/",
            show_progress=self.verbose,
        )
        if not success:
            show_error_panel("Failed to sync cluster_kit into remote venv", None)
            return False

        console.print(
            f"[green][OK][/green] cluster_kit mirrored into {remote_site}\n"
        )
        return True

    def verify_deployment(self) -> bool:
        """Verify deployment by listing remote directories.

        Returns:
            bool: True if verification successful, False otherwise
        """
        show_step_header(7, 7, "Verifying Deployment")

        remote_base_str = str(self._remote_base)

        for dir_name in self.directories:
            console.print(f"\n[yellow]--- {remote_base_str}/{dir_name} ---[/yellow]")

            try:
                result = subprocess.run(
                    [
                        "ssh",
                        self._ssh_host,
                        f"ls -la {remote_base_str}/{dir_name} 2>/dev/null | head -5",
                    ],
                    capture_output=True,
                    text=True,
                )

                if result.returncode == 0:
                    console.print(result.stdout)
                else:
                    console.print("[red]Directory not found or empty[/red]")

            except Exception as e:
                console.print(
                    f"[yellow]Warning: Could not list {dir_name}: {e}[/yellow]"
                )

        console.print("[green][OK][/green] Verification complete\n")
        return True

    def deploy(self) -> bool:
        """Execute full deployment workflow.

        Returns:
            bool: True if deployment successful, False otherwise
        """
        # Show configuration
        self.show_configuration()

        # Handle dry run
        if self.dry_run:
            self.show_dry_run_summary()
            return True

        try:
            with RemoteDeployLock(
                host=self._ssh_host,
                remote_base=self._remote_base,
                purpose="code deploy",
            ):
                return self._deploy_locked()
        except RemoteDeployLockError as exc:
            show_error_panel("Failed to acquire deployment lock", str(exc))
            return False

    def _deploy_locked(self) -> bool:
        """Execute deployment after the remote-base deploy lock is held."""
        # Step 1: Test connection
        show_step_header(1, 6, "Testing Cluster Connection")
        if not ClusterConnection.test_connection(verbose=True):
            return False

        # Step 2: Verify local directories
        if not self.verify_local_directories():
            return False

        # Step 3: Clean local cache (always done)
        self.clean_local_cache_step()

        # Step 4: Create the worktree remote base + shared symlinks (no-op
        # outside a linked worktree)
        if not self.provision_remote_base():
            return False

        # Step 5: Remove remote directories (cache files are deleted along
        # with everything else, so a separate remote cache clean is unnecessary)
        if not self.remove_remote_directories():
            return False

        # Step 5: Sync directories (rsync excludes __pycache__/*.pyc/*.pyo)
        if not self.sync_directories():
            return False

        # Step 6: Project metadata (pyproject.toml / uv.lock) for uv deployments
        if not self.sync_project_files():
            return False

        # Step 6: Build the worktree-specific uv environment from its lockfile.
        # Conda-flow projects have no uv.lock and retain their existing path.
        if not self.provision_remote_uv_environment():
            return False

        # Step 7: Deploy cluster-kit's own worker.slurm
        if not self.provision_remote_worker():
            return False

        # Step 8: Mirror cluster_kit into the remote uv venv. The environment
        # step above guarantees uv projects cannot silently use shared Conda.
        if not self.provision_remote_cluster_kit():
            return False

        # Step 9: Verify deployment
        if not self.verify_deployment():
            return False

        # Success!
        show_success_panel(
            "Deployment completed successfully!",
            {
                "Status": "Cluster is now synced with local code",
                "Cache": "Local Python cache files have been removed",
            },
        )

        return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_code(
    dry_run: bool = False,
    verbose: bool = False,
    directories: Optional[list[str]] = None,
) -> bool:
    """Sync code directories to the cluster.

    This is the programmatic entry point for code synchronization.

    Args:
        dry_run: If True, preview actions without executing
        verbose: If True, show detailed output
        directories: List of directory names to sync (default: ["src", "runnables"])

    Returns:
        bool: True if deployment successful, False otherwise

    Example:
        >>> from cluster_kit.sync.code import sync_code
        >>> sync_code(dry_run=True)
        >>> sync_code(verbose=True, directories=["src", "scripts"])
    """
    deployer = CodeDeployer(
        dry_run=dry_run,
        verbose=verbose,
        directories=directories,
    )
    return deployer.deploy()


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def main():
    """Main entry point for CLI usage."""
    parser = argparse.ArgumentParser(
        description="Deploy local code directories to cluster",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cluster-kit sync code              # Deploy code to cluster (always cleans cache)
  cluster-kit sync code --dry-run    # Preview what would be deployed
  cluster-kit sync code --verbose    # Show detailed rsync output

Requirements:
  • VPN connection active
  • SSH key configured
  • Local directories exist

Note:
  Local Python cache files (__pycache__, *.pyc, *.pyo) are cleaned before
  syncing. Remote cache is not cleaned separately because the remote
  directories are fully removed and re-synced with cache patterns excluded.
        """,
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed rsync output",
    )
    parser.add_argument(
        "--directories",
        nargs="+",
        default=None,
        help="Directory names to sync (default: src runnables)",
    )

    args = parser.parse_args()

    # Create deployer and execute
    deployer = CodeDeployer(
        dry_run=args.dry_run,
        verbose=args.verbose,
        directories=args.directories,
    )

    success = deployer.deploy()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
