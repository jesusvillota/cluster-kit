# pyright: reportMissingImports=false
"""Log discovery helpers for the cluster TUI."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from cluster_kit.config import get_remote_base
from cluster_kit.utils import SSH_HOST

from .ssh import SSHResult, run_ssh_command

LOGS_DIR = f"{get_remote_base()}/_logs_"


@dataclass
class LogFile:
    """Discovered remote log file."""

    path: str
    filename: str
    is_stderr: bool
    job_id: str


@dataclass
class FileStat:
    """Parsed file metadata from remote stat output."""

    size_bytes: int
    modified: str


def discover_log_files(job_id: str, task_id: str | None = None) -> SSHResult:
    """Discover remote log files for a job or array task."""
    if task_id is None:
        find_cmd = (
            f"find {LOGS_DIR} \\( "
            f"-name '*_{job_id}.out' -o -name '*_{job_id}.err' "
            f"-o -name '*_{job_id}_*.out' -o -name '*_{job_id}_*.err' \\) "
            "-type f 2>/dev/null"
        )
    else:
        find_cmd = (
            f"find {LOGS_DIR} \\( "
            f"-name '*_{job_id}_{task_id}.out' -o -name '*_{job_id}_{task_id}.err' \\) "
            "-type f 2>/dev/null"
        )
    return run_ssh_command(find_cmd, timeout=30)


def parse_log_files(find_output: str) -> list[LogFile]:
    """Parse newline-delimited find output into LogFile objects."""
    files: list[LogFile] = []
    for line in (entry.strip() for entry in find_output.splitlines()):
        if not line:
            continue
        filename = line.split("/")[-1]
        stem, _, _extension = filename.rpartition(".")
        parts = stem.split("_")
        numeric_parts = [part for part in parts if part.isdigit()]

        if not numeric_parts:
            extracted_job_id = ""
        elif parts[-1].isdigit() and len(numeric_parts) >= 2:
            extracted_job_id = numeric_parts[-2]
        else:
            extracted_job_id = numeric_parts[-1]

        files.append(
            LogFile(
                path=line,
                filename=filename,
                is_stderr=filename.endswith(".err"),
                job_id=extracted_job_id,
            )
        )
    return files


def fetch_log_tail(file_path: str, lines: int = 50) -> SSHResult:
    """Fetch the last N lines from a remote log file."""
    return run_ssh_command(
        f"tail -n {lines} {shlex.quote(file_path)}",
        timeout=30,
    )


def fetch_log_content(file_path: str) -> SSHResult:
    """Fetch full content from a remote log file."""
    return run_ssh_command(f"cat {shlex.quote(file_path)}", timeout=60)


def get_file_stat(file_path: str) -> FileStat | None:
    """Fetch and parse file size and modified time from the cluster."""
    result = run_ssh_command(
        f"stat --printf='%s\\t%y\\n' {shlex.quote(file_path)}",
        timeout=15,
    )
    if not result.success:
        return None

    try:
        parts = result.stdout.strip().split("\t", 1)
        size_bytes = int(parts[0])
        modified = parts[1][:19]
        return FileStat(size_bytes=size_bytes, modified=modified)
    except (IndexError, ValueError, TypeError):
        return None


def colorize_log_line(line: str) -> str:
    """Apply Rich markup matching the existing CLI log viewer."""
    if "ERROR" in line or "Error" in line or "error" in line:
        return f"[red]{line}[/red]"
    if "WARNING" in line or "Warning" in line:
        return f"[yellow]{line}[/yellow]"
    if "SUCCESS" in line or "Success" in line or "[OK]" in line:
        return f"[green]{line}[/green]"
    if "==>" in line:
        return f"[bold cyan]{line}[/bold cyan]"
    return line


__all__ = [
    "FileStat",
    "LOGS_DIR",
    "LogFile",
    "SSH_HOST",
    "SSHResult",
    "colorize_log_line",
    "discover_log_files",
    "fetch_log_content",
    "fetch_log_tail",
    "get_file_stat",
    "parse_log_files",
]
