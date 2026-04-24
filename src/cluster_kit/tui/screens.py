from __future__ import annotations

import os
import subprocess
import sys

from textual import work  # type: ignore[reportMissingImports]
from textual.app import ComposeResult  # type: ignore[reportMissingImports]
from textual.binding import Binding  # type: ignore[reportMissingImports]
from textual.containers import Grid  # type: ignore[reportMissingImports]
from textual.screen import ModalScreen, Screen  # type: ignore[reportMissingImports]
from textual.widgets import Button, Label, RichLog  # type: ignore[reportMissingImports]


class ConfirmCancelScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel")]
    DEFAULT_CSS = """
    ConfirmCancelScreen {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }

    ConfirmCancelScreen.phone-compact > Grid {
        width: 92vw;
        max-width: 36;
        height: auto;
        min-height: 12;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        grid-size: 1;
        grid-rows: auto auto auto;
        grid-gutter: 1;
    }

    ConfirmCancelScreen > Grid {
        width: 60;
        height: 13;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
        grid-size: 1 3;
        grid-rows: 3 3 3;
        grid-gutter: 1;
    }

    ConfirmCancelScreen #dialog-label {
        width: 1fr;
        height: 3;
        content-align: center middle;
        text-align: center;
    }

    ConfirmCancelScreen.phone-compact #dialog-label {
        height: auto;
        min-height: 3;
    }

    ConfirmCancelScreen #confirm {
        width: 1fr;
        height: 3;
    }

    ConfirmCancelScreen #keep {
        width: 1fr;
        height: 3;
    }
    """

    def __init__(
        self, job_id: str, job_name: str = "", *, compact: bool = False
    ) -> None:
        super().__init__()
        self._job_id = job_id
        self._job_name = job_name
        if compact:
            self.add_class("phone-compact")

    def compose(self) -> ComposeResult:
        if self._job_name:
            label_text = f"Cancel job {self._job_id} ({self._job_name})?"
        else:
            label_text = f"Cancel job {self._job_id}?"
        with Grid():
            yield Label(label_text, id="dialog-label", markup=False)
            yield Button("Confirm", variant="error", id="confirm")
            yield Button("Keep Running", variant="primary", id="keep")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(False)


class SyncScreen(Screen):
    BINDINGS = [Binding("escape", "dismiss_screen", "Close")]
    DEFAULT_CSS = """
    SyncScreen {
        background: $surface;
    }

    SyncScreen #sync-title {
        height: 3;
        content-align: center middle;
        text-align: center;
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
    }

    SyncScreen #sync-log {
        height: 1fr;
    }

    SyncScreen.phone-compact #sync-title {
        height: 4;
        padding: 0 1;
    }

    SyncScreen.phone-compact #sync-log {
        min-height: 8;
    }

    SyncScreen #sync-cancel {
        dock: bottom;
        height: 3;
        width: 1fr;
    }

    SyncScreen #sync-close {
        dock: bottom;
        height: 3;
        width: 1fr;
    }
    """

    def __init__(self, qa_safe_mode: bool = False, *, compact: bool = False) -> None:
        super().__init__()
        self._qa_safe_mode = qa_safe_mode
        self._sync_proc: subprocess.Popen | None = None
        self._is_running = False
        if compact:
            self.add_class("phone-compact")

    def compose(self) -> ComposeResult:
        yield Label(
            "Code Sync — Deploying src/ and runnables/ to cluster", id="sync-title"
        )
        yield RichLog(highlight=False, markup=False, auto_scroll=True, id="sync-log")
        yield Button("Cancel", variant="error", id="sync-cancel")
        yield Button("Close", variant="primary", id="sync-close", disabled=True)

    def on_mount(self) -> None:
        self._is_running = True
        self._sync_worker()

    @work(thread=True)
    def _sync_worker(self) -> None:
        rich_log = self.query_one("#sync-log", RichLog)
        success = False
        try:
            if self._qa_safe_mode:
                self._run_qa_safe_sync(rich_log)
                success = True
                return
            proc = subprocess.Popen(
                [sys.executable, "-m", "cluster_kit.sync.code"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
            )
            self._sync_proc = proc
            if proc.stdout is not None:
                for line in proc.stdout:
                    self.app.call_from_thread(rich_log.write, line.rstrip())
            proc.wait()
            success = proc.returncode == 0
        except Exception as exc:
            self.app.call_from_thread(rich_log.write, f"Launch error: {exc}")
        finally:
            self._sync_proc = None
            self._is_running = False
            self.app.call_from_thread(self._on_sync_done, success)

    def _run_qa_safe_sync(self, rich_log: RichLog) -> None:
        for line in (
            "[QA safe mode] Starting sync dry run",
            "[QA safe mode] Skipping live sync — would deploy src/ and runnables/",
            "[QA safe mode] Sync completed without cluster side effects",
        ):
            self.app.call_from_thread(rich_log.write, line)

    def _on_sync_done(self, success: bool) -> None:
        log = self.query_one("#sync-log", RichLog)
        if success:
            log.write("[Sync completed successfully]")
        else:
            log.write("[Sync FAILED or was cancelled]")
        self.query_one("#sync-cancel", Button).disabled = True
        self.query_one("#sync-close", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sync-cancel":
            self._stop_sync()
        elif event.button.id == "sync-close":
            self.app.pop_screen()

    def _stop_sync(self) -> None:
        proc = self._sync_proc
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except Exception:
            pass
        self._sync_proc = None
        self._is_running = False
        log = self.query_one("#sync-log", RichLog)
        log.write("[Cancelled by user]")
        self.query_one("#sync-cancel", Button).disabled = True
        self.query_one("#sync-close", Button).disabled = False

    def action_dismiss_screen(self) -> None:
        if not self._is_running:
            self.app.pop_screen()
