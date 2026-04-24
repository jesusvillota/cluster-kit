"""SSH helpers for the cluster TUI backend."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from cluster_kit.utils import SSH_HOST


@dataclass(slots=True)
class SSHResult:
    """Normalized result for SSH command execution."""

    stdout: str = ""
    stderr: str = ""
    success: bool = True
    error_message: str = ""


def run_ssh_command(command: str, timeout: int = 30) -> SSHResult:
    """Run a command on the cluster over SSH."""

    try:
        result = subprocess.run(
            ["ssh", SSH_HOST, command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return SSHResult(
            success=False,
            error_message=f"SSH command timed out after {timeout}s",
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
