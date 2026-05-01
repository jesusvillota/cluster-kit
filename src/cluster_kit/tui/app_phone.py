"""
uv run cluster_kit/tui/app_phone.py

uv run cluster_kit/tui/app_phone.py \
    --refresh 60 \
    --all-users

CLI Arguments:
--refresh (default: 60) {squeue auto-refresh interval in seconds}
--all-users (default: true) {Show all users' jobs, not just the current user}

LLM-optimized description: Runs a phone-oriented Textual terminal UI for cluster
monitoring with explicit tap-first controls for queue, available resources, logs,
refresh, cancel, stderr toggling, manual log loading, and code sync. Reuses the
shared ClusterTUIController plus the phone queue selector, AvailableResourcesTable,
LogViewer, and modal screens while avoiding the desktop footer-driven shell. Reads
the same SLURM queue, resource, SSH, and log-discovery backends as the desktop app
but keeps a portrait-first, button-led layout.
"""

# pyright: reportMissingImports=false

from __future__ import annotations

import argparse
import os
from datetime import datetime

from textual import work  # type: ignore[reportMissingImports]
from textual.app import App, ComposeResult  # type: ignore[reportMissingImports]
from textual.binding import Binding  # type: ignore[reportMissingImports]
from textual.containers import (  # type: ignore[reportMissingImports]
    Grid,
    Vertical,
)
from textual.widgets import (  # type: ignore[reportMissingImports]
    Button,
    Input,
    Static,
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
from cluster_kit.tui.styles import PHONE_CSS
from cluster_kit.tui.widgets.available_resources_table import (
    AvailableResourcesTable,
)
from cluster_kit.tui.widgets.log_viewer import LogViewer
from cluster_kit.tui.widgets.phone_queue_selector import (
    PhoneQueueSelector,
)
from cluster_kit.tui.widgets.status_bar import (
    ConnectionStatus,
)

PHONE_VIEWS = ("queue", "available", "logs")


class PhoneClusterTUI(App[None]):
    CSS = PHONE_CSS
    BINDINGS = [Binding("q", "quit", "Quit")]

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
        self.active_view = "queue"
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
        with Vertical(id="phone-shell"):
            yield Static("Cluster TUI Phone", id="phone-title")
            with Grid(id="phone-nav-row", classes="phone-row"):
                yield Button("Queue", id="phone-nav-queue", classes="active-view")
                yield Button("Available", id="phone-nav-available")
                yield Button("Logs", id="phone-nav-logs")
            with Grid(id="phone-action-row-primary", classes="phone-row"):
                yield Button("Refresh", variant="primary", id="phone-action-refresh")
                yield Button("Selected Logs", id="phone-action-selected-logs")
                yield Button("Cancel Job", variant="error", id="phone-action-cancel")
            with Grid(id="phone-action-row-secondary", classes="phone-row"):
                yield Button("Manual Logs", id="phone-action-manual-logs")
                yield Button("Stdout/Err", id="phone-action-toggle-stderr")
                yield Button("Sync Code", id="phone-action-sync")
            yield ConnectionStatus(id="phone-status")
            with Vertical(id="phone-views"):
                with Vertical(id="phone-queue-view", classes="phone-view"):
                    yield PhoneQueueSelector()
                with Vertical(id="phone-available-view", classes="phone-view"):
                    yield AvailableResourcesTable(compact=True)
                with Vertical(id="phone-logs-view", classes="phone-view"):
                    yield LogViewer(compact=True)

    def on_mount(self) -> None:
        self._set_active_view("queue")
        self._test_connection_on_mount()
        self.set_interval(self.refresh_interval, self.action_refresh)
        self.action_refresh()

    def _set_active_view(self, view: str) -> None:
        if view not in PHONE_VIEWS:
            return

        self.active_view = view
        for view_name in PHONE_VIEWS:
            self.query_one(f"#phone-{view_name}-view").display = view_name == view
            self.query_one(f"#phone-nav-{view_name}", Button).set_class(
                view_name == view,
                "active-view",
            )

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
        self.query_one(PhoneQueueSelector).set_loading(value)

    def _update_data(
        self,
        jobs: list[JobInfo],
        availability_rows: list[AvailableResourceRow],
        connected: bool,
        job_count: int,
    ) -> None:
        queue_table = self.query_one(PhoneQueueSelector)
        available_resources_table = self.query_one(AvailableResourcesTable)
        status_bar = self.query_one(ConnectionStatus)
        queue_table.refresh_data(jobs, get_cluster_user())
        available_resources_table.refresh_data(availability_rows)
        queue_table.set_loading(False)
        status_bar.update_status(connected, job_count, datetime.now())
        self._update_job_action_enabled_state()

    def _update_queue_stale(
        self, availability_rows: list[AvailableResourceRow]
    ) -> None:
        queue_table = self.query_one(PhoneQueueSelector)
        available_resources_table = self.query_one(AvailableResourcesTable)
        status_bar = self.query_one(ConnectionStatus)
        available_resources_table.refresh_data(availability_rows)
        queue_table.set_loading(False)
        status_bar.mark_stale()
        self._update_job_action_enabled_state()

    def _get_selected_job(self) -> SelectedJob | None:
        return self.query_one(PhoneQueueSelector).get_selected_job()

    def _update_job_action_enabled_state(self) -> None:
        queue_selector = self.query_one(PhoneQueueSelector)
        log_viewer = self.query_one(LogViewer)
        has_selected_job = self._get_selected_job() is not None
        self.query_one(
            "#phone-action-selected-logs", Button
        ).disabled = not has_selected_job
        self.query_one("#phone-action-cancel", Button).disabled = not has_selected_job
        self.query_one("#phone-action-toggle-stderr", Button).disabled = (
            log_viewer.current_job_id is None or log_viewer.current_file is None
        )
        tooltip = None if queue_selector.has_jobs else "No job selected"
        self.query_one("#phone-action-selected-logs", Button).tooltip = tooltip
        self.query_one("#phone-action-cancel", Button).tooltip = tooltip

    def _switch_to_logs(self, job_id: str, log_file: LogFile) -> None:
        self._set_active_view("logs")
        log_viewer = self.query_one(LogViewer)
        log_viewer.show_log(job_id, log_file)
        self._update_job_action_enabled_state()
        try:
            self.query_one("#job-id-input", Input).value = job_id
        except Exception:
            pass

    def action_show_view(self, view: str) -> None:
        self._set_active_view(view)

    def action_cancel_job(self) -> None:
        selected_job, message = self._controller.require_selected_job(
            self._get_selected_job()
        )
        if selected_job is None:
            self.notify(message or "No job selected")
            return
        self.push_screen(
            ConfirmCancelScreen(
                selected_job.job_id,
                selected_job.name,
                compact=True,
            ),
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

    def action_toggle_stderr(self) -> None:
        self._set_active_view("logs")
        log_viewer = self.query_one(LogViewer)
        if log_viewer.current_job_id is None or log_viewer.current_file is None:
            self.notify("Load a log first")
            return
        log_viewer.toggle_stderr()
        self._update_job_action_enabled_state()

    def action_sync_code(self) -> None:
        self.push_screen(
            self._controller.create_sync_screen(
                qa_safe_mode=self.qa_safe_mode,
                compact=True,
            )
        )

    def action_job_logs(self) -> None:
        self._set_active_view("logs")
        self.query_one(LogViewer).reset_log_view()
        self.query_one("#job-id-input", Input).focus()

    def on_log_viewer_log_job_requested(
        self, message: LogViewer.LogJobRequested
    ) -> None:
        self._show_logs_for_job(message.job_id)

    def on_option_list_option_highlighted(self, _message) -> None:
        self._update_job_action_enabled_state()

    def on_option_list_option_selected(self, _message) -> None:
        self._update_job_action_enabled_state()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "phone-nav-queue":
            self.action_show_view("queue")
        elif button_id == "phone-nav-available":
            self.action_show_view("available")
        elif button_id == "phone-nav-logs":
            self.action_show_view("logs")
        elif button_id == "phone-action-refresh":
            self.action_refresh()
        elif button_id == "phone-action-selected-logs":
            self.action_view_logs()
        elif button_id == "phone-action-cancel":
            self.action_cancel_job()
        elif button_id == "phone-action-manual-logs":
            self.action_job_logs()
        elif button_id == "phone-action-toggle-stderr":
            self.action_toggle_stderr()
        elif button_id == "phone-action-sync":
            self.action_sync_code()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cluster TUI Phone — portrait-first SLURM monitoring terminal interface"
        ),
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
        default=True,
        help="Show all users' jobs, not just current user's",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app = PhoneClusterTUI(refresh_interval=args.refresh, all_users=args.all_users)
    app.run()
