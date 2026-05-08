from __future__ import annotations

import asyncio

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Button
from textual.widgets import DataTable, OptionList

from cluster_kit.tui.backend.queue_parser import JobInfo
from cluster_kit.tui.controller import ClusterTUIController, SelectedJob
from cluster_kit.tui.screens import ConfirmCancelScreen
from cluster_kit.tui.widgets.phone_queue_selector import PhoneQueueSelector
from cluster_kit.tui.widgets.queue_table import QueueTable


def _make_job(job_id: str, user: str, *, state: str = "R") -> JobInfo:
    return JobInfo(
        job_id=job_id,
        name=f"job-{job_id}",
        user=user,
        partition="cpu_large",
        state=state,
        time="01:23:45",
        nodes="1",
        reason="Priority",
        cpus_display="64",
        ram_display="128 GB",
        gpus_display="0",
        node_list="HPCOM-01",
    )


class _QueueTableApp(App[None]):
    def compose(self) -> ComposeResult:
        yield QueueTable()


class _PhoneQueueSelectorApp(App[None]):
    def compose(self) -> ComposeResult:
        yield PhoneQueueSelector()


class _ConfirmCancelApp(App[None]):
    def compose(self) -> ComposeResult:
        yield from ()


def test_confirm_cancel_screen_renders_equal_buttons_and_focuses_keep_running() -> None:
    async def run() -> None:
        app = _ConfirmCancelApp()
        async with app.run_test() as pilot:
            screen = ConfirmCancelScreen("17037", "process_whale_counts")
            app.push_screen(screen)
            await pilot.pause()

            confirm = screen.query_one("#confirm", Button)
            keep = screen.query_one("#keep", Button)

            assert confirm.label.plain == "Confirm"
            assert keep.label.plain == "Keep Running"
            assert confirm.size == keep.size
            assert keep.has_focus

    asyncio.run(run())


def test_require_selected_job_rejects_non_owned_jobs() -> None:
    selected_job = SelectedJob(job_id="17004", name="job-17004", user="a-vaz-37")

    selected, message = ClusterTUIController.require_selected_job(
        selected_job,
        allowed_user="j-vill36",
    )

    assert selected is None
    assert message == "Only jobs owned by j-vill36 can be selected"


def test_queue_table_dims_non_owned_rows_and_selects_first_owned_job() -> None:
    async def run() -> None:
        app = _QueueTableApp()
        async with app.run_test() as pilot:
            widget = app.query_one(QueueTable)
            widget.refresh_data(
                [
                    _make_job("17004", "a-vaz-37"),
                    _make_job("17022", "j-vill36"),
                ],
                "j-vill36",
            )
            await pilot.pause()

            table = widget.query_one(DataTable)
            first_cell = table.get_cell_at((0, 0))

            assert isinstance(first_cell, Text)
            assert str(first_cell.style) == "grey62"
            assert table.cursor_row == 1
            assert widget.get_selected_job() == _make_job("17022", "j-vill36")

    asyncio.run(run())


def test_queue_table_skips_non_owned_rows_during_navigation() -> None:
    async def run() -> None:
        app = _QueueTableApp()
        async with app.run_test() as pilot:
            widget = app.query_one(QueueTable)
            widget.refresh_data(
                [
                    _make_job("17022", "j-vill36"),
                    _make_job("17004", "a-vaz-37"),
                    _make_job("17026", "j-vill36"),
                ],
                "j-vill36",
            )
            await pilot.pause()

            table = widget.query_one(DataTable)
            assert table.cursor_row == 0

            table.move_cursor(row=1)
            await pilot.pause()

            assert table.cursor_row == 2
            selected_job = widget.get_selected_job()
            assert selected_job is not None
            assert selected_job.job_id == "17026"

    asyncio.run(run())


def test_phone_selector_disables_non_owned_jobs() -> None:
    async def run() -> None:
        app = _PhoneQueueSelectorApp()
        async with app.run_test() as pilot:
            selector = app.query_one(PhoneQueueSelector)
            selector.refresh_data(
                [
                    _make_job("17004", "a-vaz-37"),
                    _make_job("17022", "j-vill36"),
                ],
                "j-vill36",
            )
            await pilot.pause()

            option_list = selector.query_one(OptionList)
            assert option_list.get_option_at_index(0).disabled is True
            assert option_list.highlighted == 1

            option_list.focus()
            await pilot.press("up")

            assert option_list.highlighted == 1
            selected_job = selector.get_selected_job()
            assert selected_job is not None
            assert selected_job.job_id == "17022"

    asyncio.run(run())
