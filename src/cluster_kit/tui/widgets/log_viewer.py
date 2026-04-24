"""LogViewer widget — displays and live-follows SLURM job log files."""

from __future__ import annotations

import shlex
import subprocess

from textual import work  # type: ignore[reportMissingImports]
from textual.app import ComposeResult  # type: ignore[reportMissingImports]
from textual.containers import (  # type: ignore[reportMissingImports]
    Horizontal,
    Vertical,
)
from textual.message import Message  # type: ignore[reportMissingImports]
from textual.widget import Widget  # type: ignore[reportMissingImports]
from textual.widgets import (  # type: ignore[reportMissingImports]
    Button,
    Input,
    Label,
    RichLog,
)

from cluster_kit.utils import SSH_HOST  # noqa: E402
from cluster_kit.utils.clipboard import copy_to_clipboard, is_ssh_session  # noqa: E402

from ..backend.log_discovery import (
    LogFile,
    colorize_log_line,
    discover_log_files,
    fetch_log_content,
    fetch_log_tail,
    get_file_stat,
    parse_log_files,
)
from ..backend.ssh import SSHResult  # noqa: F401


class LogViewer(Widget):
    """Textual widget that renders and optionally live-follows a remote SLURM log file.

    Composes a single ``RichLog`` child.  All public methods are safe to call
    from any thread — ``RichLog`` mutations from worker threads go via
    ``call_from_thread``.

    Public state: ``current_job_id``, ``current_file``, ``is_following``,
    ``tail_process``.
    """

    DEFAULT_CSS = """
    LogViewer {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    #job-id-bar { height: 3; }
    LogViewer RichLog {
        height: 1fr;
    }
    LogViewer.phone-compact {
        padding: 0;
    }

    LogViewer.phone-compact #job-id-bar {
        height: auto;
        grid-size: 1;
    }

    LogViewer.phone-compact #job-id-bar Input {
        min-width: 0;
    }

    LogViewer.phone-compact #log-placeholder {
        height: auto;
        min-height: 2;
        padding: 0 1;
    }

    LogViewer.phone-compact RichLog {
        min-height: 8;
        padding: 0 1;
    }
    """

    class LogJobRequested(Message):
        """Posted when the user requests logs for a manual job ID."""

        def __init__(self, job_id: str) -> None:
            self.job_id = job_id
            super().__init__()

    def __init__(self, compact: bool = False, **kwargs: object) -> None:
        super().__init__(classes="phone-compact" if compact else None, **kwargs)
        self._compact = compact
        self.current_job_id: str | None = None
        self.current_file: LogFile | None = None
        self.is_following: bool = False
        self.tail_process: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._copy_in_progress: bool = False
        self._raw_log_lines: list[str] = []
        self._log_lines: list[str] = []

    def compose(self) -> ComposeResult:
        if self._compact:
            with Vertical(id="job-id-bar"):
                yield Label("Job ID", id="job-id-label")
                yield Input(placeholder="e.g. 12345", id="job-id-input")
                yield Button("Load", variant="primary", id="job-id-btn")
                yield Button("Copy", variant="default", id="copy-btn")
        else:
            with Horizontal(id="job-id-bar"):
                yield Label("Job ID:", id="job-id-label")
                yield Input(placeholder="e.g. 12345", id="job-id-input")
                yield Button("Load", variant="primary", id="job-id-btn")
                yield Button("Copy", variant="default", id="copy-btn")
        yield Label(
            self._instruction_text(),
            id="log-placeholder",
        )
        yield RichLog(highlight=True, markup=True, auto_scroll=True)

    def on_unmount(self) -> None:
        self.stop_follow()

    @property
    def _rich_log(self) -> RichLog:
        return self.query_one(RichLog)

    @property
    def _log_placeholder(self) -> Label:
        return self.query_one("#log-placeholder", Label)

    def _is_compact(self) -> bool:
        return getattr(self, "_compact", False)

    def _instruction_text(self) -> str:
        if self._is_compact():
            return "[dim]Tap a queue job or enter a job ID, then load logs.[/dim]"
        return "[dim]Enter a job ID to load logs[/dim]"

    def _log_header_text(self, job_id: str, log_file: LogFile) -> str:
        file_type = "stderr (.err)" if log_file.is_stderr else "stdout (.out)"
        if self._is_compact():
            return (
                f"[bold]Job:[/bold] [cyan]{job_id}[/cyan]\n"
                f"[bold]File:[/bold] [yellow]{log_file.filename}[/yellow]\n"
                f"[bold]Type:[/bold] {file_type}"
            )
        return (
            f"[bold]Job:[/bold] [cyan]{job_id}[/cyan]  "
            f"[bold]File:[/bold] [yellow]{log_file.filename}[/yellow]  "
            f"[bold]Type:[/bold] {file_type}"
        )

    def _write_line(self, line: str, raw_line: str | None = None) -> None:
        self._rich_log.write(line)
        self._log_lines.append(line)
        if raw_line is not None:
            self._raw_log_lines.append(raw_line)
        if len(self._raw_log_lines) > 5000:
            self._raw_log_lines = self._raw_log_lines[-4000:]
        if len(self._log_lines) > 5000:
            self._log_lines = self._log_lines[-4000:]

    def _submit_job_id(self, raw_value: str) -> None:
        job_id = raw_value.strip()
        if not job_id.isdigit():
            self.notify("Please enter a numeric job ID")
            return
        self.post_message(self.LogJobRequested(job_id))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "job-id-btn":
            job_id_input = self.query_one("#job-id-input", Input)
            self._submit_job_id(job_id_input.value)
        elif event.button.id == "copy-btn":
            self.copy_log_content()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "job-id-input":
            return
        self._submit_job_id(event.value)

    def show_log(self, job_id: str, log_file: LogFile, lines: int = 500) -> None:
        self.stop_follow()

        self.current_job_id = job_id
        self.current_file = log_file
        self._raw_log_lines = []
        self._log_lines = []

        self._log_placeholder.update(self._instruction_text())
        self._log_placeholder.display = False

        rich_log = self._rich_log
        rich_log.clear()

        separator = "─" * (32 if self._is_compact() else 60)
        header = f"[bold cyan]{separator}[/bold cyan]"
        rich_log.write(header)
        self._raw_log_lines.append(header)
        self._log_lines.append(header)
        header_text = self._log_header_text(job_id, log_file)
        rich_log.write(header_text)
        self._raw_log_lines.append(header_text)
        self._log_lines.append(header_text)
        rich_log.write(f"[bold cyan]{separator}[/bold cyan]")
        self._raw_log_lines.append(header)
        self._log_lines.append(header)

        result = fetch_log_tail(log_file.path, lines=lines)

        if not result.success:
            error_line = (
                f"[red]SSH error — could not fetch log:[/red] {result.error_message}"
            )
            rich_log.write(error_line)
            self._raw_log_lines.append(error_line)
            self._log_lines.append(error_line)
            return

        content = result.stdout
        if not content or not content.strip():
            empty_line = "[dim](File is empty or not yet available)[/dim]"
            rich_log.write(empty_line)
            self._raw_log_lines.append(empty_line)
            self._log_lines.append(empty_line)
            return

        for line in content.splitlines():
            self._raw_log_lines.append(line)
            colored = colorize_log_line(line)
            rich_log.write(colored)
            self._log_lines.append(colored)

    def toggle_stderr(self) -> None:
        if self.current_job_id is None or self.current_file is None:
            return

        job_id = self.current_job_id
        current_file = self.current_file
        target_is_stderr = not current_file.is_stderr

        result = discover_log_files(job_id)
        if not result.success:
            self._rich_log.write(
                f"[red]Could not discover log files:[/red] {result.error_message}"
            )
            return

        candidates = parse_log_files(result.stdout)
        alternate: LogFile | None = None
        for candidate in candidates:
            if candidate.is_stderr == target_is_stderr:
                alternate = candidate
                break

        if alternate is None:
            ext = ".err" if target_is_stderr else ".out"
            self._rich_log.write(
                f"[yellow]No {ext} file found for job {job_id}.[/yellow]"
            )
            return

        self.show_log(job_id, alternate)

    def reset_log_view(self) -> None:
        self.stop_follow()
        self.current_job_id = None
        self.current_file = None
        self._raw_log_lines = []
        self._log_lines = []
        self._rich_log.clear()
        self._log_placeholder.update(self._instruction_text())
        self._log_placeholder.display = True

    def start_follow(self) -> None:
        if self.current_file is None or self.is_following:
            return

        self.is_following = True
        self._follow_worker()

    def stop_follow(self) -> None:
        if self.tail_process is None:
            self.is_following = False
            return

        proc = self.tail_process
        self.tail_process = None
        self.is_following = False

        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        except Exception:
            pass

    @work(thread=True)
    def _follow_worker(self) -> None:  # type: ignore[return]
        if self.current_file is None:
            self.is_following = False
            return

        file_path = self.current_file.path

        try:
            proc = subprocess.Popen(
                ["ssh", SSH_HOST, f"tail -f {shlex.quote(file_path)}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.tail_process = proc

            try:
                for raw_line in proc.stdout:  # type: ignore[union-attr]
                    if not self.is_following:
                        break
                    line = raw_line.rstrip()
                    self.call_from_thread(
                        self._write_line, colorize_log_line(line), line
                    )
            finally:
                if self.tail_process is proc:
                    self.stop_follow()
        except Exception as exc:
            self.is_following = False
            self.tail_process = None
            self.call_from_thread(
                self._write_line,
                f"[red]Follow error:[/red] {exc}",
            )

    def copy_log_content(self) -> None:
        """Initiate copy of current log content to clipboard (non-blocking)."""
        if self._copy_in_progress:
            self.notify("Copy already in progress", severity="warning")
            return

        if self.current_file is None:
            self.notify("No log content to copy", severity="warning")
            return

        if is_ssh_session():
            self.notify(
                "Clipboard may be unavailable over SSH session",
                severity="warning",
            )

        file_stat = get_file_stat(self.current_file.path)
        size_threshold = 5 * 1024 * 1024

        is_full_copy = False
        text = ""

        if file_stat is not None and file_stat.size_bytes < size_threshold:
            result = fetch_log_content(self.current_file.path)
            if result.success:
                text = result.stdout
                is_full_copy = True
            else:
                self.notify(
                    f"Could not fetch full log: {result.error_message}. "
                    "Falling back to visible content.",
                    severity="warning",
                )
                text = "\n".join(self._raw_log_lines)
                is_full_copy = False
        else:
            text = "\n".join(self._raw_log_lines)
            is_full_copy = False

        if not text.strip():
            self.notify("No log content to copy", severity="warning")
            return

        self._copy_in_progress = True
        self._start_copy_worker(text, is_full_copy)

    @work(thread=True)
    def _start_copy_worker(self, text: str, is_full_copy: bool = False) -> None:  # type: ignore[return]
        """Entry point that runs _do_copy in a Textual-managed thread."""
        self._do_copy(text, is_full_copy)

    def _do_copy(self, text: str, is_full_copy: bool = False) -> None:
        """Perform the actual clipboard copy operation (runs in thread)."""

        def _call_from_thread(target, *args, **kwargs):
            app = getattr(self, "app", None)
            if app is not None:
                try:
                    app.call_from_thread(target, *args, **kwargs)
                except AttributeError:
                    pass
            elif hasattr(self, "call_from_thread"):
                self.call_from_thread(target, *args, **kwargs)
            else:
                target(*args, **kwargs)

        try:
            success, error = copy_to_clipboard(text)
            if success:
                line_count = text.count("\n") + 1
                size_kb = len(text.encode("utf-8")) / 1024
                filename = self.current_file.filename if self.current_file else "log"
                if is_full_copy:
                    message = (
                        f"[green]Copied full log ({line_count:,} lines, "
                        f"{size_kb:.0f}KB) from {filename}[/green]"
                    )
                else:
                    message = (
                        f"[yellow]Copied visible content only "
                        f"({line_count:,} lines, {size_kb:.0f}KB) - "
                        f"file too large for full copy[/yellow]"
                    )
                _call_from_thread(
                    self.notify,
                    message,
                    severity="information",
                    timeout=5,
                )
            else:
                byte_size = len(text.encode("utf-8"))
                if byte_size > 10 * 1024 * 1024:
                    size_mb = byte_size / (1024 * 1024)
                    _call_from_thread(
                        self.notify,
                        f"Log too large ({size_mb:.1f}MB > 10.0MB limit)",
                        severity="error",
                    )
                else:
                    _call_from_thread(
                        self.notify,
                        f"Copy failed: {error}",
                        severity="error",
                    )
        except Exception as exc:
            _call_from_thread(
                self.notify,
                f"Copy error: {exc}",
                severity="error",
            )
        finally:
            self._copy_in_progress = False


__all__ = ["LogViewer"]
