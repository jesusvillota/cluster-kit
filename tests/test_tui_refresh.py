from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from textual.app import ComposeResult
from textual.widgets import DataTable, TabPane

from cluster_kit.tui import base as base_mod
from cluster_kit.tui.app import ClusterTUI
from cluster_kit.tui.app_phone import PhoneClusterTUI
from cluster_kit.tui.backend import snapshot as snapshot_mod
from cluster_kit.tui.backend.available_resources import (
    AvailableResourceRow,
)
from cluster_kit.tui.backend.queue_parser import JobInfo
from cluster_kit.tui.backend.snapshot import (
    ClusterSnapshotResult,
    parse_cluster_snapshot,
)
from cluster_kit.tui.backend.ssh import SSHResult
from cluster_kit.tui.base import ClusterTUIBase
from cluster_kit.tui.controller import ClusterTUIController, RefreshOutcome
from cluster_kit.tui.widgets.available_resources_table import (
    AvailableResourcesTable,
)
from cluster_kit.tui.widgets.pc_jobs_table import PcJobsTable
from cluster_kit.tui.widgets.queue_table import QueueTable
from cluster_kit.tui.widgets.status_bar import ConnectionStatus, MirrorStatus


def _job(job_id: str) -> JobInfo:
    return JobInfo(
        job_id=job_id,
        name=f"job-{job_id}",
        user="j-vill36",
        partition="cpu_large",
        state="R",
        time="00:01:00",
        nodes="1",
        reason="",
    )


def _resource_row() -> AvailableResourceRow:
    return AvailableResourceRow(
        node_name="HPCOM-01",
        node_state="mixed",
        total_cpus=72,
        total_memory_gb=256,
        total_gpus=1,
        allocated_cpus=8,
        allocated_memory_gb=32,
        allocated_gpus=0,
        available_cpus=64,
        available_memory_gb=224,
        available_gpus=1,
    )


def _framed(queue: str, queue_rc: int, resources: str, resources_rc: int) -> str:
    return (
        f"{snapshot_mod._QUEUE_BEGIN}\n{queue}\n"
        f"{snapshot_mod._QUEUE_END}:{queue_rc}\n"
        f"{snapshot_mod._RESOURCES_BEGIN}\n{resources}\n"
        f"{snapshot_mod._RESOURCES_END}:{resources_rc}\n"
    )


def _controller(snapshot: ClusterSnapshotResult) -> ClusterTUIController:
    return ClusterTUIController(
        fetch_cluster_snapshot=lambda **_kwargs: snapshot,
        parse_squeue_output=lambda raw: [_job(raw)],
        parse_sinfo_output=lambda _raw: [_resource_row()],
        discover_log_files=lambda _job_id: SSHResult(),
        parse_log_files=lambda _raw: [],
        cancel_job=lambda _job_id, **_kwargs: SSHResult(),
        sync_screen_factory=lambda *_args, **_kwargs: object(),
    )


def test_snapshot_parser_preserves_independent_command_results() -> None:
    result = parse_cluster_snapshot(_framed("17001", 0, "sinfo failed", 1))

    assert result.queue == SSHResult(stdout="17001", success=True)
    assert result.resources.success is False
    assert result.resources.error_message == "sinfo failed"


def test_snapshot_parser_reports_missing_frame_without_losing_other_frame() -> None:
    raw = (
        f"{snapshot_mod._QUEUE_BEGIN}\n17001\n"
        f"{snapshot_mod._QUEUE_END}:0\n"
    )

    result = parse_cluster_snapshot(raw)

    assert result.queue.success is True
    assert result.resources.success is False
    assert "missing resource frame" in result.resources.error_message


def test_snapshot_fetch_uses_one_ssh_call(monkeypatch: pytest.MonkeyPatch) -> None:
    run = MagicMock(
        return_value=SSHResult(stdout=_framed("17001", 0, "node", 0))
    )
    monkeypatch.setattr(snapshot_mod, "run_ssh_command", run)

    result = snapshot_mod.fetch_cluster_snapshot(user="j-vill36")

    assert result.queue.success is True
    assert result.resources.success is True
    run.assert_called_once()
    remote_command = run.call_args.args[0]
    assert "squeue" in remote_command
    assert "sinfo" in remote_command
    assert "-u j-vill36" in remote_command


def test_controller_returns_partial_outcome() -> None:
    controller = _controller(
        ClusterSnapshotResult(
            queue=SSHResult(stdout="17001"),
            resources=SSHResult(success=False, error_message="sinfo unavailable"),
        )
    )

    outcome = controller.refresh_queue_state(
        all_users=False,
        cluster_user="j-vill36",
    )

    assert outcome.jobs == [_job("17001")]
    assert outcome.availability_rows is None
    assert outcome.resources_error == "sinfo unavailable"


def test_tui_ssh_uses_configured_noninteractive_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cluster_kit.tui.backend import ssh as ssh_mod

    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    monkeypatch.setattr(ssh_mod, "get_cluster_host", lambda: "cluster-alias")
    monkeypatch.setattr(ssh_mod, "get_ssh_timeout", lambda: 12)
    monkeypatch.setattr(ssh_mod.subprocess, "run", run)

    result = ssh_mod.run_ssh_command("echo ok")

    assert result.success is True
    assert run.call_args.args[0] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=12",
        "cluster-alias",
        "echo ok",
    ]
    assert run.call_args.kwargs["timeout"] == 17


