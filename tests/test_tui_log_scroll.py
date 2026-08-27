from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from textual.app import App, ComposeResult
from textual.widgets import Button, RichLog

from cluster_kit.tui.app_phone import PhoneClusterTUI
from cluster_kit.tui.widgets.log_viewer import LogViewer


class _CompactLogApp(App[None]):
    def compose(self) -> ComposeResult:
        yield LogViewer(compact=True)


class _DesktopLogApp(App[None]):
    def compose(self) -> ComposeResult:
        yield LogViewer(compact=False)


class _PhoneShellApp(PhoneClusterTUI):
    AUTO_START_REFRESH = False

    def on_mount(self) -> None:
        self.viewed_logs = False
        self._set_active_view("queue")

    def action_view_logs(self) -> None:
        self.viewed_logs = True


def test_phone_log_view_has_scroll_buttons_and_no_copy() -> None:
    async def run() -> None:
        app = _CompactLogApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            ids = {b.id for b in app.query(Button)}
            scroll_ids = [b.id for b in app.query("#log-scroll-bar Button")]
            assert {
                "log-scroll-top",
                "log-scroll-left-big",
                "log-scroll-left",
                "log-scroll-up",
                "log-scroll-down",
                "log-scroll-right",
                "log-scroll-right-big",
                "log-scroll-end",
            } <= ids
            assert scroll_ids == [
                "log-scroll-top",
                "log-scroll-down",
                "log-scroll-up",
                "log-scroll-end",
                "log-scroll-left-big",
                "log-scroll-left",
                "log-scroll-right",
                "log-scroll-right-big",
            ]
            # Copy is removed on the phone (it copied to the host Mac, not the phone).
            assert "copy-btn" not in ids

    asyncio.run(run())


def test_scroll_buttons_route_to_richlog() -> None:
    async def run() -> None:
        app = _CompactLogApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            rich = app.query_one(RichLog)
            rich.scroll_home = MagicMock()
            rich.scroll_end = MagicMock()
            rich.scroll_relative = MagicMock()

            app.query_one("#log-scroll-top", Button).press()
            await pilot.pause()
            rich.scroll_home.assert_called_once()

            app.query_one("#log-scroll-end", Button).press()
            await pilot.pause()
            rich.scroll_end.assert_called_once()

            # ↑ and ↓ move by a multi-row step (negative up, positive down).
            app.query_one("#log-scroll-up", Button).press()
            await pilot.pause()
            up_y = rich.scroll_relative.call_args.kwargs["y"]
            assert up_y < 0

            app.query_one("#log-scroll-down", Button).press()
            await pilot.pause()
            down_y = rich.scroll_relative.call_args.kwargs["y"]
            assert down_y > 0
            assert abs(down_y) == abs(up_y)

            # Small arrows move horizontally; big arrows move farther.
            app.query_one("#log-scroll-left", Button).press()
            await pilot.pause()
            left_x = rich.scroll_relative.call_args.kwargs["x"]
            assert left_x < 0

            app.query_one("#log-scroll-left-big", Button).press()
            await pilot.pause()
            big_left_x = rich.scroll_relative.call_args.kwargs["x"]
            assert big_left_x < left_x

            app.query_one("#log-scroll-right", Button).press()
            await pilot.pause()
            right_x = rich.scroll_relative.call_args.kwargs["x"]
            assert right_x > 0
            assert abs(right_x) == abs(left_x)

            app.query_one("#log-scroll-right-big", Button).press()
            await pilot.pause()
            big_right_x = rich.scroll_relative.call_args.kwargs["x"]
            assert big_right_x > right_x
            assert abs(big_right_x) == abs(big_left_x)

    asyncio.run(run())


def test_desktop_log_view_keeps_copy_button() -> None:
    async def run() -> None:
        app = _DesktopLogApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            ids = {b.id for b in app.query(Button)}
            assert "copy-btn" in ids
            assert "log-scroll-top" not in ids
            assert "log-scroll-left" not in ids

    asyncio.run(run())


def test_phone_action_dock_changes_by_active_view() -> None:
    async def run() -> None:
        app = _PhoneShellApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            assert app.query_one("#phone-action-dock").display is True
            assert app.query_one("#phone-action-row-queue-primary").display is True
            assert app.query_one("#phone-action-row-queue-secondary").display is True
            assert app.query_one("#phone-action-row-log").display is False
            assert "phone-action-refresh" in {b.id for b in app.query(Button)}
            assert "phone-action-selected-logs" in {b.id for b in app.query(Button)}
            assert "phone-action-sync" in {b.id for b in app.query(Button)}

            app._set_active_view("available")
            await pilot.pause()

            assert app.query_one("#phone-action-dock").display is False

            app._set_active_view("logs")
            await pilot.pause()

            assert app.query_one("#phone-action-dock").display is True
            assert app.query_one("#phone-action-row-queue-primary").display is False
            assert app.query_one("#phone-action-row-queue-secondary").display is False
            assert app.query_one("#phone-action-row-log").display is True
            ids = {b.id for b in app.query(Button)}
            assert "phone-action-toggle-stderr" in ids
            assert "phone-action-cancel-log" in {b.id for b in app.query(Button)}

    asyncio.run(run())


def test_phone_queue_selection_does_not_load_logs_without_log_button_press() -> None:
    async def run() -> None:
        app = _PhoneShellApp()
        async with app.run_test() as pilot:
            await pilot.pause()

            app.on_option_list_option_selected(None)

            assert app.viewed_logs is False

    asyncio.run(run())
