# pyright: reportMissingImports=false
"""SLURM job actions for the cluster TUI."""

from __future__ import annotations

from dataclasses import dataclass

from .ssh import SSHResult, run_ssh_command

QA_SAFE_MODE_ENV_VAR = "WSB_CLUSTER_TUI_QA_SAFE_MODE"


def is_qa_safe_mode_enabled(value: str | None) -> bool:
    """Parse explicit QA-safe-mode environment values."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class JobStatus:
    """Parsed row from sacct output."""

    job_id: str
    state: str
    exit_code: str
    elapsed: str


def cancel_job(job_id: str, *, qa_safe_mode: bool = False) -> SSHResult:
    """Cancel a SLURM job via scancel."""
    if qa_safe_mode:
        return SSHResult(
            stdout=f"QA safe mode: skipped scancel for job {job_id}",
            success=True,
        )
    return run_ssh_command(f"scancel {job_id}")


def get_job_status(job_id: str) -> SSHResult:
    """Fetch sacct status for a SLURM job."""
    return run_ssh_command(
        f"sacct -j {job_id} --format=JobID,State,ExitCode,Elapsed --noheader -P"
    )


def parse_sacct_output(raw: str) -> list[JobStatus]:
    """Parse pipe-delimited sacct output into JobStatus objects."""
    rows: list[JobStatus] = []
    for line in (entry.strip() for entry in raw.splitlines()):
        if not line:
            continue
        job_id, state, exit_code, elapsed = line.split("|", 3)
        rows.append(
            JobStatus(
                job_id=job_id,
                state=state,
                exit_code=exit_code,
                elapsed=elapsed,
            )
        )
    return rows


__all__ = [
    "JobStatus",
    "QA_SAFE_MODE_ENV_VAR",
    "SSHResult",
    "cancel_job",
    "get_job_status",
    "is_qa_safe_mode_enabled",
    "parse_sacct_output",
]
