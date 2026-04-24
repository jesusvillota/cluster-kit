"""Cluster utilities package.

This package provides common functionality for cluster operations:
- SSH connection testing (ssh)
- Rsync/SCP file transfers (rsync)
- Python cache cleaning (cache)
- Rich display formatting (display)

Example:
    >>> from cluster_kit.utils import ClusterConnection, RsyncRunner
    >>> from cluster_kit.utils import show_success_panel
"""

from __future__ import annotations


def __getattr__(name: str):
    if name == "ClusterConnection":
        from cluster_kit.utils.ssh import ClusterConnection

        return ClusterConnection
    if name == "SSH_HOST":
        from cluster_kit.utils.ssh import __getattr__ as ssh_getattr

        return ssh_getattr("SSH_HOST")
    if name == "RsyncRunner":
        from cluster_kit.utils.rsync import RsyncRunner

        return RsyncRunner
    if name == "ScpRunner":
        from cluster_kit.utils.rsync import ScpRunner

        return ScpRunner
    if name == "PythonCacheCleaner":
        from cluster_kit.utils.cache import PythonCacheCleaner

        return PythonCacheCleaner
    if name in (
        "show_config_panel",
        "show_success_panel",
        "show_error_panel",
        "show_step_header",
    ):
        from cluster_kit.utils import display as _display

        return getattr(_display, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # SSH
    "ClusterConnection",
    "SSH_HOST",
    # Rsync/SCP
    "RsyncRunner",
    "ScpRunner",
    # Cache
    "PythonCacheCleaner",
    # Display
    "show_config_panel",
    "show_success_panel",
    "show_error_panel",
    "show_step_header",
]
