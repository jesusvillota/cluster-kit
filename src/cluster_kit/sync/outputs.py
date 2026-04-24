"""Output synchronization from cluster.

Downloads cluster-generated outputs (visualizations, processed data) to the
local output/ directory via rsync. Supports filtering by mode (visualization/
processed/all) and by file format (pdf, png, csv, etc.).

Example:
    >>> from cluster_kit.sync.outputs import OutputSyncer
    >>> syncer = OutputSyncer(mode="visualization", formats=["pdf", "png"])
    >>> syncer.sync()

CLI:
    $ cluster-kit sync outputs --dry-run
    $ cluster-kit sync outputs --all --formats pdf,png
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

from cluster_kit.config import get_cluster_host, get_cluster_user, get_remote_base
from cluster_kit.utils import (
    ClusterConnection,
    RsyncRunner,
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
    return current


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_FORMATS = {
    "pdf",
    "png",
    "tex",
    "csv",
    "json",
    "parquet",
    "yaml",
    "all",
}


# ---------------------------------------------------------------------------
# OutputSyncer Class
# ---------------------------------------------------------------------------


class OutputSyncer:
    """Handle syncing of cluster outputs to local machine."""

    def __init__(
        self,
        mode: str = "visualization",
        formats: Optional[list[str]] = None,
        dry_run: bool = False,
        delete: bool = False,
        verbose: bool = False,
        show_tree: bool = False,
    ):
        """Initialize OutputSyncer.

        Args:
            mode: Sync mode ('all', 'visualization', or 'processed')
            formats: List of file formats to sync (None = all formats)
            dry_run: If True, preview actions without executing
            delete: If True, delete local files not present on cluster
            verbose: If True, show detailed output
            show_tree: If True, display directory tree after sync
        """
        self.mode = mode
        self.formats = formats
        self.dry_run = dry_run
        self.delete = delete
        self.verbose = verbose
        self.show_tree = show_tree

        self._local_base = _find_project_root()
        self._remote_base = get_remote_base()
        self._cluster_host = get_cluster_host()
        self._cluster_user = get_cluster_user()

        # Ensure local output directory exists
        self.local_output_dir = self._local_base / "output"
        self.local_output_dir.mkdir(parents=True, exist_ok=True)

    def show_configuration(self):
        """Display sync configuration."""
        console.print(
            Panel(
                "[bold cyan]Cluster Output Sync[/bold cyan]",
                border_style="cyan",
                box=box.DOUBLE,
            )
        )

        config = {
            "Cluster": f"{self._cluster_user}@{self._cluster_host}",
            "Remote path": f"{self._remote_base}/output/",
            "Local path": str(self.local_output_dir),
            "Sync mode": self._get_mode_description(),
            "File formats": self._get_formats_description(),
        }

        if self.dry_run:
            config["Mode"] = "[yellow]DRY RUN - No files will be transferred[/yellow]"

        if self.delete:
            config["Delete"] = (
                "[yellow][WARN] DELETE MODE - Local files not on cluster"
                " will be removed[/yellow]"
            )

        show_config_panel("Sync Configuration", config)

    def _get_mode_description(self) -> str:
        """Get human-readable mode description."""
        mode_descriptions = {
            "all": "[cyan]All outputs[/cyan] (visualization + processed + legacy)",
            "visualization": (
                "[cyan]Visualization outputs only[/cyan] (plots, tables, figures)"
            ),
            "processed": (
                "[cyan]Processed data only[/cyan] (intermediate analysis results)"
            ),
        }
        return mode_descriptions.get(self.mode, self.mode)

    def _get_formats_description(self) -> str:
        """Get human-readable formats description."""
        if not self.formats or "all" in self.formats:
            return "[cyan]All formats[/cyan]"
        return f"[cyan]{', '.join(sorted(self.formats))}[/cyan]"

    def _get_rsync_options(self) -> tuple[Optional[list], Optional[list]]:
        """Get rsync include/exclude patterns based on sync mode and formats.

        Returns:
            Tuple of (includes, excludes) lists
        """
        includes = []
        excludes = []

        # Step 1: Apply directory filtering based on mode
        if self.mode == "visualization":
            includes.append("visualization/***")
            excludes.append("*")
        elif self.mode == "processed":
            includes.append("processed/***")
            excludes.append("*")
        # For mode == "all", no directory filtering

        # Step 2: Apply format filtering
        if self.formats and "all" not in self.formats:
            if includes:
                # We already have directory filters, need to combine them
                # Strategy: include directory/** AND format patterns
                # The directory/*** pattern already covers all files in that directory
                # We need to be more specific
                new_includes = []
                for dir_pattern in includes:
                    if dir_pattern.endswith("/***"):
                        # Remove the *** and add specific format patterns
                        dir_prefix = dir_pattern[:-3]  # Remove "***"
                        for fmt in self.formats:
                            new_includes.append(f"{dir_prefix}**/*.{fmt}")
                    else:
                        new_includes.append(dir_pattern)
                includes = new_includes
            else:
                # No directory filter, just add format includes
                for fmt in self.formats:
                    includes.append(f"**/*.{fmt}")

            # Add exclusion for all other formats
            if "*" not in excludes:
                excludes.append("*")

        # If no filters, return None to sync everything
        if not includes and not excludes:
            return None, None

        # If only excludes without includes, return None (invalid state)
        if excludes and not includes:
            return None, None

        return includes if includes else None, excludes if excludes else None

    def show_dry_run_summary(self):
        """Display dry run summary."""
        console.print("\n[yellow]--- DRY RUN SUMMARY ---[/yellow]\n")

        includes, excludes = self._get_rsync_options()

        # Build rsync preview
        rsync = RsyncRunner(dry_run=True, verbose=self.verbose, delete=self.delete)
        source = (
            f"{self._cluster_user}@{self._cluster_host}:{self._remote_base}/output/"
        )
        dest = f"{self.local_output_dir}/"

        cmd_preview = rsync.build_command_preview(source, dest, includes, excludes)

        console.print("[cyan]Would execute:[/cyan]")
        console.print(f"[dim]{cmd_preview}[/dim]\n")

        # Show patterns
        if includes or excludes:
            table = Table(
                title="Filter Patterns",
                box=box.ROUNDED,
                show_header=True,
                header_style="bold cyan",
            )
            table.add_column("Type", style="cyan", width=12)
            table.add_column("Patterns", style="white")

            if includes:
                table.add_row("Include", "\n".join(includes))
            if excludes:
                table.add_row("Exclude", "\n".join(excludes))

            console.print(table)

        console.print(
            "\n[cyan]To perform actual sync, run without --dry-run flag[/cyan]\n"
        )

    def sync_outputs(self) -> bool:
        """Execute rsync to sync outputs from cluster.

        Returns:
            bool: True if sync successful, False otherwise
        """
        show_step_header(1, 2, "Syncing Outputs from Cluster")

        console.print(f"[cyan]Syncing {self.mode} outputs...[/cyan]\n")

        # Get rsync options based on mode
        includes, excludes = self._get_rsync_options()

        # Create rsync runner
        rsync = RsyncRunner(
            dry_run=False,  # Handled by main dry_run logic
            verbose=self.verbose,
            delete=self.delete,
        )

        # Build source and destination paths
        source = (
            f"{self._cluster_user}@{self._cluster_host}:{self._remote_base}/output/"
        )
        dest = f"{self.local_output_dir}/"

        # Execute sync
        success = rsync.sync(
            source, dest, includes, excludes, show_progress=self.verbose
        )

        if not success:
            show_error_panel("Sync failed", "Check the error messages above")
            return False

        console.print("\n[green][OK][/green] Sync completed successfully\n")
        return True

    def show_directory_tree(self):
        """Display directory tree of local output directory."""
        show_step_header(2, 2, "Output Directory Structure")

        # Try using tree command first
        try:
            result = subprocess.run(
                ["tree", "-L", "3", "-d", str(self.local_output_dir)],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                console.print(result.stdout)
            else:
                # Fallback to find command
                self._show_directory_tree_fallback()

        except FileNotFoundError:
            # tree command not available, use find
            self._show_directory_tree_fallback()
        except Exception:
            console.print("[yellow]Could not display directory tree[/yellow]")

    def _show_directory_tree_fallback(self):
        """Fallback method to display directory structure using find."""
        try:
            result = subprocess.run(
                [
                    "find",
                    str(self.local_output_dir),
                    "-type",
                    "d",
                    "-maxdepth",
                    "3",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode == 0:
                dirs = sorted(result.stdout.strip().split("\n"))
                for directory in dirs:
                    # Calculate depth and indent
                    rel_path = Path(directory).relative_to(self.local_output_dir)
                    depth = len(rel_path.parts)
                    indent = "  " * depth
                    name = rel_path.name if depth > 0 else "output/"
                    console.print(f"{indent}{name}")
            else:
                console.print("[yellow]Could not list directories[/yellow]")

        except Exception as e:
            console.print(f"[yellow]Error displaying tree: {e}[/yellow]")

    def show_latex_usage_hints(self):
        """Display LaTeX usage examples."""
        console.print("\n[cyan]--- LaTeX Usage Examples ---[/cyan]\n")

        examples = [
            (
                "Include Figure",
                r"\includegraphics[width=0.8\textwidth]{../../output/visualization/whale_def/figure.pdf}",
            ),
            (
                "Include Table",
                r"\input{../../output/visualization/whale_def/table.tex}",
            ),
            (
                "Reference Path",
                "Use relative paths from your LaTeX document to output/",
            ),
        ]

        for title, example in examples:
            console.print(f"[green]•[/green] [bold]{title}:[/bold]")
            console.print(f"  [dim]{example}[/dim]\n")

    def sync(self) -> bool:
        """Execute full sync workflow.

        Returns:
            bool: True if sync successful, False otherwise
        """
        # Show configuration
        self.show_configuration()

        # Handle dry run
        if self.dry_run:
            self.show_dry_run_summary()
            return True

        # Test connection
        if not ClusterConnection.test_connection(verbose=True):
            return False

        # Sync outputs
        if not self.sync_outputs():
            return False

        # Show directory tree if requested
        if self.show_tree:
            self.show_directory_tree()

        # Show LaTeX usage hints
        self.show_latex_usage_hints()

        # Success!
        show_success_panel(
            "Outputs synced successfully!",
            {
                "Mode": self.mode,
                "Location": str(self.local_output_dir),
            },
        )

        return True


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def parse_formats(formats_str: str) -> list[str]:
    """Parse and validate comma-separated format string.

    Args:
        formats_str: Comma-separated list of formats (e.g., "pdf,png")

    Returns:
        List of validated format strings

    Raises:
        ValueError: If any format is not supported
    """
    if not formats_str:
        return []

    # Parse comma-separated formats
    formats = [fmt.strip().lower() for fmt in formats_str.split(",")]

    # Validate formats
    invalid_formats = [fmt for fmt in formats if fmt not in SUPPORTED_FORMATS]
    if invalid_formats:
        raise ValueError(
            f"Unsupported format(s): {', '.join(invalid_formats)}\n"
            f"Supported formats: {', '.join(sorted(SUPPORTED_FORMATS))}"
        )

    # If "all" is specified, return it alone
    if "all" in formats:
        return ["all"]

    return formats


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_outputs(
    mode: str = "visualization",
    formats: Optional[list[str]] = None,
    dry_run: bool = False,
    delete: bool = False,
    verbose: bool = False,
    show_tree: bool = False,
) -> bool:
    """Sync output files from the cluster to local machine.

    This is the programmatic entry point for output synchronization.

    Args:
        mode: Sync mode ('all', 'visualization', or 'processed')
        formats: List of file formats to sync (e.g., ["pdf", "png"])
        dry_run: If True, preview actions without executing
        delete: If True, delete local files not present on cluster
        verbose: If True, show detailed output
        show_tree: If True, display directory tree after sync

    Returns:
        bool: True if sync successful, False otherwise

    Example:
        >>> from cluster_kit.sync.outputs import sync_outputs
        >>> sync_outputs(mode="visualization", formats=["pdf", "png"])
        >>> sync_outputs(dry_run=True)
    """
    syncer = OutputSyncer(
        mode=mode,
        formats=formats,
        dry_run=dry_run,
        delete=delete,
        verbose=verbose,
        show_tree=show_tree,
    )
    return syncer.sync()


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def main():
    """Main entry point for CLI usage."""
    parser = argparse.ArgumentParser(
        description="Sync cluster-generated outputs to local output/ directory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cluster-kit sync outputs                       # Sync visualization outputs (default)
  cluster-kit sync outputs --all                 # Sync all outputs
  cluster-kit sync outputs --processed           # Sync only processed data
  cluster-kit sync outputs --formats pdf         # Sync only PDF files
  cluster-kit sync outputs --formats pdf,png   # Sync only PDF and PNG files
  cluster-kit sync outputs --dry-run             # Preview what would be synced
  cluster-kit sync outputs --verbose             # Show detailed rsync output
  cluster-kit sync outputs --show-tree           # Show directory tree after sync

Sync Modes:
  --visualization    PDF/PNG plots, LaTeX tables, figures (default)
  --processed        Intermediate CSV/JSON/Parquet analysis results
  --all              Everything in output/ directory

File Formats:
  --formats pdf,png  Filter by extension (pdf, png, tex, csv, json, parquet, yaml, all)

Requirements:
  • VPN connection active
  • SSH key configured
  • Cluster outputs exist in remote output/ directory
        """,
    )

    # Sync mode (mutually exclusive)
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--all",
        action="store_const",
        const="all",
        dest="mode",
        help="Sync all outputs",
    )
    mode_group.add_argument(
        "--visualization",
        action="store_const",
        const="visualization",
        dest="mode",
        help="Sync only visualization outputs (default)",
    )
    mode_group.add_argument(
        "--processed",
        action="store_const",
        const="processed",
        dest="mode",
        help="Sync only processed outputs",
    )

    # Options
    parser.add_argument(
        "--formats",
        type=str,
        default=None,
        help="Comma-separated list of file formats to sync (e.g., pdf,png,tex). "
        "Supported: pdf, png, tex, csv, json, parquet, yaml, all. Default: all formats",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without actually syncing",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete local files not present on cluster (use with caution!)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed rsync output",
    )
    parser.add_argument(
        "--show-tree",
        action="store_true",
        help="Display directory tree after sync",
    )

    args = parser.parse_args()

    # Default mode is visualization
    mode = args.mode if args.mode else "visualization"

    # Parse and validate formats
    formats = None
    if args.formats:
        try:
            formats = parse_formats(args.formats)
        except ValueError as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    # Create syncer and execute
    syncer = OutputSyncer(
        mode=mode,
        formats=formats,
        dry_run=args.dry_run,
        delete=args.delete,
        verbose=args.verbose,
        show_tree=args.show_tree,
    )

    success = syncer.sync()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
