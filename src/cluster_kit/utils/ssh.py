"""SSH connection utilities for cluster operations.

Provides connection testing and validation functionality for cluster access.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Optional

from rich import box
from rich.console import Console
from rich.panel import Panel

from cluster_kit.config import ClusterConfig, get_cluster_host, get_ssh_timeout

if TYPE_CHECKING:
    pass


class RemoteUnreachableError(RuntimeError):
    """Raised when the remote host cannot be reached over SSH."""


def _get_console() -> Console:
    """Create a Rich Console that works on both macOS and Windows.

    On Windows the default stdout codec is cp1252 which cannot encode Unicode
    characters used by Rich (checkmarks, box-drawing, etc.). Wrapping stdout
    in a UTF-8 TextIOWrapper makes Rich skip the broken legacy Windows
    renderer and write UTF-8 directly.
    """
    import io
    import sys

    if sys.platform == "win32":
        utf8_stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
        return Console(file=utf8_stdout)
    return Console()


# Global console instance for this module
_console = _get_console()


def _resolve_host_timeout(
    config: Optional[ClusterConfig],
) -> tuple[str, int]:
    if config is not None:
        return config.host, config.ssh_timeout
    return get_cluster_host(), get_ssh_timeout()


def run_remote(
    command: str,
    *,
    config: Optional[ClusterConfig] = None,
    timeout: Optional[int] = None,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run *command* on the remote host through exactly one shell layer.

    The command is passed as a single argv element to ``ssh host <command>``,
    so the remote login shell is the only place it is re-parsed. Callers must
    quote embedded paths themselves (``shlex.quote``).
    """
    host, default_timeout = _resolve_host_timeout(config)
    return subprocess.run(
        ["ssh", host, command],
        capture_output=capture,
        text=True,
        timeout=timeout if timeout is not None else default_timeout,
    )


def stream_remote(
    command: str,
    *,
    config: Optional[ClusterConfig] = None,
) -> int:
    """Run *command* remotely with inherited stdio; returns the exit code.

    Used for synchronous ``exec`` and log following, where output should
    stream straight to the user's terminal.
    """
    host, _ = _resolve_host_timeout(config)
    result = subprocess.run(["ssh", host, command])
    return result.returncode


def ensure_reachable(*, config: Optional[ClusterConfig] = None) -> None:
    """Fail fast with a VPN hint when the remote host is unreachable."""
    host, timeout = _resolve_host_timeout(config)
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={timeout}",
                host,
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteUnreachableError(
            f"Host '{host}' unreachable (timeout) — "
            "connect the VPN (FortiClient) first."
        ) from exc
    if result.returncode != 0:
        raise RemoteUnreachableError(
            f"Host '{host}' unreachable: {result.stderr.strip() or 'ssh failed'} — "
            "connect the VPN (FortiClient) first and check the SSH alias/key."
        )


class ClusterConnection:
    """Handle SSH connection testing and validation."""

    @staticmethod
    def test_connection(verbose: bool = True) -> bool:
        """Test SSH connection to the cluster.

        Args:
            verbose: Whether to print status messages

        Returns:
            True if connection successful, False otherwise
        """
        if verbose:
            _console.print("\n[cyan]Testing cluster connection...[/cyan]")

        try:
            ssh_host = get_cluster_host()
            timeout = get_ssh_timeout()
            result = subprocess.run(
                ["ssh", ssh_host, "echo 'SSH connection successful'"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if result.returncode == 0:
                if verbose:
                    _console.print("[green][OK] Cluster connection successful[/green]")
                return True
            else:
                if verbose:
                    ClusterConnection._show_connection_error()
                return False

        except subprocess.TimeoutExpired:
            if verbose:
                ClusterConnection._show_connection_error("Connection timeout")
            return False
        except Exception as e:
            if verbose:
                ClusterConnection._show_connection_error(str(e))
            return False

    @staticmethod
    def _show_connection_error(error_msg: Optional[str] = None):
        """Display formatted connection error message."""
        message = "[red][FAIL] Cannot connect to cluster[/red]\n\n"
        if error_msg:
            message += f"[yellow]Error:[/yellow] {error_msg}\n\n"

        message += (
            "[yellow]Please ensure:[/yellow]\n"
            "  • VPN is connected (FortiClient for hosts behind the VPN)\n"
            "  • SSH key is configured\n"
            "  • SSH alias is configured correctly"
        )

        _console.print(
            Panel(
                message,
                title="• Connection Error",
                border_style="red",
                box=box.ROUNDED,
            )
        )


def __getattr__(name: str):
    if name == "SSH_HOST":
        return get_cluster_host()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
