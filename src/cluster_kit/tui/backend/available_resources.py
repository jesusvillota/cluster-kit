"""Fixed-node resource availability helpers for the cluster TUI."""

# pyright: reportMissingImports=false

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .ssh import run_ssh_command

FIELD_DELIMITER = "|"
EXPECTED_FIELD_COUNT = 4
TARGET_NODE_NAMES = ("HPCOM-01", "HPCOM-02", "HPCOM-04", "HPCOM-05")


@dataclass(frozen=True, slots=True)
class NodeTotals:
    """Fixed total resources configured for one node."""

    cpus: int
    memory_gb: int
    gpus: int


FIXED_NODE_TOTALS = {
    "HPCOM-01": NodeTotals(cpus=72, memory_gb=256, gpus=1),
    "HPCOM-02": NodeTotals(cpus=72, memory_gb=256, gpus=1),
    "HPCOM-04": NodeTotals(cpus=64, memory_gb=256, gpus=1),
    "HPCOM-05": NodeTotals(cpus=64, memory_gb=256, gpus=1),
}


@dataclass(frozen=True, slots=True)
class AvailableResourceRow:
    """Availability summary for one fixed cluster node."""

    node_name: str
    total_cpus: int
    total_memory_gb: int
    total_gpus: int
    allocated_cpus: int
    allocated_memory_gb: int
    allocated_gpus: int
    available_cpus: int
    available_memory_gb: int
    available_gpus: int


def _is_header_row(values: list[str]) -> bool:
    """Return True when the parsed row is the delimiter-formatted header."""

    normalized = [value.strip().upper().replace("-", "") for value in values]
    return normalized == ["NODEHOST", "CPUSSTATE", "ALLOCMEM", "GRESUSED"]


def _clean_delimited_row(line: str) -> list[str]:
    """Split one delimiter-safe sinfo row into trimmed fields."""

    values = [value.strip() for value in line.split(FIELD_DELIMITER)]
    if len(values) < EXPECTED_FIELD_COUNT:
        values.extend([""] * (EXPECTED_FIELD_COUNT - len(values)))
    return values[:EXPECTED_FIELD_COUNT]


def _parse_allocated_cpus(cpus_state: str) -> int:
    """Parse allocated CPUs from Slurm's allocated/idle/other/total field."""

    first_segment = cpus_state.strip().split("/", 1)[0].strip()
    if not first_segment:
        return 0

    try:
        return max(int(first_segment), 0)
    except ValueError:
        return 0


def _parse_allocated_memory_gb(alloc_mem_mb: str) -> int:
    """Convert allocated memory in MB to a whole-GB display value."""

    value = alloc_mem_mb.strip()
    if not value:
        return 0

    try:
        allocated_mb = float(value)
    except ValueError:
        return 0

    if allocated_mb <= 0:
        return 0
    return int(allocated_mb // 1024)


def _parse_allocated_gpus(gres_used: str) -> int:
    """Parse allocated GPU count from GresUsed values."""

    value = gres_used.strip()
    if not value:
        return 0

    gpu_total = 0
    for item in value.split(","):
        match = re.search(r"gpu(?::[^:,()]+)*:(\d+)", item.strip(), re.IGNORECASE)
        if match is None:
            continue
        gpu_total += int(match.group(1))
    return gpu_total


def _clamp_available(total: int, allocated: int) -> int:
    """Clamp available resources to zero when allocations exceed totals."""

    return max(total - allocated, 0)


def _default_row(node_name: str) -> AvailableResourceRow:
    """Build a safe fallback row using fixed totals and zero allocations."""

    totals = FIXED_NODE_TOTALS[node_name]
    return AvailableResourceRow(
        node_name=node_name,
        total_cpus=totals.cpus,
        total_memory_gb=totals.memory_gb,
        total_gpus=totals.gpus,
        allocated_cpus=0,
        allocated_memory_gb=0,
        allocated_gpus=0,
        available_cpus=totals.cpus,
        available_memory_gb=totals.memory_gb,
        available_gpus=totals.gpus,
    )


def _failure_row(node_name: str) -> AvailableResourceRow:
    """Build a failure fallback row using fixed totals and zero allocations."""

    return _default_row(node_name)


def _row_from_live_values(
    node_name: str,
    cpus_state: str,
    alloc_mem_mb: str,
    gres_used: str,
) -> AvailableResourceRow:
    """Combine fixed node totals with live allocation values."""

    totals = FIXED_NODE_TOTALS[node_name]
    allocated_cpus = _parse_allocated_cpus(cpus_state)
    allocated_memory_gb = _parse_allocated_memory_gb(alloc_mem_mb)
    allocated_gpus = _parse_allocated_gpus(gres_used)

    return AvailableResourceRow(
        node_name=node_name,
        total_cpus=totals.cpus,
        total_memory_gb=totals.memory_gb,
        total_gpus=totals.gpus,
        allocated_cpus=allocated_cpus,
        allocated_memory_gb=allocated_memory_gb,
        allocated_gpus=allocated_gpus,
        available_cpus=_clamp_available(totals.cpus, allocated_cpus),
        available_memory_gb=_clamp_available(totals.memory_gb, allocated_memory_gb),
        available_gpus=_clamp_available(totals.gpus, allocated_gpus),
    )


def parse_sinfo_output(raw: str) -> list[AvailableResourceRow]:
    """Parse sinfo output and always return the fixed node inventory in order."""

    rows_by_node = {
        node_name: _default_row(node_name) for node_name in TARGET_NODE_NAMES
    }

    for line in raw.splitlines():
        if not line.strip():
            continue

        row = _clean_delimited_row(line)
        if _is_header_row(row):
            continue

        node_name = row[0]
        if node_name not in FIXED_NODE_TOTALS:
            continue

        rows_by_node[node_name] = _row_from_live_values(
            node_name=node_name,
            cpus_state=row[1],
            alloc_mem_mb=row[2],
            gres_used=row[3],
        )

    return [rows_by_node[node_name] for node_name in TARGET_NODE_NAMES]


def fetch_available_resources() -> list[AvailableResourceRow]:
    """Fetch fixed-node availability via sinfo and fall back safely on errors."""

    node_list = ",".join(TARGET_NODE_NAMES)
    command = [
        "sinfo",
        "--Node",
        "--exact",
        "--noheader",
        f"--nodes={node_list}",
        f"--Format=NodeHost:{FIELD_DELIMITER},CPUsState:{FIELD_DELIMITER},AllocMem:{FIELD_DELIMITER},GresUsed",
    ]
    result = run_ssh_command(shlex.join(command))
    if not result.success:
        return [_failure_row(node_name) for node_name in TARGET_NODE_NAMES]
    return parse_sinfo_output(result.stdout)


__all__ = [
    "AvailableResourceRow",
    "FIELD_DELIMITER",
    "FIXED_NODE_TOTALS",
    "TARGET_NODE_NAMES",
    "fetch_available_resources",
    "parse_sinfo_output",
]
