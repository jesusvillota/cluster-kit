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
from ..ownership import user_matches_allowed_owner  # noqa: E402

_RUNNING_OR_COMPLETING_STATES = {"R", "RUNNING", "CG", "COMPLETING"}
_EMPTY_MESSAGE = "No jobs in queue. Refresh to load active jobs."
_REFRESHING_MESSAGE = "Refreshing queue…"
_NON_SELECTABLE_JOB_STYLE = "grey62"


class PhoneQueueSelector(Widget):
    """Portrait-first queue list with tap-friendly selection state."""

    DEFAULT_CSS = """
    PhoneQueueSelector {
        height: 1fr;
        padding: 1 1;
    }

    PhoneQueueSelector #phone-queue-hint {
        height: 0;
        color: $text-muted;
        margin: 0;
    }

    PhoneQueueSelector #phone-queue-empty {
        height: 2;
        color: $warning;
        text-style: italic;
        content-align: center middle;
        text-align: center;
    }

    PhoneQueueSelector #phone-queue-list {
        height: 1fr;
        padding: 0;
    }

    /* Card-like option rows: taller, padded, clear selected highlight. */
    PhoneQueueSelector OptionList > Option {
        height: auto;
        min-height: 5;
        padding: 1 2;
    }

    PhoneQueueSelector OptionList > Option.--highlight {
        background: $primary-darken-2;
        text-style: bold;
    }
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._jobs: list[JobInfo] = []
        self._jobs_by_id: dict[str, JobInfo] = {}
        self._selected_job_id: str | None = None
        self._loading = False
        self._has_jobs = False
        self._allowed_user = ""
        self._is_adjusting_highlight = False

    def compose(self) -> ComposeResult:
        yield Static(_EMPTY_MESSAGE, id="phone-queue-empty")
        yield OptionList(id="phone-queue-list")

    def on_mount(self) -> None:
        self.query_one("#phone-queue-empty", Static).display = False

    def refresh_data(
        self, jobs: list[JobInfo], current_user: str = ""
    ) -> None:
        self._loading = False
        previous_selected_job_id = self._selected_job_id
        self._jobs = list(jobs)
        self._jobs_by_id = {job.job_id: job for job in self._jobs}
        self._has_jobs = bool(self._jobs)
        self._allowed_user = current_user

        option_list = self.query_one(OptionList)
        option_list.clear_options()

        if not self._jobs:
            self._selected_job_id = None
            self._render_empty_state(_EMPTY_MESSAGE)
            option_list.add_options([self._make_placeholder_option(_EMPTY_MESSAGE)])
            option_list.highlighted = None
            return

        self._hide_empty_state()
        option_list.add_options(
            [self._render_job(job, current_user) for job in self._jobs]
        )
        for index, job in enumerate(self._jobs):
            if not self._is_selectable_job(job):
                option_list.disable_option_at_index(index)

        selectable_job_ids = {
            job.job_id for job in self._jobs if self._is_selectable_job(job)
        }
        if previous_selected_job_id not in selectable_job_ids:
            self._selected_job_id = self._first_selectable_job_id()
        else:
            self._selected_job_id = previous_selected_job_id

        selected_index = self._selected_index()
        self._set_highlight(selected_index)

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
        if job is None or not self._is_selectable_job(job):
            return None

        return SelectedJob.from_job_info(job)

    @property
    def has_jobs(self) -> bool:
        return self._has_jobs

    @property
    def has_selectable_jobs(self) -> bool:
        return self._first_selectable_job_id() is not None

    @property
    def selection_unavailable_reason(self) -> str:
        if self.has_selectable_jobs and self._allowed_user:
            return f"Select a job owned by {self._allowed_user}"
        if self._has_jobs and self._allowed_user:
            return f"Only jobs owned by {self._allowed_user} can be selected"
        return "No job selected"

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
            if job.job_id == self._selected_job_id and self._is_selectable_job(job):
                return index
        return None

    def _sync_selected_job(self, option_index: int) -> None:
        if self._is_adjusting_highlight or not 0 <= option_index < len(self._jobs):
            return

        job = self._jobs[option_index]
        if self._is_selectable_job(job):
            self._selected_job_id = job.job_id
            return

        target_index = self._nearest_selectable_index(option_index)
        self._selected_job_id = (
            self._jobs[target_index].job_id if target_index is not None else None
        )
        self._set_highlight(target_index)

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
    def _render_job(job: JobInfo, current_user: str = "") -> Group:
        is_current_user = user_matches_allowed_owner(job.user, current_user)
        base_style = "" if is_current_user else _NON_SELECTABLE_JOB_STYLE
        title = Text()
        title_style = (
            "bold cyan"
            if is_current_user
            else f"bold {_NON_SELECTABLE_JOB_STYLE}"
        )
        title.append(job.job_id, style=title_style)
        title.append("  ")
        title.append(
            job.name or "Unnamed job",
            style="bold" if is_current_user else f"bold {_NON_SELECTABLE_JOB_STYLE}",
        )

        user_style = (
            "bold bright_green"
            if is_current_user
            else _NON_SELECTABLE_JOB_STYLE
        )

        summary = Text(style=base_style or "dim")
        state_style = (
            color_for_state(job.state)
            if is_current_user
            else _NON_SELECTABLE_JOB_STYLE
        )
        summary.append(job.state or "?", style=state_style)
        summary.append(" | ")
        summary.append(job.user or "?", style=user_style)
        summary.append(" | ")
        summary.append(job.partition or "?")
        summary.append(" | ")
        summary.append(job.time or "-")

        detail = Text(style=base_style or "dim")
        if job.state.strip().upper() in _RUNNING_OR_COMPLETING_STATES:
            detail.append(f"{job.cpus_display} CPU")
            detail.append(" | ")
            detail.append(job.ram_display)
            detail.append(" | ")
            detail.append(f"{job.gpus_display} GPU")
            if job.node_list.strip():
                detail.append(" | ")
                detail.append(job.node_list)
        elif job.reason:
            detail.append(job.reason)
        else:
            detail.append("No extra details")

        # Blank separator makes each option read as a distinct card.
        separator = Text(" ")
        return Group(title, summary, detail, separator)

    def _is_selectable_job(self, job: JobInfo) -> bool:
        return user_matches_allowed_owner(job.user, self._allowed_user)

    def _first_selectable_job_id(self) -> str | None:
        for job in self._jobs:
            if self._is_selectable_job(job):
                return job.job_id
        return None

    def _nearest_selectable_index(self, option_index: int) -> int | None:
        selectable_indices = [
            index
            for index, job in enumerate(self._jobs)
            if self._is_selectable_job(job)
        ]
        if not selectable_indices:
            return None

        selected_index = self._selected_index()
        if selected_index is not None:
            if option_index > selected_index:
                for index in selectable_indices:
                    if index > option_index:
                        return index
                return selected_index
            if option_index < selected_index:
                for index in reversed(selectable_indices):
                    if index < option_index:
                        return index
                return selected_index

        return min(selectable_indices, key=lambda index: abs(index - option_index))

    def _set_highlight(self, option_index: int | None) -> None:
        option_list = self.query_one(OptionList)
        self._is_adjusting_highlight = True
        try:
            option_list.highlighted = option_index
        finally:
            self._is_adjusting_highlight = False


__all__ = ["PhoneQueueSelector"]
