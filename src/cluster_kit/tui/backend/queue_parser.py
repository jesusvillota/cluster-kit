"""Queue parsing and retrieval helpers for the cluster TUI."""

# pyright: reportMissingImports=false

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .ssh import SSHResult, run_ssh_command

FIELD_DELIMITER = "|"
EMPTY_RESOURCE_DISPLAY = "—"
EXPECTED_FIELD_COUNT = 11
RUNNING_STATES_WITH_ALLOCATIONS = {"R", "RUNNING", "CG", "COMPLETING"}
MEMORY_UNITS_TO_GB = {
    "K": 1 / (1024**2),
    "KI": 1 / (1024**2),
    "M": 1 / 1024,
    "MI": 1 / 1024,
    "G": 1,
    "GI": 1,
    "T": 1024,
    "TI": 1024,
    "P": 1024**2,
    "PI": 1024**2,
}
DEFAULT_SQUEUE_FORMAT = (
    f"JobID:{FIELD_DELIMITER},"
    f"Name:{FIELD_DELIMITER},"
    f"UserName:{FIELD_DELIMITER},"
    f"Partition:{FIELD_DELIMITER},"
    f"StateCompact:{FIELD_DELIMITER},"
    f"TimeUsed:{FIELD_DELIMITER},"
    f"NumNodes:{FIELD_DELIMITER},"
    f"Reason:{FIELD_DELIMITER},"
    f"NumCPUs:{FIELD_DELIMITER},"
    f"tres-alloc:{FIELD_DELIMITER},"
    "NodeList"
)


@dataclass(slots=True)
class JobInfo:
    """Structured representation of one squeue row."""

    job_id: str
    name: str
    user: str
    partition: str
    state: str
    time: str
    nodes: str
    reason: str
    cpus_display: str = EMPTY_RESOURCE_DISPLAY
    ram_display: str = EMPTY_RESOURCE_DISPLAY
    gpus_display: str = EMPTY_RESOURCE_DISPLAY
    node_list: str = ""


def _is_header_row(values: list[str]) -> bool:
    """Return True when the parsed row is the delimiter-formatted header."""

    normalized = [value.strip().upper().replace("-", "_") for value in values]
    return normalized == [
        "JOBID",
        "NAME",
        "USERNAME",
        "PARTITION",
        "STATECOMPACT",
        "TIMEUSED",
        "NUMNODES",
        "REASON",
        "NUMCPUS",
        "TRES_ALLOC",
        "NODELIST",
    ]


def _clean_delimited_row(line: str) -> list[str]:
    """Split one delimiter-safe squeue row into trimmed fields."""

    values = [value.strip() for value in line.split(FIELD_DELIMITER)]
    if len(values) < EXPECTED_FIELD_COUNT:
        values.extend([""] * (EXPECTED_FIELD_COUNT - len(values)))
    return values[:EXPECTED_FIELD_COUNT]


def _format_number(value: str) -> str:
    """Normalize integral numeric displays and preserve non-numeric values."""

    try:
        number = float(value)
    except ValueError:
        return value

    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _format_memory_display(memory_value: str) -> str:
    """Convert Slurm memory values into deterministic GB displays when possible."""

    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([A-Za-z]+)?", memory_value.strip())
    if not match:
        return memory_value

    amount = float(match.group(1))
    unit = (match.group(2) or "G").upper()
    unit_factor = MEMORY_UNITS_TO_GB.get(unit)
    if unit_factor is None:
        return memory_value

    amount_gb = amount * unit_factor
    return f"{_format_number(f'{amount_gb:g}')} GB"


def _parse_tres_alloc(
    state: str, num_cpus: str, tres_alloc: str
) -> tuple[str, str, str]:
    """Extract CPU, RAM, and GPU display strings from tres-alloc."""

    if state.strip().upper() not in RUNNING_STATES_WITH_ALLOCATIONS:
        return (
            EMPTY_RESOURCE_DISPLAY,
            EMPTY_RESOURCE_DISPLAY,
            EMPTY_RESOURCE_DISPLAY,
        )

    entries: dict[str, str] = {}
    for item in tres_alloc.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        entries[key.strip()] = value.strip()

    cpu_value = entries.get("cpu") or num_cpus.strip()
    mem_value = entries.get("mem", "")

    gpu_total = 0.0
    gpu_found = False
    gpu_entry_present = False
    aggregate_gpu_value = entries.get("gres/gpu")
    if aggregate_gpu_value is not None:
        gpu_entry_present = True
        try:
            gpu_total = float(aggregate_gpu_value)
            gpu_found = True
        except ValueError:
            gpu_total = 0.0
            gpu_found = False
    else:
        for key, value in entries.items():
            if not key.startswith("gres/gpu:"):
                continue
            gpu_entry_present = True
            gpu_found = True
            try:
                gpu_total += float(value)
            except ValueError:
                gpu_found = False
                gpu_total = 0.0
                break

    cpus_display = _format_number(cpu_value) if cpu_value else EMPTY_RESOURCE_DISPLAY
    ram_display = (
        _format_memory_display(mem_value) if mem_value else EMPTY_RESOURCE_DISPLAY
    )
    if gpu_found:
        gpus_display = _format_number(f"{gpu_total:g}")
    elif gpu_entry_present:
        gpus_display = EMPTY_RESOURCE_DISPLAY
    else:
        gpus_display = "0"
    return cpus_display, ram_display, gpus_display


def parse_squeue_output(raw: str) -> list[JobInfo]:
    """Parse squeue text output into JobInfo objects."""

    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        return []

    jobs: list[JobInfo] = []
    for line in lines:
        row = _clean_delimited_row(line)
        if _is_header_row(row):
            continue

        cpus_display, ram_display, gpus_display = _parse_tres_alloc(
            state=row[4],
            num_cpus=row[8],
            tres_alloc=row[9],
        )
        jobs.append(
            JobInfo(
                job_id=row[0],
                name=row[1],
                user=row[2],
                partition=row[3],
                state=row[4],
                time=row[5],
                nodes=row[6],
                reason=row[7],
                cpus_display=cpus_display,
                ram_display=ram_display,
                gpus_display=gpus_display,
                node_list=row[10],
            )
        )

    return jobs


def color_for_state(state: str) -> str:
    """Return the Rich color name for a SLURM state."""

    normalized = state.strip().upper()
    if normalized in {"R", "RUNNING"}:
        return "green"
    if normalized in {"PD", "PENDING"}:
        return "yellow"
    if normalized in {"CG", "COMPLETING"}:
        return "cyan"
    if normalized in {"F", "FAILED", "CA", "CANCELLED", "TO", "TIMEOUT"}:
        return "red"
    return "white"


def fetch_queue(
    user: str | None = None,
    job_id: str | None = None,
    state: str | None = None,
) -> SSHResult:
    """Fetch the cluster queue via SSH."""

    command = ["squeue"]
    if user:
        command.extend(["-u", user])
    if job_id:
        command.extend([f"--job={job_id}"])
    if state:
        command.extend([f"--states={state}"])
    command.append("--noheader")
    command.append(f"--Format={DEFAULT_SQUEUE_FORMAT}")
    return run_ssh_command(shlex.join(command))