def test_tui_ssh_timeout_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    from cluster_kit.tui.backend import ssh as ssh_mod

    monkeypatch.setattr(ssh_mod, "get_cluster_host", lambda: "cluster-alias")
    monkeypatch.setattr(ssh_mod, "get_ssh_timeout", lambda: 7)
    monkeypatch.setattr(
        ssh_mod.subprocess,
        "run",
        MagicMock(side_effect=subprocess.TimeoutExpired("ssh", 12)),
    )

    result = ssh_mod.run_ssh_command("echo ok")

    assert result.success is False
    assert result.error_message == "SSH command timed out after 7s"


class _RefreshApp(ClusterTUIBase):
    AUTO_START_REFRESH = False

    def compose(self) -> ComposeResult:
        yield QueueTable()
        yield AvailableResourcesTable()
        yield PcJobsTable()
        yield ConnectionStatus()
        yield MirrorStatus()

    def on_mount(self) -> None:
        pass

    def _queue_view(self):  # type: ignore[override]
        return self._typed_queue_view(self.query_one(QueueTable))


class _DesktopLayoutApp(ClusterTUI):
    AUTO_START_REFRESH = False


class _PhoneLayoutApp(PhoneClusterTUI):
    AUTO_START_REFRESH = False


@pytest.fixture
def refresh_app_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base_mod, "get_cluster_user", lambda: "j-vill36")
    monkeypatch.setattr(base_mod, "get_cluster_host", lambda: "cluster-alias")


def test_refresh_coalesces_requests_and_keeps_stale_table_visible(
    refresh_app_config: None,
) -> None:
    async def run() -> None:
        started = Event()
        release = Event()

        class BlockingController:
            calls = 0

            def refresh_queue_state(self, **_kwargs: object) -> RefreshOutcome:
                self.calls += 1
                started.set()
                release.wait(timeout=2)
                return RefreshOutcome([_job("new")], [_resource_row()])

        controller = BlockingController()
        app = _RefreshApp()
        app._controller = controller  # type: ignore[assignment]
        async with app.run_test() as pilot:
            table_widget = app.query_one(QueueTable)
            table_widget.refresh_data([_job("old")], "j-vill36")
            app._last_queue_refresh = datetime(2026, 8, 27, 11, 44, 39)
            app._last_job_count = 1

            app.action_refresh()
            assert await asyncio.to_thread(started.wait, 1)
            app.action_refresh()
            app.action_refresh()
            await pilot.pause()

            table = table_widget.query_one(DataTable)
            assert controller.calls == 1
            assert table.loading is False
            assert table.get_cell_at((0, 0)) == "old"
            assert "Refreshing" in str(app.query_one(ConnectionStatus).render())

            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert table.get_cell_at((0, 0)) == "new"
            assert app._refresh_in_flight is False

    asyncio.run(run())


def test_refresh_exception_retains_data_and_allows_recovery(
    refresh_app_config: None,
) -> None:
    async def run() -> None:
        class RecoveringController:
            calls = 0

            def refresh_queue_state(self, **_kwargs: object) -> RefreshOutcome:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("VPN offline\nfull diagnostic")
                return RefreshOutcome([_job("recovered")], [_resource_row()])

        controller = RecoveringController()
        app = _RefreshApp()
        app._controller = controller  # type: ignore[assignment]
        async with app.run_test() as pilot:
            table_widget = app.query_one(QueueTable)
            table_widget.refresh_data([_job("old")], "j-vill36")

            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause()

            table = table_widget.query_one(DataTable)
            assert controller.calls == 1
            assert table.get_cell_at((0, 0)) == "old"
            assert app._refresh_in_flight is False
            assert "VPN offline" in str(app.query_one(ConnectionStatus).render())

            app.action_refresh()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert table.get_cell_at((0, 0)) == "recovered"
            assert app._refresh_in_flight is False

    asyncio.run(run())


def test_desktop_status_is_global_and_tables_use_compact_layout(
    refresh_app_config: None,
) -> None:
    async def run() -> None:
        app = _DesktopLayoutApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            status = app.query_one(ConnectionStatus)
            table = app.query_one(QueueTable).query_one(DataTable)
            nodes = app.query_one(AvailableResourcesTable).query_one(DataTable)

            assert not any(isinstance(node, TabPane) for node in status.ancestors)
            assert app.title == "Cluster Kit"
            assert app.sub_title == "j-vill36@cluster-alias"
            assert table.fixed_columns == 2
            assert table.zebra_stripes is False
            assert len(nodes.columns) == 11
            assert nodes.cursor_type == "none"
            assert nodes.show_cursor is False
            assert nodes.can_focus is False

    asyncio.run(run())


def test_phone_shell_uses_shared_status_and_theme(
    refresh_app_config: None,
) -> None:
    async def run() -> None:
        app = _PhoneLayoutApp()
        async with app.run_test(size=(48, 40)) as pilot:
            await pilot.pause()

            assert app.query_one("#phone-status") is not None
            assert app.theme == "cluster-github-dark"
            assert app.active_view == "queue"

    asyncio.run(run())
