"""Shared refresh lifecycle for the desktop and phone Textual shells."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Protocol, cast

from textual import work
from textual.app import App

from cluster_kit.config import get_cluster_host, get_cluster_user
from cluster_kit.sync.mirror import read_mirror_state
from cluster_kit.tui.backend.available_resources import parse_sinfo_output
from cluster_kit.tui.backend.job_actions import (
    QA_SAFE_MODE_ENV_VAR,
    cancel_job,
    is_qa_safe_mode_enabled,
)
from cluster_kit.tui.backend.log_discovery import (
    discover_log_files,
    parse_log_files,
)
from cluster_kit.tui.backend.pc_jobs import PcJobsResult, fetch_pc_jobs
from cluster_kit.tui.backend.queue_parser import JobInfo, parse_squeue_output
from cluster_kit.tui.backend.snapshot import fetch_cluster_snapshot
from cluster_kit.tui.controller import ClusterTUIController, RefreshOutcome
from cluster_kit.tui.screens import SyncScreen
from cluster_kit.tui.widgets.available_resources_table import (
    AvailableResourcesTable,
)
from cluster_kit.tui.widgets.pc_jobs_table import PcJobsTable
from cluster_kit.tui.widgets.status_bar import ConnectionStatus, MirrorStatus


class _QueueView(Protocol):
    def refresh_data(self, jobs: list[JobInfo], current_user: str = "") -> None: ...


class ClusterTUIBase(App[None]):
    """Common bounded refresh orchestration for all cluster TUI layouts."""

    TITLE = "Cluster Kit"
    AUTO_START_REFRESH = True

    def __init__(
        self,
        refresh_interval: int = 60,
        all_users: bool = True,
        qa_safe_mode: bool | None = None,
    ) -> None:
        super().__init__()
        self.refresh_interval = refresh_interval
        self.all_users = all_users
        self.cluster_user = get_cluster_user()
        self.sub_title = f"{self.cluster_user}@{get_cluster_host()}"
        self.qa_safe_mode = (
            is_qa_safe_mode_enabled(os.environ.get(QA_SAFE_MODE_ENV_VAR))
            if qa_safe_mode is None
            else qa_safe_mode
        )
        self._refresh_in_flight = False
        self._last_queue_refresh: datetime | None = None
        self._last_job_count = 0
        self._controller = ClusterTUIController(
            fetch_cluster_snapshot=lambda **kwargs: fetch_cluster_snapshot(**kwargs),
            parse_squeue_output=lambda raw: parse_squeue_output(raw),
            parse_sinfo_output=lambda raw: parse_sinfo_output(raw),
            discover_log_files=lambda job_id: discover_log_files(job_id),
            parse_log_files=lambda raw: parse_log_files(raw),
            cancel_job=lambda job_id, *, qa_safe_mode: cancel_job(
                job_id, qa_safe_mode=qa_safe_mode
            ),
            sync_screen_factory=SyncScreen,
        )

    def on_mount(self) -> None:
        self.query_one(MirrorStatus).update_from_state(read_mirror_state())
        if not self.AUTO_START_REFRESH:
            return
        self.set_interval(self.refresh_interval, self.action_refresh)
        self.action_refresh()
        self.set_interval(self.refresh_interval * 3, self._refresh_pc_jobs)
        self._refresh_pc_jobs()

    def _queue_view(self) -> _QueueView:
        """Return the shell-specific queue widget."""

        raise NotImplementedError

    def _after_cluster_update(self) -> None:
        """Allow a shell to update controls after queue state changes."""

    def action_refresh(self) -> None:
        """Start at most one cluster snapshot fetch at a time."""

        if self._refresh_in_flight:
            return
        self._refresh_in_flight = True
        self.query_one(ConnectionStatus).mark_refreshing(
            self._last_queue_refresh,
            self._last_job_count,
        )
        self._refresh_cluster_worker()

    @work(thread=True, group="cluster_refresh")
    def _refresh_cluster_worker(self) -> None:  # type: ignore[return]
        try:
            outcome = self._controller.refresh_queue_state(
                all_users=self.all_users,
                cluster_user=self.cluster_user,
            )
        except Exception as exc:
            self.log.error(f"Cluster refresh failed: {exc!r}")
            outcome = RefreshOutcome(
                jobs=None,
                availability_rows=None,
                queue_error=str(exc) or type(exc).__name__,
                resources_error=str(exc) or type(exc).__name__,
            )
        self.call_from_thread(self._apply_cluster_refresh, outcome)

    def _apply_cluster_refresh(self, outcome: RefreshOutcome) -> None:
        try:
            now = datetime.now()
            if outcome.jobs is not None:
                self._queue_view().refresh_data(outcome.jobs, self.cluster_user)
                self._last_queue_refresh = now
                self._last_job_count = len(outcome.jobs)
            if outcome.availability_rows is not None:
                self.query_one(AvailableResourcesTable).refresh_data(
                    outcome.availability_rows
                )

            status = self.query_one(ConnectionStatus)
            if outcome.fully_successful:
                status.update_status(True, self._last_job_count, now)
            elif outcome.jobs is not None:
                status.mark_partial(
                    "Node data stale",
                    outcome.resources_error,
                    self._last_job_count,
                    now,
                )
            else:
                status.mark_stale(
                    outcome.queue_error or outcome.resources_error,
                    self._last_queue_refresh,
                    self._last_job_count,
                )
            self._after_cluster_update()
        finally:
            self._refresh_in_flight = False

    @work(thread=True, exclusive=True, group="pc_jobs")
    def _refresh_pc_jobs(self) -> None:  # type: ignore[return]
        result = fetch_pc_jobs()
        self.call_from_thread(self._update_pc_jobs, result)

    def _update_pc_jobs(self, result: PcJobsResult) -> None:
        self.query_one(PcJobsTable).refresh_data(result.jobs, result.error)
        self.query_one(MirrorStatus).update_from_state(read_mirror_state())

    def _typed_queue_view(self, widget: object) -> _QueueView:
        """Narrow a concrete shell widget to the shared queue protocol."""

        return cast(_QueueView, widget)


__all__ = ["ClusterTUIBase"]
