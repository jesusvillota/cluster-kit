"""Phone-friendly queue selector for the Cluster TUI phone shell."""

# pyright: reportMissingImports=false

from __future__ import annotations

from rich.console import Group
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import OptionList, Static

from ..backend.queue_parser import (  # noqa: E402
    JobInfo,
    color_for_state,
)
from ..controller import SelectedJob  # noqa: E402

_RUNNING_OR_COMPLETING_STATES = {"R", "RUNNING", "CG", "COMPLETING"}
_EMPTY_MESSAGE = "No jobs in queue. Refresh to load active jobs."
_REFRESHING_MESSAGE = "Refreshing queue…"


class PhoneQueueSelector(Widget):
    """Portrait-first queue list with tap-friendly selection state."""

    DEFAULT_CSS = """
    PhoneQueueSelector {
        height: 1fr;
        padding: 0 1;
    }

    PhoneQueueSelector #phone-queue-hint {
        height: 2;
        color: $text-muted;
    }

    PhoneQueueSelector #phone-queue-empty {
        height: 2;
        color: $warning;
        text-style: italic;
    }

    PhoneQueueSelector #phone-queue-list {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._jobs: list[JobInfo] = []
        self._jobs_by_id: dict[str, JobInfo] = {}
        self._selected_job_id: str | None = None
        self._loading = False
        self._has_jobs = False

    def compose(self) -> ComposeResult:
        yield Static(
            "Tap a job to select it. Selected Logs and Cancel use that selection.",
            id="phone-queue-hint",
        )
        yield Static(_EMPTY_MESSAGE, id="phone-queue-empty")
        yield OptionList(id="phone-queue-list")

    def on_mount(self) -> None:
        self.query_one("#phone-queue-empty", Static).display = False

    def refresh_data(self, jobs: list[JobInfo]) -> None:
        self._loading = False
        self._jobs = list(jobs)
        self._jobs_by_id = {job.job_id: job for job in self._jobs}
        self._has_jobs = bool(self._jobs)

        option_list = self.query_one(OptionList)
        option_list.clear_options()

        if not self._jobs:
            self._selected_job_id = None
            self._render_empty_state(_EMPTY_MESSAGE)
            option_list.add_options([self._make_placeholder_option(_EMPTY_MESSAGE)])
            option_list.highlighted = None
            return

        self._hide_empty_state()
        option_list.add_options([self._render_job(job) for job in self._jobs])

        if self._selected_job_id not in self._jobs_by_id:
            self._selected_job_id = self._jobs[0].job_id

        selected_index = self._selected_index()
        option_list.highlighted = selected_index if selected_index is not None else 0

    def set_loading(self, value: bool) -> None:
        self._loading = value
        if self._jobs:
            return

        empty_message = _REFRESHING_MESSAGE if value else _EMPTY_MESSAGE
        self._render_empty_state(empty_message)
        option_list = self.query_one(OptionList)
        option_list.clear_options()
        option_list.add_options([self._make_placeholder_option(empty_message)])
        option_list.highlighted = None

    def get_selected_job(self) -> SelectedJob | None:
        if self._selected_job_id is None:
            return None

        job = self._jobs_by_id.get(self._selected_job_id)
        if job is None:
            return None

        return SelectedJob.from_job_info(job)

    @property
    def has_jobs(self) -> bool:
        return self._has_jobs

    def on_option_list_option_highlighted(
        self, message: OptionList.OptionHighlighted
    ) -> None:
        self._sync_selected_job(message.option_index)

    def on_option_list_option_selected(
        self, message: OptionList.OptionSelected
    ) -> None:
        self._sync_selected_job(message.option_index)

    def _selected_index(self) -> int | None:
        if self._selected_job_id is None:
            return None

        for index, job in enumerate(self._jobs):
            if job.job_id == self._selected_job_id:
                return index
        return None

    def _sync_selected_job(self, option_index: int) -> None:
        if 0 <= option_index < len(self._jobs):
            self._selected_job_id = self._jobs[option_index].job_id

    def _render_empty_state(self, message: str) -> None:
        empty_notice = self.query_one("#phone-queue-empty", Static)
        empty_notice.update(message)
        empty_notice.display = True

    def _hide_empty_state(self) -> None:
        self.query_one("#phone-queue-empty", Static).display = False

    @staticmethod
    def _make_placeholder_option(message: str) -> Text:
        return Text(message, style="dim", no_wrap=True)

    @staticmethod
    def _render_job(job: JobInfo) -> Group:
        title = Text()
        title.append(job.job_id, style="bold cyan")
        title.append("  ")
        title.append(job.name or "Unnamed job", style="bold")

        summary = Text(style="dim")
        summary.append(job.state or "?", style=color_for_state(job.state))
        summary.append(" • ")
        summary.append(job.user or "?")
        summary.append(" • ")
        summary.append(job.partition or "?")
        summary.append(" • ")
        summary.append(job.time or "—")

        detail = Text(style="dim")
        if job.state.strip().upper() in _RUNNING_OR_COMPLETING_STATES:
            detail.append(f"{job.cpus_display} CPU")
            detail.append(" • ")
            detail.append(job.ram_display)
            detail.append(" • ")
            detail.append(f"{job.gpus_display} GPU")
            if job.node_list.strip():
                detail.append(" • ")
                detail.append(job.node_list)
        elif job.reason:
            detail.append(job.reason)
        else:
            detail.append("No extra details")

        return Group(title, summary, detail)


__all__ = ["PhoneQueueSelector"]
