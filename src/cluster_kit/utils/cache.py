"""Python cache cleaning utilities.

Provides local and remote cache file removal for __pycache__ directories,
.pyc, and .pyo files.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from cluster_kit.config import get_cluster_host

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


class PythonCacheCleaner:
    """Clean Python cache files locally and remotely."""

    @staticmethod
    def clean_local(
        directories: list[Path], verbose: bool = True, dry_run: bool = False
    ) -> dict[str, int]:
        """Clean Python cache files in local directories.

        Uses pathlib for cross-platform compatibility (no ``find`` subprocess).

        Args:
            directories: List of directory paths to clean
            verbose: Whether to show progress messages
            dry_run: If True, count but don't delete files

        Returns:
            Dict with counts: {pycache_dirs, pyc_files, pyo_files}
        """
        if verbose:
            mode_str = " (DRY RUN)" if dry_run else ""
            _console.print(
                f"\n[cyan]--- Cleaning Local Python Cache{mode_str} ---[/cyan]\n"
            )

        pycache_count = 0
        pyc_count = 0
        pyo_count = 0

        with _console.status("[cyan]Scanning for cache files...") as status:
            for directory in directories:
                if not directory.exists():
                    continue

                if verbose:
                    status.update(f"[cyan]Cleaning {directory.name}/...[/cyan]")

                try:
                    # Collect all cache items upfront (avoids lazy-generator
                    # issues when parent dirs are removed mid-iteration)
                    pycache_dirs = [
                        p for p in directory.rglob("__pycache__") if p.is_dir()
                    ]
                    pyc_files = [p for p in directory.rglob("*.pyc") if p.is_file()]
                    pyo_files = [p for p in directory.rglob("*.pyo") if p.is_file()]

                    pycache_count += len(pycache_dirs)
                    pyc_count += len(pyc_files)
                    pyo_count += len(pyo_files)

                    if not dry_run:
                        for p in pycache_dirs:
                            shutil.rmtree(p, ignore_errors=True)
                        for p in pyc_files:
                            try:
                                p.unlink(missing_ok=True)
                            except Exception:
                                pass
                        for p in pyo_files:
                            try:
                                p.unlink(missing_ok=True)
                            except Exception:
                                pass

                except Exception as e:
                    if verbose:
                        _console.print(
                            f"[yellow]Warning: Error cleaning {directory}: {e}[/yellow]"
                        )

        if verbose:
            total = pycache_count + pyc_count + pyo_count
            action = "Would remove" if dry_run else "Removed"
            _console.print(
                f"[green][OK][/green] {action} {pycache_count} __pycache__ dirs, "
                f"{pyc_count} .pyc files, {pyo_count} .pyo files "
                f"(total: {total})\n"
            )

        return {
            "pycache_dirs": pycache_count,
            "pyc_files": pyc_count,
            "pyo_files": pyo_count,
        }

    @staticmethod
    def clean_remote(
        base_path: str, verbose: bool = True, dry_run: bool = False
    ) -> dict[str, int]:
        """Clean Python cache files on remote cluster.

        Uses a single SSH call to count and optionally delete all cache items,
        avoiding the overhead of multiple SSH connections and repeated ``find``
        traversals.

        Args:
            base_path: Base directory path on cluster
            verbose: Whether to show progress messages
            dry_run: If True, don't actually delete files

        Returns:
            Dict with counts: {pycache_dirs, pyc_files, pyo_files}
        """
        if verbose:
            mode_str = " (DRY RUN)" if dry_run else ""
            _console.print(
                f"\n[cyan]--- Cleaning Remote Python Cache{mode_str} ---[/cyan]\n"
            )

        try:
            ssh_host = get_cluster_host()

            # Build a single SSH command that counts and (optionally) deletes
            # everything in one pass. Two find invocations are needed: one for
            # directories (must run first so that .pyc counts aren't inflated by
            # files inside __pycache__ dirs) and one for leftover loose files.
            if dry_run:
                # Count-only: print paths so we can tally them, but don't delete
                remote_script = (
                    f"echo '===PYCACHE_DIRS==='; "
                    f"find '{base_path}' -type d -name '__pycache__' 2>/dev/null; "
                    f"echo '===PYC_FILES==='; "
                    f"find '{base_path}' -type f -name '*.pyc' 2>/dev/null; "
                    f"echo '===PYO_FILES==='; "
                    f"find '{base_path}' -type f -name '*.pyo' 2>/dev/null"
                )
            else:
                # Count by printing, then delete in the same find pass.
                # __pycache__ dirs are removed first; loose .pyc/.pyo after.
                remote_script = (
                    f"echo '===PYCACHE_DIRS==='; "
                    f"find '{base_path}' -type d -name '__pycache__' "
                    f"-print -exec rm -rf {{}} + 2>/dev/null; "
                    f"echo '===PYC_FILES==='; "
                    f"find '{base_path}' -type f -name '*.pyc' "
                    f"-print -delete 2>/dev/null; "
                    f"echo '===PYO_FILES==='; "
                    f"find '{base_path}' -type f -name '*.pyo' "
                    f"-print -delete 2>/dev/null"
                )

            with _console.status("[cyan]Cleaning cache on cluster..."):
                result = subprocess.run(
                    ["ssh", ssh_host, remote_script],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

            # Parse output into counts by splitting on sentinel markers
            pycache_count = 0
            pyc_count = 0
            pyo_count = 0

            if result.returncode == 0:
                section = None
                for line in result.stdout.splitlines():
                    if line == "===PYCACHE_DIRS===":
                        section = "pycache"
                        continue
                    elif line == "===PYC_FILES===":
                        section = "pyc"
                        continue
                    elif line == "===PYO_FILES===":
                        section = "pyo"
                        continue

                    if line.strip():
                        if section == "pycache":
                            pycache_count += 1
                        elif section == "pyc":
                            pyc_count += 1
                        elif section == "pyo":
                            pyo_count += 1

            if verbose:
                total = pycache_count + pyc_count + pyo_count
                action = "Would remove" if dry_run else "Removed"
                _console.print(
                    f"[green][OK][/green] {action} {pycache_count} __pycache__ dirs, "
                    f"{pyc_count} .pyc files, {pyo_count} .pyo files "
                    f"(total: {total})\n"
                )

            return {
                "pycache_dirs": pycache_count,
                "pyc_files": pyc_count,
                "pyo_files": pyo_count,
            }

        except Exception as e:
            if verbose:
                _console.print(f"[red]ERROR cleaning remote cache:[/red] {e}\n")
            return {"pycache_dirs": 0, "pyc_files": 0, "pyo_files": 0}
