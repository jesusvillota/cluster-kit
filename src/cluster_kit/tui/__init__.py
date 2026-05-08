"""Textual TUI components."""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "PhoneClusterTUI": ("cluster_kit.tui.app_phone", "PhoneClusterTUI"),
    "ConfirmCancelScreen": (
        "cluster_kit.tui.screens",
        "ConfirmCancelScreen",
    ),
    "SyncScreen": ("cluster_kit.tui.screens", "SyncScreen"),
    "MAIN_CSS": ("cluster_kit.tui.styles", "MAIN_CSS"),
    "PHONE_CSS": ("cluster_kit.tui.styles", "PHONE_CSS"),
    "ClusterTUIController": (
        "cluster_kit.tui.controller",
        "ClusterTUIController",
    ),
    "LogRoute": ("cluster_kit.tui.controller", "LogRoute"),
    "RefreshFailure": ("cluster_kit.tui.controller", "RefreshFailure"),
    "RefreshSuccess": ("cluster_kit.tui.controller", "RefreshSuccess"),
    "SelectedJob": ("cluster_kit.tui.controller", "SelectedJob"),
    "AvailableResourcesTable": (
        "cluster_kit.tui.widgets",
        "AvailableResourcesTable",
    ),
    "ConnectionStatus": ("cluster_kit.tui.widgets", "ConnectionStatus"),
    "JobSelected": ("cluster_kit.tui.widgets", "JobSelected"),
    "LogViewer": ("cluster_kit.tui.widgets", "LogViewer"),
    "PhoneQueueSelector": ("cluster_kit.tui.widgets", "PhoneQueueSelector"),
    "QueueTable": ("cluster_kit.tui.widgets", "QueueTable"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute_name = _EXPORTS[name]
    module = import_module(module_name)
    return getattr(module, attribute_name)


def __dir__() -> list[str]:
    return sorted(list(globals()) + __all__)
