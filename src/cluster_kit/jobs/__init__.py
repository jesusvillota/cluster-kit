"""Detached-job management for ssh-executor profiles."""

from cluster_kit.jobs.manager import (
    JobError,
    JobHandle,
    cancel,
    job_status,
    list_jobs,
    read_log,
    submit,
)

__all__ = [
    "JobError",
    "JobHandle",
    "cancel",
    "job_status",
    "list_jobs",
    "read_log",
    "submit",
]
