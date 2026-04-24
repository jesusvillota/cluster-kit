"""SLURM job launch module.

Public API:
    - add_launcher_args: Add SLURM CLI arguments to an ArgumentParser
    - maybe_launch: Gate function for local vs cluster execution
    - submit_job: Programmatic job submission
    - resolve_slurm_resources: Resolve partition defaults
    - get_worker_template: Return Path to the generic worker.slurm template
"""

from pathlib import Path

from cluster_kit.launch.launcher import (
    add_launcher_args,
    maybe_launch,
    resolve_slurm_resources,
    submit_job,
)

_WORKER_TEMPLATE = Path(__file__).parent / "worker.slurm"


def get_worker_template() -> Path:
    """Return the path to the generic worker.slurm template."""
    return _WORKER_TEMPLATE


__all__ = [
    "add_launcher_args",
    "get_worker_template",
    "maybe_launch",
    "resolve_slurm_resources",
    "submit_job",
]
