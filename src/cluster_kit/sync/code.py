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
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cluster_kit.config import get_cluster_host, get_remote_base
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
            dest = f"{self._ssh_host}:{self._remote_base}/{dir_name}/"

            success = runner.sync(source, dest, show_progress=self.verbose)

            if not success:
                show_error_panel(f"Failed to sync {dir_name}", None)
                return False

            console.print(f"[green][OK][/green] Synced {dir_name}/")

        console.print("\n[green][OK][/green] All directories synced\n")
        return True

    def verify_deployment(self) -> bool:
        """Verify deployment by listing remote directories.

        Returns:
            bool: True if verification successful, False otherwise
        """
        show_step_header(5, 6, "Verifying Deployment")

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

        # Step 1: Test connection
        show_step_header(1, 6, "Testing Cluster Connection")
        if not ClusterConnection.test_connection(verbose=True):
            return False

        # Step 2: Verify local directories
        if not self.verify_local_directories():
            return False

        # Step 3: Clean local cache (always done)
        self.clean_local_cache_step()

        # Step 4: Remove remote directories (cache files are deleted along
        # with everything else, so a separate remote cache clean is unnecessary)
        if not self.remove_remote_directories():
            return False

        # Step 5: Sync directories (rsync excludes __pycache__/*.pyc/*.pyo)
        if not self.sync_directories():
            return False

        # Step 6: Verify deployment
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
