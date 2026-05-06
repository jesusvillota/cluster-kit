"""Cluster environment creation and launch.

Provides tools for generating conda environment files from pyproject.toml
and submitting environment build jobs to the cluster.
"""

from __future__ import annotations

__all__ = [
    "create_environment_files",
    "launch_environment",
    "parse_pyproject",
    "generate_environment_yml",
    "generate_slurm_script",
]


def __getattr__(name: str):
    if name in __all__:
        from cluster_kit.env import builder as _builder

        return getattr(_builder, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
