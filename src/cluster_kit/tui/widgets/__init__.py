"""TUI widgets for cluster management."""

from __future__ import annotations

from cluster_kit.tui.widgets.available_resources_table import AvailableResourcesTable
from cluster_kit.tui.widgets.log_viewer import LogViewer
from cluster_kit.tui.widgets.phone_queue_selector import PhoneQueueSelector
from cluster_kit.tui.widgets.queue_table import JobSelected, QueueTable
from cluster_kit.tui.widgets.status_bar import ConnectionStatus

__all__ = [
    "AvailableResourcesTable",
    "ConnectionStatus",
    "JobSelected",
    "LogViewer",
    "PhoneQueueSelector",
    "QueueTable",
]
