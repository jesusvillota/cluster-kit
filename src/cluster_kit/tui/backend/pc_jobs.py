"""Fetch detached-job state from the PC (ssh-executor 'pc' profile).

Never raises: a down VPN becomes a rendered "PC unreachable" state so the
cluster panels are unaffected.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

from cluster_kit.config import ClusterConfig, ConfigError, load_config
from cluster_kit.jobs.manager import JobError, list_jobs


@dataclass(frozen=True)
class PcJobsResult:
    jobs: list[dict] = field(default_factory=list)
    error: str | None = None


@lru_cache(maxsize=1)
def _pc_config() -> ClusterConfig:
    return load_config(env_profile="pc")


def fetch_pc_jobs() -> PcJobsResult:
    try:
        return PcJobsResult(jobs=list_jobs(config=_pc_config()))
    except (
        JobError,
        ConfigError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        return PcJobsResult(error=f"PC unreachable: {exc}")
