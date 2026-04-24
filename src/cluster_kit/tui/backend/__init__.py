"""TUI backend modules for cluster operations."""

from __future__ import annotations

from cluster_kit.tui.backend.available_resources import (
    FIXED_NODE_TOTALS,
    TARGET_NODE_NAMES,
    AvailableResourceRow,
    NodeTotals,
    fetch_available_resources,
    parse_sinfo_output,
)
from cluster_kit.tui.backend.job_actions import (
    QA_SAFE_MODE_ENV_VAR,
    JobStatus,
    cancel_job,
    get_job_status,
    is_qa_safe_mode_enabled,
    parse_sacct_output,
)
from cluster_kit.tui.backend.log_discovery import (
    LOGS_DIR,
    FileStat,
    LogFile,
    colorize_log_line,
    discover_log_files,
    fetch_log_content,
    fetch_log_tail,
    get_file_stat,
    parse_log_files,
)
from cluster_kit.tui.backend.queue_parser import (
    JobInfo,
    color_for_state,
    fetch_queue,
    parse_squeue_output,
)
from cluster_kit.tui.backend.ssh import SSHResult, run_ssh_command, test_connection

__all__ = [
    "SSHResult",
    "run_ssh_command",
    "test_connection",
    "JobInfo",
    "fetch_queue",
    "parse_squeue_output",
    "color_for_state",
    "LogFile",
    "FileStat",
    "LOGS_DIR",
    "discover_log_files",
    "parse_log_files",
    "fetch_log_tail",
    "fetch_log_content",
    "get_file_stat",
    "colorize_log_line",
    "NodeTotals",
    "AvailableResourceRow",
    "FIXED_NODE_TOTALS",
    "TARGET_NODE_NAMES",
    "fetch_available_resources",
    "parse_sinfo_output",
    "JobStatus",
    "QA_SAFE_MODE_ENV_VAR",
    "cancel_job",
    "get_job_status",
    "is_qa_safe_mode_enabled",
    "parse_sacct_output",
]
