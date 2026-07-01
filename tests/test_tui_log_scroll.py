from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from textual.app import App, ComposeResult
from textual.widgets import Button, RichLog

from cluster_kit.tui.widgets.log_viewer import LogViewer


class _CompactLogApp(App[None]):
    def compose(self) -> ComposeResult:
        yield LogViewer(compact=True)


class _DesktopLogApp(App[None]):
    def compose(self) -> ComposeResult:
        yield LogViewer(compact=False)


def test_phone_log_view_has_scroll_buttons_and_no_copy() -> None:
    async def run() -> None:
        app = _CompactLogApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            ids = {b.id for b in app.query(Button)}
            assert {
                "log-scroll-top",
                "log-scroll-up",
                "log-scroll-down",
                "log-scroll-end",
            } <= ids
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

    asyncio.run(run())


def test_desktop_log_view_keeps_copy_button() -> None:
    async def run() -> None:
        app = _DesktopLogApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            ids = {b.id for b in app.query(Button)}
            assert "copy-btn" in ids
            assert "log-scroll-top" not in ids

    asyncio.run(run())
