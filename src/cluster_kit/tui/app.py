"""
uv run cluster_kit/tui/app.py

uv run cluster_kit/tui/app.py \
    --refresh 60 \
    --all-users

CLI Arguments:
--refresh (default: 60) {squeue auto-refresh interval in seconds}
--all-users (default: true) {Show all users' jobs, not just the current user}

LLM-optimized description: Runs a Textual terminal UI for cluster monitoring
with 3 tabs: queue, available resources, and logs. Reads SLURM queue state,
fixed-node capacity summaries, and remote log-file discovery results over SSH.
Wires data into QueueTable, AvailableResourcesTable, LogViewer, and
ConnectionStatus widgets. Supports periodic queue refresh, job cancellation
confirmation, and one-keystroke navigation without blocking the Textual loop.
"""

# pyright: reportMissingImports=false

from __future__ import annotations

import argparse
import os
from datetime import datetime

from textual import work  # type: ignore[reportMissingImports]
from textual.app import App, ComposeResult  # type: ignore[reportMissingImports]
from textual.binding import Binding  # type: ignore[reportMissingImports]
from textual.widgets import (  # type: ignore[reportMissingImports]
    Footer,
    Header,
    TabbedContent,
    TabPane,
)

from cluster_kit.config import get_cluster_user
from cluster_kit.tui.backend.available_resources import (
    AvailableResourceRow,
    fetch_available_resources,
)
from cluster_kit.tui.backend.job_actions import (
    QA_SAFE_MODE_ENV_VAR,
    cancel_job,
    is_qa_safe_mode_enabled,
)
from cluster_kit.tui.backend.log_discovery import (
    LogFile,
    discover_log_files,
    parse_log_files,
)
from cluster_kit.tui.backend.queue_parser import (
    JobInfo,
    fetch_queue,
    parse_squeue_output,
)
from cluster_kit.tui.backend.ssh import test_connection
from cluster_kit.tui.controller import (
    ClusterTUIController,
    RefreshFailure,
    RefreshSuccess,
    SelectedJob,
)
from cluster_kit.tui.screens import (
    ConfirmCancelScreen,
    SyncScreen,
)
from cluster_kit.tui.styles import MAIN_CSS
from cluster_kit.tui.widgets.available_resources_table import (
    AvailableResourcesTable,
)
from cluster_kit.tui.widgets.log_viewer import LogViewer
from cluster_kit.tui.widgets.queue_table import (
    JobSelected,
    QueueTable,
)
from cluster_kit.tui.widgets.status_bar import (
    ConnectionStatus,
)


