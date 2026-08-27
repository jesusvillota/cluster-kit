"""Packaged SLURM worker script location."""

from pathlib import Path

_WORKER_TEMPLATE = Path(__file__).parent / "worker.slurm"


def get_worker_template() -> Path:
    """Return the path to the generic worker.slurm template."""
    return _WORKER_TEMPLATE


__all__ = ["get_worker_template"]
