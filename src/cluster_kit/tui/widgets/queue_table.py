"""QueueTable widget for displaying SLURM squeue output in a Textual DataTable."""

# pyright: reportMissingImports=false

from __future__ import annotations

from rich.text import Text
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable

from ..backend.queue_parser import JobInfo, color_for_state
from ..ownership import user_matches_allowed_owner

_RUNNING_OR_COMPLETING_STATES = {"R", "RUNNING", "CG", "COMPLETING"}
_NON_SELECTABLE_ROW_STYLE = "grey62"

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
        self._allowed_user = ""
        self._selected_job_id: str | None = None
        self._selectable_rows: tuple[int, ...] = ()
        self._last_selectable_row: int | None = None
        self._is_adjusting_cursor = False

    def compose(self):  # type: ignore[override]
        table: DataTable[Text] = DataTable(cursor_type="row", id="queue_data_table")
        table.add_columns(*COLUMNS)
        yield table

    def on_mount(self) -> None:
        self._set_placeholder()

    def refresh_data(
        self, jobs: list[JobInfo], current_user: str = ""
    ) -> None:
        """Clear and repopulate the table with fresh job data."""
        table = self.query_one(DataTable)
        previous_selected_job_id = self._selected_job_id
        table.clear(columns=False)
        self._jobs = list(jobs)
        self._allowed_user = current_user
        self._selectable_rows = tuple(
            index
            for index, job in enumerate(self._jobs)
            if self._is_selectable_job(job)
        )
        table.show_cursor = bool(self._selectable_rows)

        if not jobs:
            self._selected_job_id = None
            self._last_selectable_row = None
            self._set_placeholder()
            return

        for job in jobs:
            table.add_row(*self._build_row(job))

        selected_row = self._selectable_row_for_job_id(previous_selected_job_id)
        if selected_row is None and self._selectable_rows:
            selected_row = self._selectable_rows[0]

        if selected_row is None:
            self._selected_job_id = None
            self._last_selectable_row = None
            return

        self._move_cursor_to_row(selected_row, scroll=False)

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
        if not self._is_selectable_row(cursor_row):
            return None
        return self._jobs[cursor_row]

    def set_loading(self, value: bool) -> None:
        self.query_one(DataTable).loading = value

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Keep the row cursor on jobs owned by the configured selectable user."""

        if self._is_adjusting_cursor:
            return

        cursor_row = event.cursor_row
        if self._is_selectable_row(cursor_row):
            self._remember_selectable_row(cursor_row)
            return

        target_row = self._nearest_selectable_row(cursor_row)
        if target_row is None:
            self.query_one(DataTable).show_cursor = False
            self._selected_job_id = None
            self._last_selectable_row = None
            return

        self._move_cursor_to_row(target_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Forward row selection as a JobSelected message."""
        cursor_row = event.cursor_row
        if not self._is_selectable_row(cursor_row):
            target_row = self._nearest_selectable_row(cursor_row)
            if target_row is not None:
                self._move_cursor_to_row(target_row)
            return

        self._remember_selectable_row(cursor_row)
        self.post_message(JobSelected(self._jobs[cursor_row]))

    def _set_placeholder(self) -> None:
        """Show a 'no jobs' placeholder in the otherwise empty table."""
        table = self.query_one(DataTable)
        table.clear(columns=False)
        table.show_cursor = False
        empty_cells: tuple[str, ...] = ("No jobs in queue",) + ("",) * (
            len(COLUMNS) - 1
        )
        table.add_row(*empty_cells, key=_PLACEHOLDER_ROW_KEY)

    def _build_row(self, job: JobInfo) -> tuple[Text | str, ...]:
        if not self._is_selectable_job(job):
            return tuple(
                self._inactive_cell(value)
                for value in (
                    job.job_id,
                    job.name,
                    job.user,
                    job.partition,
                    job.state,
                    job.time,
                    job.nodes,
                    job.cpus_display,
                    job.ram_display,
                    job.gpus_display,
                    self._reason_column_value(job),
                )
            )

        state_color = color_for_state(job.state)
        return (
            job.job_id,
            job.name,
            Text(job.user, style="bold bright_green"),
            job.partition,
            Text(job.state, style=state_color),
            job.time,
            job.nodes,
            job.cpus_display,
            job.ram_display,
            job.gpus_display,
            self._reason_column_value(job),
        )

    def _is_selectable_job(self, job: JobInfo) -> bool:
        return user_matches_allowed_owner(job.user, self._allowed_user)

    def _is_selectable_row(self, row_index: int) -> bool:
        return 0 <= row_index < len(self._jobs) and row_index in self._selectable_rows

    def _selectable_row_for_job_id(self, job_id: str | None) -> int | None:
        if job_id is None:
            return None

        for index, job in enumerate(self._jobs):
            if job.job_id == job_id and self._is_selectable_job(job):
                return index
        return None

    def _nearest_selectable_row(self, row_index: int) -> int | None:
        if not self._selectable_rows:
            return None

        if self._last_selectable_row is not None:
            if row_index > self._last_selectable_row:
                for selectable_row in self._selectable_rows:
                    if selectable_row > row_index:
                        return selectable_row
                return self._last_selectable_row
            if row_index < self._last_selectable_row:
                for selectable_row in reversed(self._selectable_rows):
                    if selectable_row < row_index:
                        return selectable_row
                return self._last_selectable_row

        return min(
            self._selectable_rows,
            key=lambda selectable_row: abs(selectable_row - row_index),
        )

    def _move_cursor_to_row(self, row_index: int, *, scroll: bool = True) -> None:
        table = self.query_one(DataTable)
        self._is_adjusting_cursor = True
        try:
            table.show_cursor = True
            table.move_cursor(row=row_index, animate=False, scroll=scroll)
        finally:
            self._is_adjusting_cursor = False
        self._remember_selectable_row(row_index)

    def _remember_selectable_row(self, row_index: int) -> None:
        if not self._is_selectable_row(row_index):
            return

        self._last_selectable_row = row_index
        self._selected_job_id = self._jobs[row_index].job_id

    @staticmethod
    def _inactive_cell(value: str) -> Text:
        return Text(value, style=_NON_SELECTABLE_ROW_STYLE)
