"""File transfer module for copying files between local and cluster.

Provides bidirectional file/folder copying with automatic direction detection
based on path format (cluster:path vs local path).

Example:
    >>> from cluster_kit.sync.transfer import FileTransfer
    >>> transfer = FileTransfer(dry_run=True, verbose=True)
    >>> transfer.copy("local/file.txt", "cluster:/remote/path/")
    >>> transfer.copy("cluster:/remote/file.txt", "local/path/")

CLI:
    $ cluster-kit sync cp local/file.txt cluster:/remote/path/
    $ cluster-kit sync cp -r local/folder/ cluster:/remote/
    $ cluster-kit sync cp cluster:/remote/file.txt local/path/ --dry-run
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
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
    ScpRunner,
    show_error_panel,
    show_step_header,
    show_success_panel,
)

# ---------------------------------------------------------------------------
# Console Utility
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


# ---------------------------------------------------------------------------
# Path Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedPath:
    """Parsed path with optional host information.

    Attributes:
        host: Remote host identifier (e.g., "cluster") or None for local paths
        path: The actual path (local or remote)
        is_remote: True if this is a remote path (host is not None)
    """

    host: Optional[str]
    path: str
    is_remote: bool

    @property
    def full_path(self) -> str:
        """Return the full path string for rsync/scp.

        For remote paths: "host:path"
        For local paths: just the path
        """
        if self.host:
            return f"{self.host}:{self.path}"
        return self.path


def parse_path(path_str: str, default_host: Optional[str] = None) -> ParsedPath:
    """Parse a path string into host and path components.

    Path format examples:
        - "cluster:/remote/path" → host="cluster", path="/remote/path"
        - "/local/path" → host=None, path="/local/path"
        - "user@cluster:/path" → host="user@cluster", path="/path"
        - "relative/path" → host=None, path="relative/path" (relative local)

    Args:
        path_str: The path string to parse
        default_host: Optional default host to use if not specified in path

    Returns:
        ParsedPath with host, path, and is_remote flag
    """
    # Check for remote path format (contains ":" before any path separator)
    # This handles:
    #   - "cluster:/path" → host="cluster", path="/path"
    #   - "user@cluster:/path" → host="user@cluster", path="/path"
    #   - "/local/path:with:colons" → local (absolute path starts with /)
    #   - "./local:path" → local (relative path with colon)

    if ":" in path_str:
        # Split on first colon
        parts = path_str.split(":", 1)
        potential_host = parts[0]
        potential_path = parts[1]

        # Check if it looks like a remote path
        # Remote: host contains only valid hostname chars, path starts with /
        # Local absolute: starts with / but no valid host pattern before it
        if potential_path.startswith("/") and _is_valid_host(potential_host):
            return ParsedPath(
                host=potential_host,
                path=potential_path,
                is_remote=True,
            )

    # Local path (no valid remote format detected)
    # Expand ~ to home directory for local paths
    expanded_path = Path(path_str).expanduser()
    return ParsedPath(
        host=None,
        path=str(expanded_path),
        is_remote=False,
    )


def _is_valid_host(host_str: str) -> bool:
    """Check if a string looks like a valid host identifier.

    Valid hosts:
        - "cluster"
        - "user@cluster"
        - "192.168.1.1" (IP addresses)
        - "host-name" (with hyphens)
        - "host_name" (with underscores)

    Invalid:
        - Empty string
        - Starts with / (would be local path)
        - Starts with . (would be relative path)
        - Contains invalid characters

    Args:
        host_str: String to validate

    Returns:
        True if valid host identifier
    """
    if not host_str or len(host_str) < 1:
        return False

    # Must not start with path indicators
    if host_str.startswith(("/", ".", "~")):
        return False

    # Must not contain path separators (would be ambiguous)
    if "/" in host_str:
        return False

    # Valid hostname chars: alphanumerics, hyphens, underscores, @ for user@host
    # IPv4 addresses: digits and dots
    valid_chars = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.@"
    )

    return all(c in valid_chars for c in host_str)


# ---------------------------------------------------------------------------
# Direction Detection
# ---------------------------------------------------------------------------


class TransferDirection:
    """Transfer direction constants."""

    LOCAL_TO_CLUSTER = "local_to_cluster"
    CLUSTER_TO_LOCAL = "cluster_to_local"
    LOCAL_TO_LOCAL = "local_to_local"


def detect_direction(src: ParsedPath, dst: ParsedPath) -> str:
    """Detect transfer direction based on parsed paths.

    Args:
        src: Parsed source path
        dst: Parsed destination path

    Returns:
        One of TransferDirection constants

    Raises:
        ValueError: If direction is ambiguous or unsupported
    """
    if src.is_remote and dst.is_remote:
        raise ValueError(
            "Cannot transfer between two remote hosts. At least one path must be local."
        )

    if src.is_remote and not dst.is_remote:
        return TransferDirection.CLUSTER_TO_LOCAL

    if not src.is_remote and dst.is_remote:
        return TransferDirection.LOCAL_TO_CLUSTER

    # Both are local
    return TransferDirection.LOCAL_TO_LOCAL


# ---------------------------------------------------------------------------
# FileTransfer Class
# ---------------------------------------------------------------------------


class FileTransfer:
    """Handle file transfers between local and cluster.

    Supports bidirectional copying with automatic direction detection,
    progress display, dry-run preview, and verbose output.
    """

    def __init__(
        self,
        dry_run: bool = False,
        verbose: bool = False,
        recursive: bool = False,
    ):
        """Initialize FileTransfer.

        Args:
            dry_run: If True, preview actions without executing
            verbose: If True, show detailed rsync output
            recursive: If True, copy directories recursively
        """
        self.dry_run = dry_run
        self.verbose = verbose
        self.recursive = recursive

        # Load config defaults
        self._default_host = get_cluster_host()
        self._cluster_user = get_cluster_user()
        self._remote_base = get_remote_base()

    def _get_runner(self) -> RsyncRunner | ScpRunner:
        """Get appropriate runner based on platform.

        Uses RsyncRunner on Unix-like systems, ScpRunner on Windows.
        """
        is_windows = sys.platform == "win32"

        if is_windows:
            return ScpRunner(dry_run=self.dry_run, verbose=self.verbose)
        else:
            return RsyncRunner(
                dry_run=self.dry_run,
                verbose=self.verbose,
                delete=False,
            )

    def copy(
        self,
        src: str,
        dst: str,
        recursive: Optional[bool] = None,
        dry_run: Optional[bool] = None,
        verbose: Optional[bool] = None,
    ) -> bool:
        """Copy a file or directory from source to destination.

        Automatically detects direction based on path format:
            - "cluster:/path" → cluster to local or vice versa
            - "/local/path" → local path

        Args:
            src: Source path (local or cluster:path format)
            dst: Destination path (local or cluster:path format)
            recursive: Override recursive setting (default: use instance setting)
            dry_run: Override dry_run setting (default: use instance setting)
            verbose: Override verbose setting (default: use instance setting)

        Returns:
            True if transfer successful, False otherwise

        Raises:
            ValueError: If paths are invalid or direction is ambiguous
        """
        # Use instance settings unless overridden
        use_recursive = self.recursive if recursive is None else recursive
        use_dry_run = self.dry_run if dry_run is None else dry_run
        use_verbose = self.verbose if verbose is None else verbose

        # Parse paths
        src_parsed = parse_path(src, default_host=self._default_host)
        dst_parsed = parse_path(dst, default_host=self._default_host)

        # Detect direction
        try:
            direction = detect_direction(src_parsed, dst_parsed)
        except ValueError as e:
            show_error_panel("Invalid transfer configuration", str(e))
            return False

        # Handle local-to-local (we allow it but show a warning)
        if direction == TransferDirection.LOCAL_TO_LOCAL:
            console.print(
                "[yellow]Note: Both paths are local. Using local copy.[/yellow]"
            )
            return self._copy_local(src_parsed.path, dst_parsed.path, use_recursive)

        # For remote transfers, test connection first
        if not use_dry_run:
            if not ClusterConnection.test_connection(verbose=use_verbose):
                return False

        # Show transfer configuration
        self._show_transfer_config(src_parsed, dst_parsed, direction, use_recursive)

        # Perform the transfer
        return self._transfer(
            src_parsed, dst_parsed, direction, use_recursive, use_dry_run, use_verbose
        )

    def _show_transfer_config(
        self,
        src: ParsedPath,
        dst: ParsedPath,
        direction: str,
        recursive: bool,
    ):
        """Display transfer configuration."""
        console.print(
            Panel(
                "[bold cyan]File Transfer[/bold cyan]",
                border_style="cyan",
                box=box.DOUBLE,
            )
        )

        # Build direction description
        if direction == TransferDirection.LOCAL_TO_CLUSTER:
            direction_desc = "[cyan]Local → Cluster[/cyan]"
        elif direction == TransferDirection.CLUSTER_TO_LOCAL:
            direction_desc = "[cyan]Cluster → Local[/cyan]"
        else:
            direction_desc = "[cyan]Local → Local[/cyan]"

        config = {
            "Source": src.full_path,
            "Destination": dst.full_path,
            "Direction": direction_desc,
            "Recursive": "[green]Yes[/green]" if recursive else "[dim]No[/dim]",
        }

        if self.dry_run:
            config["Mode"] = "[yellow]DRY RUN - No files will be transferred[/yellow]"

        table = Table(box=box.ROUNDED, show_header=False)
        table.add_column("Setting", style="cyan", width=15)
        table.add_column("Value", style="white")

        for key, value in config.items():
            table.add_row(key, value)

        console.print(table)
        console.print()

    def _transfer(
        self,
        src: ParsedPath,
        dst: ParsedPath,
        direction: str,
        recursive: bool,
        dry_run: bool,
        verbose: bool,
    ) -> bool:
        """Execute the actual file transfer.

        Args:
            src: Parsed source path
            dst: Parsed destination path
            direction: Transfer direction constant
            recursive: Whether to copy recursively
            dry_run: Whether to preview only
            verbose: Whether to show detailed output

        Returns:
            True if successful, False otherwise
        """
        runner = self._get_runner()

        # Prepare source and destination for rsync
        if recursive and not src.path.endswith(("/", "\\")):
            # For recursive copy, ensure source ends with / for rsync -a behavior
            # This copies the directory contents rather than the directory itself
            source_path = src.path + "/"
        else:
            source_path = src.path

        if src.is_remote:
            source_full = f"{src.host}:{source_path}"
        else:
            source_full = source_path

        if dst.is_remote:
            dest_full = f"{dst.host}:{dst.path}"
        else:
            # Ensure destination directory exists for local paths
            dst_path = Path(dst.path)
            if not dst_path.parent.exists():
                dst_path.parent.mkdir(parents=True, exist_ok=True)
            dest_full = dst.path

        # Handle dry run
        if dry_run:
            console.print("[yellow]--- DRY RUN SUMMARY ---[/yellow]\n")

            cmd_preview = runner.build_command_preview(source_full, dest_full)
            console.print("[cyan]Would execute:[/cyan]")
            console.print(f"[dim]{cmd_preview}[/dim]\n")

            console.print(
                "[cyan]To perform actual transfer, run without --dry-run flag[/cyan]\n"
            )
            return True

        # Execute transfer
        show_step_header(1, 1, "Transferring Files")

        console.print(f"[cyan]Source:[/cyan] {source_full}")
        console.print(f"[cyan]Destination:[/cyan] {dest_full}\n")

        # Add recursive flag for directories
        if recursive:
            # For rsync, directories should end with / to copy contents
            # For scp, -r flag handles recursion
            pass

        success = runner.sync(
            source_full,
            dest_full,
            show_progress=verbose,
        )

        if not success:
            show_error_panel("Transfer failed", "Check the error messages above")
            return False

        # Show success
        show_success_panel(
            "Transfer completed successfully!",
            {
                "Source": src.full_path,
                "Destination": dst.full_path,
                "Direction": (
                    "Local → Cluster"
                    if direction == TransferDirection.LOCAL_TO_CLUSTER
                    else "Cluster → Local"
                ),
            },
        )

        return True

    def _copy_local(self, src: str, dst: str, recursive: bool) -> bool:
        """Copy files locally using shutil.

        Args:
            src: Source path
            dst: Destination path
            recursive: Whether to copy directories recursively

        Returns:
            True if successful, False otherwise
        """
        import shutil

        src_path = Path(src)
        dst_path = Path(dst)

        if not src_path.exists():
            show_error_panel(f"Source does not exist: {src}")
            return False

        try:
            if src_path.is_dir():
                if recursive:
                    if dst_path.exists():
                        shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
                    else:
                        shutil.copytree(src_path, dst_path)
                else:
                    show_error_panel(
                        "Source is a directory",
                        f"Use --recursive flag to copy directories: {src}",
                    )
                    return False
            else:
                # Copy file
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dst_path)

            show_success_panel(
                "Local copy completed successfully!",
                {
                    "Source": str(src_path),
                    "Destination": str(dst_path),
                },
            )
            return True

        except Exception as e:
            show_error_panel(f"Local copy failed: {e}")
            return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def copy_file(
    src: str,
    dst: str,
    recursive: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> bool:
    """Copy a file between local and cluster.

    This is the programmatic entry point for file transfer.

    Args:
        src: Source path (local or cluster:path format)
        dst: Destination path (local or cluster:path format)
        recursive: If True, copy directories recursively
        dry_run: If True, preview actions without executing
        verbose: If True, show detailed output

    Returns:
        bool: True if transfer successful, False otherwise

    Example:
        >>> from cluster_kit.sync.transfer import copy_file
        >>> copy_file("local/file.txt", "cluster:/remote/path/")
        >>> copy_file("cluster:/remote/file.txt", "local/path/", dry_run=True)
        >>> copy_file("local/folder/", "cluster:/remote/", recursive=True)
    """
    transfer = FileTransfer(
        dry_run=dry_run,
        verbose=verbose,
        recursive=recursive,
    )
    return transfer.copy(src, dst)


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------


def main():
    """Main entry point for CLI usage."""
    parser = argparse.ArgumentParser(
        description="Copy files between local machine and cluster",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  cluster-kit sync cp local/file.txt cluster:/remote/path/
  cluster-kit sync cp cluster:/remote/file.txt local/path/
  cluster-kit sync cp -r local/folder/ cluster:/remote/
  cluster-kit sync cp -r cluster:/remote/folder/ local/path/
  cluster-kit sync cp local/file.txt cluster:/remote/ --dry-run
  cluster-kit sync cp local/file.txt cluster:/remote/ --verbose

Path Formats:
  Local path:        /absolute/path or relative/path
  Remote path:       host:/remote/path or cluster:/remote/path

Notes:
  • Use --recursive (-r) for directories
  • Use --dry-run to preview without copying
  • Use --verbose to see detailed transfer output
  • Cluster host is configured via CLUSTER_HOST environment variable
        """,
    )

    parser.add_argument(
        "src",
        help="Source path (local or host:remote_path)",
    )
    parser.add_argument(
        "dst",
        help="Destination path (local or host:remote_path)",
    )
    parser.add_argument(
        "--recursive",
        "-r",
        action="store_true",
        default=False,
        help="Copy directories recursively",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview the copy operation without executing",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show detailed transfer output",
    )

    args = parser.parse_args()

    # Create transfer and execute
    transfer = FileTransfer(
        dry_run=args.dry_run,
        verbose=args.verbose,
        recursive=args.recursive,
    )

    success = transfer.copy(args.src, args.dst)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
