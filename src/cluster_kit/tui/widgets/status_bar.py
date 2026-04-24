from __future__ import annotations

from datetime import datetime

from textual.widgets import Static


class ConnectionStatus(Static):
    def update_status(
        self, connected: bool, job_count: int, last_refresh: datetime
    ) -> None:
        time_str = last_refresh.strftime("%H:%M:%S")
        if connected:
            self.update(f"● Connected | {job_count} jobs | Last refresh: {time_str}")
        else:
            self.update(f"✗ Disconnected | {job_count} jobs | Last refresh: {time_str}")

    def mark_stale(self) -> None:
        self.update("⚠ Data may be stale")

    def mark_connected(self) -> None:
        self.update("● Connected")

    def mark_error(self, message: str) -> None:
        self.update(f"✗ Connection Error: {message}")
