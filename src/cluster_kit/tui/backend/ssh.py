"""SSH helpers for the cluster TUI backend."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from cluster_kit.config import get_cluster_host, get_ssh_timeout

_PROCESS_TIMEOUT_GRACE_SECONDS = 5


@dataclass(slots=True)
class SSHResult:
    """Normalized result for SSH command execution."""

    stdout: str = ""
    stderr: str = ""
    success: bool = True
    error_message: str = ""


def run_ssh_command(command: str, timeout: int | None = None) -> SSHResult:
    """Run a command on the cluster over SSH."""

    operation_timeout = get_ssh_timeout() if timeout is None else timeout
    try:
        ssh_host = get_cluster_host()
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={operation_timeout}",
                ssh_host,
                command,
            ],
            capture_output=True,
            text=True,
            timeout=operation_timeout + _PROCESS_TIMEOUT_GRACE_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return SSHResult(
            success=False,
            error_message=f"SSH command timed out after {operation_timeout}s",
        )
    except Exception as exc:
        return SSHResult(success=False, error_message=str(exc))

    if result.returncode == 0:
        return SSHResult(stdout=result.stdout, stderr=result.stderr, success=True)

    return SSHResult(
        stdout=result.stdout,
        stderr=result.stderr,
        success=False,
        error_message=result.stderr or "SSH command failed",
    )


def test_connection() -> SSHResult:
    """Test basic SSH connectivity to the cluster."""

    return run_ssh_command("echo 'ok'")
