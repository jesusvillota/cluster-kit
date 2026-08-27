"""PcJobsTable widget for displaying detached jobs running on the PC."""

# pyright: reportMissingImports=false

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.widget import Widget
from textual.widgets import DataTable, Static

COLUMNS = ("NAME", "STATE", "CREATED", "RC", "JOB_ID")
COLUMN_WIDTHS = (28, 12, 24, 6, 28)

_STATE_STYLES = {
    "RUNNING": "green",
    "FAILED": "red",
    "DIED": "red",
    "CANCELLED": "yellow",
    "COMPLETED": "dim",
}


def _state_text(state: str) -> Text:
    return Text(state, style=_STATE_STYLES.get(state, ""))


class PcJobsTable(Widget):
    """Read-only view of the PC's detached-job registry."""

    DEFAULT_CSS = """
    PcJobsTable {
        height: 1fr;
    }
    """

    def __init__(self, compact: bool = False, **kwargs: object) -> None:
        super().__init__(classes="phone-compact" if compact else None, **kwargs)
        self._compact = compact

    def compose(self) -> ComposeResult:
        if self._compact:
            with ScrollableContainer(id="pc_jobs_cards"):
                yield Static(
                    "[dim]Detached jobs on the PC[/dim]",
                    id="pc_jobs_hint",
                )
                yield Static("", id="pc_jobs_cards_body")
            return

        table: DataTable[str] = DataTable(
            id="pc_jobs_data_table",
        )
        for label, width in zip(COLUMNS, COLUMN_WIDTHS):
            table.add_column(label, width=width)
        yield table

    def refresh_data(self, jobs: list[dict], error: str | None) -> None:
        """Clear and repopulate with fresh PC job state (or an error line)."""
        if self._compact:
            body = self.query_one("#pc_jobs_cards_body", Static)
            body.update(self._render_compact(jobs, error))
            return

        table = self.query_one(DataTable)
        table.clear(columns=False)
        if error is not None:
            table.add_row(Text(error, style="red"), "", "", "", "")
            return
        for job in jobs:
            table.add_row(
                job.get("name") or "?",
                _state_text(job.get("state") or "UNKNOWN"),
                job.get("created_at") or "",
                "" if job.get("rc") is None else str(job["rc"]),
                job.get("job_id") or "",
            )

    @staticmethod
    def _render_compact(jobs: list[dict], error: str | None) -> str:
        if error is not None:
            return f"[red]{error}[/red]"
        if not jobs:
            return "[dim]No jobs on the PC.[/dim]"
        cards = []
        for job in jobs:
            state = job.get("state") or "UNKNOWN"
            style = _STATE_STYLES.get(state, "white")
            rc = job.get("rc")
            rc_part = "" if rc is None else f" • rc={rc}"
            cards.append(
                f"[bold cyan]{job.get('name') or '?'}[/bold cyan]\n"
                f"[{style}]{state}[/{style}]{rc_part}\n"
                f"[dim]{job.get('created_at') or ''} · {job.get('job_id') or ''}[/dim]"
            )
        return "\n\n[dim]─[/dim]\n\n".join(cards)
