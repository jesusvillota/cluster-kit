from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from cluster_kit.tui.backend.available_resources import (
    AvailableResourceRow,
)
from cluster_kit.tui.backend.job_actions import SSHResult
from cluster_kit.tui.backend.log_discovery import LogFile
from cluster_kit.tui.backend.queue_parser import JobInfo


@dataclass(frozen=True)
class SelectedJob:
    """UI-agnostic representation of a selected queue job."""

    job_id: str
    name: str = ""

    @classmethod
    def from_job_info(cls, job_info: JobInfo) -> "SelectedJob":
        return cls(job_id=job_info.job_id, name=job_info.name)


@dataclass(frozen=True)
class RefreshSuccess:
    """Successful queue refresh payload."""

    jobs: list[JobInfo]
    availability_rows: list[AvailableResourceRow]

    @property
    def job_count(self) -> int:
        return len(self.jobs)


@dataclass(frozen=True)
class RefreshFailure:
    """Failed queue refresh payload preserving non-queue state."""

    availability_rows: list[AvailableResourceRow]


@dataclass(frozen=True)
class LogRoute:
    """A resolved log file to show for a job."""

    job_id: str
    log_file: LogFile


class ClusterTUIController:
    """Shared business logic for cluster TUI actions across UI shells."""

    def __init__(
        self,
        *,
        fetch_queue: Callable[..., SSHResult],
        parse_squeue_output: Callable[[str], list[JobInfo]],
        fetch_available_resources: Callable[[], list[AvailableResourceRow]],
        discover_log_files: Callable[[str], SSHResult],
        parse_log_files: Callable[[str], list[LogFile]],
        cancel_job: Callable[..., SSHResult],
        sync_screen_factory: Callable[..., Any],
    ) -> None:
        self._fetch_queue = fetch_queue
        self._parse_squeue_output = parse_squeue_output
        self._fetch_available_resources = fetch_available_resources
        self._discover_log_files = discover_log_files
        self._parse_log_files = parse_log_files
        self._cancel_job = cancel_job
        self._sync_screen_factory = sync_screen_factory

    def refresh_queue_state(
        self,
        *,
        all_users: bool,
        cluster_user: str,
    ) -> RefreshSuccess | RefreshFailure:
        result = self._fetch_queue(user=None if all_users else cluster_user)
        availability_rows = self._fetch_available_resources()
        if result.success:
            jobs = self._parse_squeue_output(result.stdout)
            return RefreshSuccess(jobs=jobs, availability_rows=availability_rows)
        return RefreshFailure(availability_rows=availability_rows)

    @staticmethod
    def require_selected_job(
        selected_job: SelectedJob | None,
    ) -> tuple[SelectedJob | None, str | None]:
        if selected_job is None:
            return None, "No job selected"
        return selected_job, None

    def cancel_selected_job(self, job_id: str, *, qa_safe_mode: bool) -> SSHResult:
        return self._cancel_job(job_id, qa_safe_mode=qa_safe_mode)

    def resolve_log_route(self, job_id: str) -> tuple[LogRoute | None, str | None]:
        result = self._discover_log_files(job_id)
        if not result.success:
            return None, f"Failed to discover logs for job {job_id}"

        log_files = self._parse_log_files(result.stdout)
        if not log_files:
            return None, f"No log files found for job {job_id}"

        return LogRoute(
            job_id=job_id, log_file=self.pick_initial_log_file(log_files)
        ), None

    @staticmethod
    def pick_initial_log_file(log_files: list[LogFile]) -> LogFile:
        for log_file in log_files:
            if not log_file.is_stderr:
                return log_file
        return log_files[0]

    def create_sync_screen(self, *, qa_safe_mode: bool, compact: bool = False) -> Any:
        return self._sync_screen_factory(qa_safe_mode, compact=compact)


__all__ = [
    "ClusterTUIController",
    "LogRoute",
    "RefreshFailure",
    "RefreshSuccess",
    "SelectedJob",
]
