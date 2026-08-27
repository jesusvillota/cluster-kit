from __future__ import annotations

from datetime import datetime

from textual.widgets import Static

_STATUS_CLASSES = ("connected", "stale", "error")
_MAX_ERROR_LENGTH = 120


def _concise_error(message: str | None) -> str:
    if not message:
        return "unknown error"
    first_line = next(
        (line.strip() for line in message.splitlines() if line.strip()), ""
    )
    if len(first_line) <= _MAX_ERROR_LENGTH:
        return first_line or "unknown error"
    return f"{first_line[: _MAX_ERROR_LENGTH - 1]}…"


class ConnectionStatus(Static):
    def _set_state(self, state: str | None) -> None:
        for name in _STATUS_CLASSES:
            self.set_class(name == state, name)

    def update_status(
        self, connected: bool, job_count: int, last_refresh: datetime
    ) -> None:
        time_str = last_refresh.strftime("%H:%M:%S")
        if connected:
            self._set_state("connected")
            self.update(f"● Connected · {job_count} jobs · {time_str}")
        else:
            self._set_state("error")
            self.update(f"✗ Disconnected · {job_count} jobs · {time_str}")

    def mark_refreshing(
        self, last_refresh: datetime | None, job_count: int
    ) -> None:
        self._set_state(None)
        if last_refresh is None:
            self.update("↻ Connecting…")
            return
        self.update(
            f"↻ Refreshing · showing {job_count} jobs from "
            f"{last_refresh.strftime('%H:%M:%S')}"
        )

    def mark_partial(
        self,
        label: str,
        message: str | None,
        job_count: int,
        refreshed_at: datetime,
    ) -> None:
        self._set_state("stale")
        self.update(
            f"⚠ {label}: {_concise_error(message)} · {job_count} jobs updated "
            f"{refreshed_at.strftime('%H:%M:%S')}"
        )

    def mark_stale(
        self,
        message: str | None = None,
        last_refresh: datetime | None = None,
        job_count: int = 0,
    ) -> None:
        self._set_state("error")
        detail = _concise_error(message)
        if last_refresh is None:
            self.update(f"✗ Cluster unavailable: {detail}")
            return
        self.update(
            f"⚠ Queue stale: {detail} · showing {job_count} jobs from "
            f"{last_refresh.strftime('%H:%M:%S')}"
        )

    def mark_connected(self) -> None:
        self._set_state("connected")
        self.update("● Connected")

    def mark_error(self, message: str) -> None:
        self._set_state("error")
        self.update(f"✗ Connection Error: {message}")


class MirrorStatus(Static):
    """Last cluster⇄PC data-mirror time per dataset, with staleness color."""

    def _set_state(self, state: str | None) -> None:
        for name in _STATUS_CLASSES:
            self.set_class(name == state, name)

    def update_from_state(
        self, state: dict, stale_after_hours: float = 24.0
    ) -> None:
        if not state:
            self._set_state("stale")
            self.update("⇄ Mirror: never run")
            return

        now = datetime.now().astimezone()
        parts: list[str] = []
        worst = "connected"
        for name, entry in state.items():
            last_success = entry.get("last_success")
            if not last_success:
                parts.append(f"{name} · never")
                worst = "stale" if worst == "connected" else worst
                continue
            age = now - datetime.fromisoformat(last_success)
            hours = age.total_seconds() / 3600
            ago = f"{hours:.0f}h ago" if hours >= 1 else f"{age.seconds // 60}m ago"
            parts.append(f"{name} · {ago}")
            if not entry.get("ok"):
                worst = "error"
            elif hours > stale_after_hours and worst != "error":
                worst = "stale"
        self._set_state(worst)
        self.update(f"⇄ {' | '.join(parts)}")
