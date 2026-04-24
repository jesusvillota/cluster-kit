"""Textual TUI components."""

from __future__ import annotations

from cluster_kit.tui.app_phone import PhoneClusterTUI
from cluster_kit.tui.controller import (
    ClusterTUIController,
    LogRoute,
    RefreshFailure,
    RefreshSuccess,
    SelectedJob,
)
from cluster_kit.tui.screens import ConfirmCancelScreen, SyncScreen
from cluster_kit.tui.styles import MAIN_CSS, PHONE_CSS
from cluster_kit.tui.widgets import (
    AvailableResourcesTable,
    ConnectionStatus,
    JobSelected,
    LogViewer,
    PhoneQueueSelector,
    QueueTable,
)

__all__ = [
    # Apps
    "PhoneClusterTUI",
    # Screens
    "ConfirmCancelScreen",
    "SyncScreen",
    # Styles
    "MAIN_CSS",
    "PHONE_CSS",
    # Controller
    "ClusterTUIController",
    "LogRoute",
    "RefreshFailure",
    "RefreshSuccess",
    "SelectedJob",
    # Widgets
    "AvailableResourcesTable",
    "ConnectionStatus",
    "JobSelected",
    "LogViewer",
    "PhoneQueueSelector",
    "QueueTable",
]
