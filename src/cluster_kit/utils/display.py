"""Rich display utilities for formatted console output.

Provides panel-based formatting for configuration display, success/error
messages, and step headers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from rich import box
from rich.console import Console
from rich.panel import Panel

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


def show_config_panel(title: str, config: dict[str, str]):
    """Display configuration in a formatted panel.

    Args:
        title: Panel title
        config: Dictionary of configuration key-value pairs
    """
    content = "\n".join([f"[cyan]{k}:[/cyan] {v}" for k, v in config.items()])

    _console.print(
        Panel(
            content,
            title=f"[bold]{title}[/bold]",
            border_style="cyan",
            box=box.ROUNDED,
        )
    )


def show_success_panel(message: str, details: Optional[dict[str, str]] = None):
    """Display success message in a formatted panel.

    Args:
        message: Success message
        details: Optional dictionary of additional details
    """
    content = f"[green]{message}[/green]"

    if details:
        content += "\n\n" + "\n".join(
            [f"[cyan]{k}:[/cyan] {v}" for k, v in details.items()]
        )

    _console.print(
        Panel(
            content,
            title="[OK] Success",
            border_style="green",
            box=box.ROUNDED,
        )
    )


def show_error_panel(message: str, details: Optional[str] = None):
    """Display error message in a formatted panel.

    Args:
        message: Error message
        details: Optional additional details
    """
    content = f"[red]{message}[/red]"

    if details:
        content += f"\n\n[yellow]Details:[/yellow]\n{details}"

    _console.print(
        Panel(
            content,
            title="• Error",
            border_style="red",
            box=box.ROUNDED,
        )
    )


def show_step_header(step_num: int, total_steps: int, description: str):
    """Display a step header with progress indicator.

    Args:
        step_num: Current step number
        total_steps: Total number of steps
        description: Step description
    """
    _console.print(
        f"\n[cyan]--- Step {step_num}/{total_steps}: {description} ---[/cyan]\n"
    )
