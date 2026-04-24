"""QueueTable widget for displaying SLURM squeue output in a Textual DataTable."""

# pyright: reportMissingImports=false

from __future__ import annotations

from rich.text import Text
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable

from ..backend.queue_parser import JobInfo, color_for_state

_RUNNING_OR_COMPLETING_STATES = {"R", "RUNNING", "CG", "COMPLETING"}

COLUMNS = (
    "JOBID",
    "NAME",
    "USER",
    "PARTITION",
    "STATE",
    "TIME",
    "NODES",
    "CPUS",
    "RAM",
    "GPUS",
    "REASON",
)

_PLACEHOLDER_ROW_KEY = "__no_jobs__"


class JobSelected(Message):
    """Posted when the user selects a row in the queue table."""

    def __init__(self, job_info: JobInfo) -> None:
        self.job_info: JobInfo = job_info
        super().__init__()


class QueueTable(Widget):
    """A Textual widget that wraps DataTable for SLURM queue display."""

    DEFAULT_CSS = """
    QueueTable {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._jobs: list[JobInfo] = []

    def compose(self):  # type: ignore[override]
        table: DataTable[Text] = DataTable(cursor_type="row", id="queue_data_table")
        table.add_columns(*COLUMNS)
        yield table

    def on_mount(self) -> None:
        self._set_placeholder()

    def refresh_data(self, jobs: list[JobInfo]) -> None:
        """Clear and repopulate the table with fresh job data."""
        table = self.query_one(DataTable)
        table.clear(columns=False)
        self._jobs = list(jobs)

        if not jobs:
            self._set_placeholder()
            return

        for job in jobs:
            state_color = color_for_state(job.state)
            row: tuple[Text | str, ...] = (
                job.job_id,
                job.name,
                job.user,
                job.partition,
                Text(job.state, style=state_color),
                job.time,
                job.nodes,
                job.cpus_display,
                job.ram_display,
                job.gpus_display,
                self._reason_column_value(job),
            )
            table.add_row(*row)

    @staticmethod
    def _reason_column_value(job: JobInfo) -> str:
        """Return the last-column display value without mutating parsed job data."""

        if (
            job.state.strip().upper() in _RUNNING_OR_COMPLETING_STATES
            and job.node_list.strip()
        ):
            return job.node_list
        return job.reason

    def get_selected_job(self) -> JobInfo | None:
        """Return the JobInfo for the currently highlighted row, or None."""
        table = self.query_one(DataTable)
        if not self._jobs:
            return None
        cursor_row = table.cursor_row
        if cursor_row < 0 or cursor_row >= len(self._jobs):
            return None
        return self._jobs[cursor_row]

    def set_loading(self, value: bool) -> None:
        self.query_one(DataTable).loading = value

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Forward row selection as a JobSelected message."""
        cursor_row = event.cursor_row
        if 0 <= cursor_row < len(self._jobs):
            self.post_message(JobSelected(self._jobs[cursor_row]))

    def _set_placeholder(self) -> None:
        """Show a 'no jobs' placeholder in the otherwise empty table."""
        table = self.query_one(DataTable)
        table.clear(columns=False)
        empty_cells: tuple[str, ...] = ("No jobs in queue",) + ("",) * (
            len(COLUMNS) - 1
        )
        table.add_row(*empty_cells, key=_PLACEHOLDER_ROW_KEY)
