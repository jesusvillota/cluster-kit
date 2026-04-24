"""AvailableResourcesTable widget for displaying fixed-node cluster capacity."""

# pyright: reportMissingImports=false

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.widget import Widget
from textual.widgets import DataTable, Static

from ..backend.available_resources import AvailableResourceRow

COLUMNS = (
    "NODE",
    "AVAIL_CPUS",
    "AVAIL_RAM",
    "AVAIL_GPUS",
    "USED_CPUS",
    "USED_RAM",
    "USED_GPUS",
    "TOTAL_CPUS",
    "TOTAL_RAM",
    "TOTAL_GPUS",
)

_AVAILABLE_STYLE = "green"
_USED_STYLE = "red"


@dataclass(frozen=True, slots=True)
class _PhoneResourceSummary:
    node_name: str
    capacity_line: str
    allocation_line: str
    availability_line: str

    def render(self) -> str:
        return (
            f"[bold cyan]{self.node_name}[/bold cyan]\n"
            f"[dim]{self.capacity_line}[/dim]\n"
            f"{self.allocation_line}\n"
            f"{self.availability_line}"
        )


class AvailableResourcesTable(Widget):
    """A Textual widget that wraps DataTable for node availability display."""

    DEFAULT_CSS = """
    AvailableResourcesTable {
        height: 1fr;
    }
    """

    def __init__(self, compact: bool = False, **kwargs: object) -> None:
        super().__init__(classes="phone-compact" if compact else None, **kwargs)
        self._compact = compact

    def compose(self) -> ComposeResult:
        if self._compact:
            with ScrollableContainer(id="available_resources_cards"):
                yield Static(
                    "[dim]Portrait summaries of fixed-node availability[/dim]",
                    id="available_resources_hint",
                )
                yield Static("", id="available_resources_cards_body")
            return

        table: DataTable[str] = DataTable(id="available_resources_data_table")
        table.add_columns(*COLUMNS)
        yield table

    def refresh_data(self, rows: list[AvailableResourceRow]) -> None:
        """Clear and repopulate the table with fresh availability data."""
        if self._compact:
            body = self.query_one("#available_resources_cards_body", Static)
            body.update(self._render_compact_rows(rows))
            return

        table = self.query_one(DataTable)
        table.clear(columns=False)

        for row in rows:
            table.add_row(
                row.node_name,
                Text(str(row.available_cpus), style=_AVAILABLE_STYLE),
                Text(f"{row.available_memory_gb} GB", style=_AVAILABLE_STYLE),
                Text(str(row.available_gpus), style=_AVAILABLE_STYLE),
                Text(str(row.allocated_cpus), style=_USED_STYLE),
                Text(f"{row.allocated_memory_gb} GB", style=_USED_STYLE),
                Text(str(row.allocated_gpus), style=_USED_STYLE),
                str(row.total_cpus),
                f"{row.total_memory_gb} GB",
                str(row.total_gpus),
            )

    def _render_compact_rows(self, rows: list[AvailableResourceRow]) -> str:
        if not rows:
            return "[dim]No resource data loaded yet. Pull to refresh.[/dim]"

        summaries = [self._make_summary(row).render() for row in rows]
        return "\n\n[dim]─[/dim]\n\n".join(summaries)

    @staticmethod
    def _make_summary(row: AvailableResourceRow) -> _PhoneResourceSummary:
        capacity_line = (
            f"{row.total_cpus} CPU • {row.total_memory_gb} GB • {row.total_gpus} GPU"
        )
        allocation_line = (
            f"[red]Used:[/red] {row.allocated_cpus} CPU • "
            f"{row.allocated_memory_gb} GB • {row.allocated_gpus} GPU"
        )
        availability_line = (
            f"[green]Free:[/green] {row.available_cpus} CPU • "
            f"{row.available_memory_gb} GB • {row.available_gpus} GPU"
        )
        return _PhoneResourceSummary(
            node_name=row.node_name,
            capacity_line=capacity_line,
            allocation_line=allocation_line,
            availability_line=availability_line,
        )