class ClusterTUI(App[None]):
    CSS = MAIN_CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "show_tab('queue')", "Queue"),
        Binding("2", "show_tab('available')", "Available"),
        Binding("3", "show_tab('logs')", "Logs"),
        Binding("r", "refresh", "Refresh"),
        Binding("c", "cancel_job", "Cancel"),
        Binding("l", "view_logs", "Logs"),
        Binding("e", "toggle_stderr", "Toggle .err"),
        Binding("s", "sync_code", "Sync Code"),
        Binding("j", "job_logs", "Job Logs"),
        Binding("y", "copy_log", "Copy"),
    ]

    def __init__(
        self,
        refresh_interval: int = 60,
        all_users: bool = True,
        qa_safe_mode: bool | None = None,
    ) -> None:
        super().__init__()
        self.refresh_interval = refresh_interval
        self.all_users = all_users
        self.qa_safe_mode = (
            is_qa_safe_mode_enabled(os.environ.get(QA_SAFE_MODE_ENV_VAR))
            if qa_safe_mode is None
            else qa_safe_mode
        )
        self._controller = ClusterTUIController(
            fetch_queue=lambda **kwargs: fetch_queue(**kwargs),
            parse_squeue_output=lambda raw: parse_squeue_output(raw),
            fetch_available_resources=lambda: fetch_available_resources(),
            discover_log_files=lambda job_id: discover_log_files(job_id),
            parse_log_files=lambda raw: parse_log_files(raw),
            cancel_job=lambda job_id, *, qa_safe_mode: cancel_job(
                job_id, qa_safe_mode=qa_safe_mode
            ),
            sync_screen_factory=SyncScreen,
        )

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="queue"):
            with TabPane("Queue", id="queue"):
                yield QueueTable()
                yield ConnectionStatus()
            with TabPane("Available", id="available"):
                yield AvailableResourcesTable()
            with TabPane("Logs", id="logs"):
                yield LogViewer()
        yield Footer()

    def on_mount(self) -> None:
        self._test_connection_on_mount()
        self.set_interval(self.refresh_interval, self.action_refresh)
        self.action_refresh()

    @work(thread=True)
    def _test_connection_on_mount(self) -> None:  # type: ignore[return]
        result = test_connection()
        if result.success:
            self.call_from_thread(self._mark_connected)
            return
        self.call_from_thread(
            self._mark_connection_error,
            result.error_message or result.stderr or "SSH test failed",
        )

    def _mark_connected(self) -> None:
        self.query_one(ConnectionStatus).mark_connected()

    def _mark_connection_error(self, message: str) -> None:
        self.query_one(ConnectionStatus).mark_error(message)

    @work(thread=True, exclusive=True)
    def action_refresh(self) -> None:  # type: ignore[return]
        self.call_from_thread(self._set_queue_loading, True)

        outcome = self._controller.refresh_queue_state(
            all_users=self.all_users,
            cluster_user=get_cluster_user(),
        )
        if isinstance(outcome, RefreshSuccess):
            self.call_from_thread(
                self._update_data,
                outcome.jobs,
                outcome.availability_rows,
                True,
                outcome.job_count,
            )
            return

        assert isinstance(outcome, RefreshFailure)
        self.call_from_thread(self._update_queue_stale, outcome.availability_rows)

    def _set_queue_loading(self, value: bool) -> None:
        self.query_one(QueueTable).set_loading(value)

    def _update_data(
        self,
        jobs: list[JobInfo],
        availability_rows: list[AvailableResourceRow],
        connected: bool,
        job_count: int,
    ) -> None:
        queue_table = self.query_one(QueueTable)
        available_resources_table = self.query_one(AvailableResourcesTable)
        status_bar = self.query_one(ConnectionStatus)
        queue_table.refresh_data(jobs, get_cluster_user())
        available_resources_table.refresh_data(availability_rows)
        queue_table.set_loading(False)
        status_bar.update_status(connected, job_count, datetime.now())

    def _update_queue_stale(
        self, availability_rows: list[AvailableResourceRow]
    ) -> None:
        queue_table = self.query_one(QueueTable)
        available_resources_table = self.query_one(AvailableResourcesTable)
        status_bar = self.query_one(ConnectionStatus)
        available_resources_table.refresh_data(availability_rows)
        queue_table.set_loading(False)
        status_bar.mark_stale()

    def action_cancel_job(self) -> None:
        selected_job, message = self._controller.require_selected_job(
            self._get_selected_job()
        )
        if selected_job is None:
            self.notify(message or "No job selected")
            return
        self.push_screen(
            ConfirmCancelScreen(selected_job.job_id, selected_job.name),
            lambda confirmed: self._on_cancel_confirmed(selected_job.job_id, confirmed),
        )

    def _on_cancel_confirmed(self, job_id: str, confirmed: bool) -> None:
        if confirmed:
            self._do_cancel(job_id)

    @work(thread=True)
    def _do_cancel(self, job_id: str) -> None:  # type: ignore[return]
        self._controller.cancel_selected_job(job_id, qa_safe_mode=self.qa_safe_mode)
        self.call_from_thread(self.action_refresh)

    def action_view_logs(self) -> None:
        selected_job, message = self._controller.require_selected_job(
            self._get_selected_job()
        )
        if selected_job is None:
            self.notify(message or "No job selected")
            return
        self._show_logs_for_job(selected_job.job_id)

    @work(thread=True)
    def _show_logs_for_job(self, job_id: str) -> None:  # type: ignore[return]
        route, message = self._controller.resolve_log_route(job_id)
        if route is None:
            self.call_from_thread(
                self.notify,
                message or f"Failed to discover logs for job {job_id}",
            )
            return

        self.call_from_thread(self._switch_to_logs, route.job_id, route.log_file)

    def _get_selected_job(self) -> SelectedJob | None:
        selected = self.query_one(QueueTable).get_selected_job()
        if selected is None:
            return None
        return SelectedJob.from_job_info(selected)

    def _switch_to_logs(self, job_id: str, log_file: LogFile) -> None:
        self.query_one(TabbedContent).active = "logs"
        log_viewer = self.query_one(LogViewer)
        log_viewer.show_log(job_id, log_file)
        try:
            self.query_one("#job-id-input").value = job_id
        except Exception:
            pass

    def action_toggle_stderr(self) -> None:
        self.query_one(LogViewer).toggle_stderr()

    def action_copy_log(self) -> None:
        self.query_one(LogViewer).copy_log_content()

    def on_job_selected(self, message: JobSelected) -> None:
        self._show_logs_for_job(message.job_info.job_id)

    def action_show_tab(self, tab: str) -> None:
        tabs = self.query_one(TabbedContent)
        if tabs.active == "logs" or tab != "logs":
            self.query_one(LogViewer).stop_follow()
        tabs.active = tab

    def action_sync_code(self) -> None:
        self.push_screen(
            self._controller.create_sync_screen(qa_safe_mode=self.qa_safe_mode)
        )

    def action_job_logs(self) -> None:
        self.query_one(TabbedContent).active = "logs"
        self.query_one(LogViewer).reset_log_view()
        self.query_one("#job-id-input").focus()

    def on_log_viewer_log_job_requested(
        self, message: LogViewer.LogJobRequested
    ) -> None:
        self._show_logs_for_job(message.job_id)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cluster TUI — unified SLURM monitoring terminal interface",
    )
    parser.add_argument(
        "--refresh",
        type=int,
        default=60,
        metavar="SECONDS",
        help="squeue auto-refresh interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--all-users",
        action="store_true",
        default=False,
        help="Show all users' jobs, not just current user's",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app = ClusterTUI(refresh_interval=args.refresh, all_users=args.all_users)
    app.run()
