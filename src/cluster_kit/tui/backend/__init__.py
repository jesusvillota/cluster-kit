"""TUI backend modules for cluster operations."""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "SSHResult": ("cluster_kit.tui.backend.ssh", "SSHResult"),
    "run_ssh_command": ("cluster_kit.tui.backend.ssh", "run_ssh_command"),
    "test_connection": ("cluster_kit.tui.backend.ssh", "test_connection"),
    "JobInfo": ("cluster_kit.tui.backend.queue_parser", "JobInfo"),
    "fetch_queue": ("cluster_kit.tui.backend.queue_parser", "fetch_queue"),
    "parse_squeue_output": (
        "cluster_kit.tui.backend.queue_parser",
        "parse_squeue_output",
    ),
    "color_for_state": ("cluster_kit.tui.backend.queue_parser", "color_for_state"),
    "LogFile": ("cluster_kit.tui.backend.log_discovery", "LogFile"),
    "FileStat": ("cluster_kit.tui.backend.log_discovery", "FileStat"),
    "LOGS_DIR": ("cluster_kit.tui.backend.log_discovery", "LOGS_DIR"),
    "discover_log_files": (
        "cluster_kit.tui.backend.log_discovery",
        "discover_log_files",
    ),
    "parse_log_files": (
        "cluster_kit.tui.backend.log_discovery",
        "parse_log_files",
    ),
    "fetch_log_tail": (
        "cluster_kit.tui.backend.log_discovery",
        "fetch_log_tail",
    ),
    "fetch_log_content": (
        "cluster_kit.tui.backend.log_discovery",
        "fetch_log_content",
    ),
    "get_file_stat": (
        "cluster_kit.tui.backend.log_discovery",
        "get_file_stat",
    ),
    "colorize_log_line": (
        "cluster_kit.tui.backend.log_discovery",
        "colorize_log_line",
    ),
    "NodeTotals": ("cluster_kit.tui.backend.available_resources", "NodeTotals"),
    "AvailableResourceRow": (
        "cluster_kit.tui.backend.available_resources",
        "AvailableResourceRow",
    ),
    "FIXED_NODE_TOTALS": (
        "cluster_kit.tui.backend.available_resources",
        "FIXED_NODE_TOTALS",
    ),
    "TARGET_NODE_NAMES": (
        "cluster_kit.tui.backend.available_resources",
        "TARGET_NODE_NAMES",
    ),
    "fetch_available_resources": (
        "cluster_kit.tui.backend.available_resources",
        "fetch_available_resources",
    ),
    "parse_sinfo_output": (
        "cluster_kit.tui.backend.available_resources",
        "parse_sinfo_output",
    ),
    "JobStatus": ("cluster_kit.tui.backend.job_actions", "JobStatus"),
    "QA_SAFE_MODE_ENV_VAR": (
        "cluster_kit.tui.backend.job_actions",
        "QA_SAFE_MODE_ENV_VAR",
    ),
    "cancel_job": ("cluster_kit.tui.backend.job_actions", "cancel_job"),
    "get_job_status": ("cluster_kit.tui.backend.job_actions", "get_job_status"),
    "is_qa_safe_mode_enabled": (
        "cluster_kit.tui.backend.job_actions",
        "is_qa_safe_mode_enabled",
    ),
    "parse_sacct_output": (
        "cluster_kit.tui.backend.job_actions",
        "parse_sacct_output",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute_name = _EXPORTS[name]
    module = import_module(module_name)
    return getattr(module, attribute_name)


def __dir__() -> list[str]:
    return sorted(list(globals()) + __all__)
