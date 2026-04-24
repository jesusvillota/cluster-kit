"""SSH connection utilities for cluster operations.

Provides connection testing and validation functionality for cluster access.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Optional

from rich import box
from rich.console import Console
from rich.panel import Panel

from cluster_kit.config import get_cluster_host, get_ssh_timeout

if TYPE_CHECKING:
    pass


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
            "  • VPN is connected\n"
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


# Backward compatibility: SSH_HOST constant
SSH_HOST: str = get_cluster_host()
