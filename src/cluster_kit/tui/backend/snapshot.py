"""Fetch queue and node state through one framed SSH round trip."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .available_resources import build_sinfo_command
from .queue_parser import build_squeue_command
from .ssh import SSHResult, run_ssh_command

_PREFIX = "__CLUSTER_KIT_SNAPSHOT_V1__"
_QUEUE_BEGIN = f"{_PREFIX}QUEUE_BEGIN"
_QUEUE_END = f"{_PREFIX}QUEUE_END"
_RESOURCES_BEGIN = f"{_PREFIX}RESOURCES_BEGIN"
_RESOURCES_END = f"{_PREFIX}RESOURCES_END"


@dataclass(frozen=True, slots=True)
class ClusterSnapshotResult:
    """Independent queue and node-resource results from one SSH request."""

    queue: SSHResult
    resources: SSHResult


def _failed_snapshot(message: str) -> ClusterSnapshotResult:
    return ClusterSnapshotResult(
        queue=SSHResult(success=False, error_message=message),
        resources=SSHResult(success=False, error_message=message),
    )


def _extract_frame(raw: str, begin: str, end: str, label: str) -> SSHResult:
    pattern = re.compile(
        rf"^{re.escape(begin)}\r?\n(.*?)^{re.escape(end)}:(\d+)\r?$",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(raw)
    if match is None:
        return SSHResult(
            success=False,
            error_message=f"Malformed cluster snapshot: missing {label} frame",
        )

    output = match.group(1)
    if output.endswith("\r\n"):
        output = output[:-2]
    elif output.endswith("\n"):
        output = output[:-1]
    return_code = int(match.group(2))
    if return_code == 0:
        return SSHResult(stdout=output, success=True)

    detail = output.strip().splitlines()
    message = detail[0] if detail else f"{label} command exited {return_code}"
    return SSHResult(
        stdout=output,
        success=False,
        error_message=message,
    )


def parse_cluster_snapshot(raw: str) -> ClusterSnapshotResult:
    """Parse independently framed squeue and sinfo command results."""

    return ClusterSnapshotResult(
        queue=_extract_frame(raw, _QUEUE_BEGIN, _QUEUE_END, "queue"),
        resources=_extract_frame(
            raw,
            _RESOURCES_BEGIN,
            _RESOURCES_END,
            "resource",
        ),
    )


def _frame_command(command: str, begin: str, end: str, status_name: str) -> str:
    return " ".join(
        (
            f"printf '%s\\n' {shlex.quote(begin)};",
            f"{command} 2>&1;",
            f"{status_name}=$?;",
            f"printf '\\n%s:%s\\n' {shlex.quote(end)} \"${status_name}\";",
        )
    )


def fetch_cluster_snapshot(user: str | None = None) -> ClusterSnapshotResult:
    """Fetch squeue and sinfo output in one bounded SSH subprocess."""

    remote_command = " ".join(
        (
            _frame_command(
                build_squeue_command(user=user),
                _QUEUE_BEGIN,
                _QUEUE_END,
                "queue_status",
            ),
            _frame_command(
                build_sinfo_command(),
                _RESOURCES_BEGIN,
                _RESOURCES_END,
                "resource_status",
            ),
            "exit 0",
        )
    )
    result = run_ssh_command(remote_command)
    if not result.success:
        return _failed_snapshot(result.error_message or "Cluster SSH request failed")
    return parse_cluster_snapshot(result.stdout)


__all__ = [
    "ClusterSnapshotResult",
    "fetch_cluster_snapshot",
    "parse_cluster_snapshot",
]
