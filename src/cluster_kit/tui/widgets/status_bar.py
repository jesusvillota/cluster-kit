from __future__ import annotations

from datetime import datetime

from textual.widgets import Static


_STATUS_CLASSES = ("connected", "stale", "error")


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

    def mark_stale(self) -> None:
        self._set_state("stale")
        self.update("⚠ Data may be stale")

    def mark_connected(self) -> None:
        self._set_state("connected")
        self.update("● Connected")

    def mark_error(self, message: str) -> None:
        self._set_state("error")
        self.update(f"✗ Connection Error: {message}")
