"""SLURM job launch module.

Public API:
    - add_launcher_args: Add SLURM CLI arguments to an ArgumentParser
    - maybe_launch: Gate function for local vs cluster execution
    - submit_command: Programmatic command submission
    - submit_job: Programmatic job submission
    - resolve_slurm_resources: Resolve partition defaults
    - get_worker_template: Return Path to the generic worker.slurm template
"""

from cluster_kit.launch.launcher import (
    add_launcher_args,
    maybe_launch,
    resolve_slurm_resources,
    submit_command,
    submit_job,
)
from cluster_kit.launch.worker import get_worker_template

__all__ = [
    "add_launcher_args",
    "get_worker_template",
    "maybe_launch",
    "resolve_slurm_resources",
    "submit_command",
    "submit_job",
]
