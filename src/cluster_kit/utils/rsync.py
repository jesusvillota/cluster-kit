"""Rsync and SCP file transfer utilities.

Provides rsync-based syncing with progress tracking and SCP fallback for
Windows compatibility.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from rich.console import Console

if TYPE_CHECKING:
    pass


def _get_console() -> Console:
    """Create a Rich Console that works on both macOS and Windows."""
    import io
    import sys

    if sys.platform == "win32":
        utf8_stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        return Console(file=utf8_stdout)
    return Console()


_console = _get_console()


class RsyncRunner:
    """Execute rsync commands with progress tracking."""

    def __init__(
        self, dry_run: bool = False, verbose: bool = False, delete: bool = False
    ):
        """Initialize RsyncRunner.

        Args:
            dry_run: If True, show what would be done without executing
            verbose: If True, show detailed rsync output
            delete: If True, delete files in dest that don't exist in source
        """
        self.dry_run = dry_run
        self.verbose = verbose
        self.delete = delete

    def sync(
        self,
        source: str,
        dest: str,
        includes: Optional[list[str]] = None,
        excludes: Optional[list[str]] = None,
        show_progress: bool = True,
    ) -> bool:
        """Execute rsync with optional progress tracking.

        Args:
            source: Source path (can include user@host:path)
            dest: Destination path
            includes: List of patterns to include (--include)
            excludes: List of patterns to exclude (--exclude)
            show_progress: Whether to show progress bars

        Returns:
            True if sync successful, False otherwise
        """
        # Build rsync command
        cmd = ["rsync", "-az"]

        if self.verbose:
            cmd.append("-v")

        if self.dry_run:
            cmd.append("--dry-run")

        if self.delete:
            cmd.append("--delete")

        # Add includes (must come before excludes)
        if includes:
            for pattern in includes:
                cmd.extend(["--include", pattern])

        # Add excludes
        if excludes:
            for pattern in excludes:
                cmd.extend(["--exclude", pattern])

        # Standard excludes for Python cache
        cmd.extend(["--exclude", "__pycache__"])
        cmd.extend(["--exclude", "*.pyc"])
        cmd.extend(["--exclude", "*.pyo"])

        # Add progress flag if we're showing live output
        if self.verbose and show_progress:
            cmd.append("--progress")

        # Add source and dest
        cmd.extend([source, dest])

        # Execute rsync
        try:
            if self.verbose and show_progress:
                # Show live rsync output
                result = subprocess.run(cmd, text=True)
            else:
                # Run quietly with spinner
                with _console.status("[cyan]Syncing files..."):
                    result = subprocess.run(cmd, capture_output=True, text=True)

            return result.returncode == 0

        except Exception as e:
            _console.print(f"[red]ERROR during rsync:[/red] {e}")
            return False

    def build_command_preview(
        self,
        source: str,
        dest: str,
        includes: Optional[list[str]] = None,
        excludes: Optional[list[str]] = None,
    ) -> str:
        """Build the rsync command for preview/dry-run display."""
        cmd = ["rsync", "-az"]

        if self.verbose:
            cmd.append("-v")
        if self.dry_run:
            cmd.append("--dry-run")
        if self.delete:
            cmd.append("--delete")

        if includes:
            for pattern in includes:
                cmd.extend(["--include", pattern])
        if excludes:
            for pattern in excludes:
                cmd.extend(["--exclude", pattern])

        cmd.extend(["--exclude", "__pycache__"])
        cmd.extend(["--exclude", "*.pyc"])
        cmd.extend(["--exclude", "*.pyo"])
        cmd.extend([source, dest])

        return " ".join(cmd)


class ScpRunner:
    """Execute scp file transfers — Windows-compatible drop-in for RsyncRunner.

    Uses ``scp -r`` which ships with OpenSSH on Windows 10+. Includes/excludes
    are not supported by scp and are silently ignored.
    """

    def __init__(
        self, dry_run: bool = False, verbose: bool = False, delete: bool = False
    ):
        """Initialize ScpRunner.

        Args:
            dry_run: If True, preview the command without executing it
            verbose: If True, show scp progress output
            delete: Accepted for interface compatibility; not used by scp
        """
        self.dry_run = dry_run
        self.verbose = verbose
        self.delete = delete  # unused — remote dirs are removed before scp runs

    def sync(
        self,
        source: str,
        dest: str,
        includes: Optional[list[str]] = None,
        excludes: Optional[list[str]] = None,
        show_progress: bool = True,
    ) -> bool:
        """Copy a local directory to a remote host using ``scp -r``.

        Args:
            source: Local source path (trailing slash stripped automatically)
            dest: Destination in ``host:path`` form
            includes: Ignored — scp does not support include filters
            excludes: Ignored — scp does not support exclude filters
            show_progress: If True, let scp write progress to the terminal

        Returns:
            True if the transfer succeeded, False otherwise
        """
        # Strip trailing slash/backslash so scp copies the directory itself
        # rather than only its contents, which matches rsync -az src/ behaviour
        # when the remote directory has already been removed.
        source_clean = str(Path(source))  # normalise separators

        cmd = ["scp", "-r"]
        if not (self.verbose and show_progress):
            cmd.append("-q")  # suppress scp's per-file noise when not verbose

        cmd.extend([source_clean, dest])

        if self.dry_run:
            _console.print(f"[yellow](dry-run) Would run:[/yellow] {' '.join(cmd)}")
            return True

        try:
            if self.verbose and show_progress:
                result = subprocess.run(cmd, text=True)
            else:
                with _console.status("[cyan]Copying files via scp..."):
                    result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                stderr = getattr(result, "stderr", "") or ""
                if stderr:
                    _console.print(f"[red]scp error:[/red] {stderr}")

            return result.returncode == 0

        except Exception as e:
            _console.print(f"[red]ERROR during scp:[/red] {e}")
            return False
