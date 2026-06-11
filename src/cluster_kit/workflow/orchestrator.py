#!/usr/bin/env python3
"""Login-node workflow orchestrator (shipped per run, stdlib only).

Executes a JSON execution plan produced by ``cluster_kit.workflow.plan``:
submits each job's pre-rendered sbatch argv once all its dependencies have
completed, while keeping the user's TOTAL queued job count (pending + running,
including jobs submitted outside this run) below ``max_concurrent`` so the
association MaxSubmit/MaxJobs limit is never violated.

This file must stay importable on a bare cluster login node: standard library
only, no cluster_kit imports, Python 3.8-compatible syntax.

Usage:
    python3 orchestrator.py /path/to/run_dir/plan.json

State is persisted next to the plan as ``state.json`` after every cycle; if
the orchestrator dies, re-running the same command resumes from that state.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import time

# Job states
WAITING = "WAITING"
SUBMITTED = "SUBMITTED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
SKIPPED = "SKIPPED"
SUBMIT_FAILED = "SUBMIT_FAILED"

TERMINAL_STATES = {COMPLETED, FAILED, SKIPPED, SUBMIT_FAILED}
FAILED_LIKE_STATES = {FAILED, SKIPPED, SUBMIT_FAILED}

SACCT_FAILURE_STATES = {
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "BOOT_FAIL",
    "DEADLINE",
    "PREEMPTED",
    "REVOKED",
}

MAX_SUBMIT_RETRIES = 3
MAX_MISSING_POLLS = 5

_terminate_requested = False


def _run(argv, cwd=None):
    """Single subprocess boundary (monkeypatched in tests)."""
    return subprocess.run(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )


def _log(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[{0}] {1}".format(timestamp, message))
    sys.stdout.flush()


def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def load_plan(plan_path):
    with open(plan_path) as handle:
        plan = json.load(handle)
    if plan.get("schema_version") != 1:
        raise SystemExit(
            "Unsupported plan schema_version: {0!r}".format(plan.get("schema_version"))
        )
    return plan


def init_or_resume_state(plan, state_path):
    """Build run state, resuming job states from a previous crash if present."""
    jobs = []
    for job in plan["jobs"]:
        jobs.append(
            {
                "index": job["index"],
                "name": job["name"],
                "stage": job["stage"],
                "state": WAITING,
                "slurm_job_id": None,
                "slurm_state": None,
                "exit_code": None,
                "submit_retries": 0,
                "missing_polls": 0,
            }
        )
    state = {
        "run_id": plan["run_id"],
        "workflow_name": plan["workflow_name"],
        "status": "running",
        "max_concurrent": plan["max_concurrent"],
        "poll_interval": plan["poll_interval"],
        "started_at": _now_iso(),
        "finished_at": None,
        "heartbeat": _now_iso(),
        "jobs": jobs,
    }
    if os.path.exists(state_path):
        try:
            with open(state_path) as handle:
                previous = json.load(handle)
        except (OSError, ValueError):
            _log("WARNING: could not read previous state.json; starting fresh")
            return state
        previous_jobs = {job["index"]: job for job in previous.get("jobs", [])}
        for job in state["jobs"]:
            old = previous_jobs.get(job["index"])
            if old is not None:
                job.update(
                    {
                        "state": old.get("state", WAITING),
                        "slurm_job_id": old.get("slurm_job_id"),
                        "slurm_state": old.get("slurm_state"),
                        "exit_code": old.get("exit_code"),
                        "submit_retries": old.get("submit_retries", 0),
                        "missing_polls": old.get("missing_polls", 0),
                    }
                )
        state["started_at"] = previous.get("started_at", state["started_at"])
        _log("Resumed from existing state.json")
    return state


def write_state(state, state_path):
    state["heartbeat"] = _now_iso()
    tmp_path = state_path + ".tmp"
    with open(tmp_path, "w") as handle:
        json.dump(state, handle, indent=2)
    os.replace(tmp_path, state_path)


def poll_squeue(user):
    """Return the set of ALL queued job ids for the user (the shared budget)."""
    result = _run(["squeue", "-u", user, "-h", "-o", "%i"])
    if result.returncode != 0:
        _log("WARNING: squeue failed: {0}".format(result.stderr.strip()))
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def poll_sacct(job_ids):
    """Return {job_id: (state, exit_code)} for the given ids via sacct."""
    if not job_ids:
        return {}
    result = _run(
        [
            "sacct",
            "-j",
            ",".join(sorted(job_ids)),
            "-X",
            "-n",
            "-P",
            "--format=JobID,State,ExitCode",
        ]
    )
    if result.returncode != 0:
        _log("WARNING: sacct failed: {0}".format(result.stderr.strip()))
        return {}
    states = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        job_id, raw_state, exit_code = parts[0], parts[1], parts[2]
        # Normalize states like "CANCELLED by 12345"
        state = raw_state.split()[0] if raw_state else ""
        states[job_id] = (state, exit_code)
    return states


def refresh_submitted_jobs(state, squeue_ids):
    """Update SUBMITTED jobs from sacct (and squeue as a liveness fallback)."""
    submitted = [job for job in state["jobs"] if job["state"] == SUBMITTED]
    if not submitted:
        return
    sacct_states = poll_sacct([job["slurm_job_id"] for job in submitted])
    for job in submitted:
        info = sacct_states.get(job["slurm_job_id"])
        if info is not None:
            slurm_state, exit_code = info
            job["slurm_state"] = slurm_state
            job["exit_code"] = exit_code
            job["missing_polls"] = 0
            if slurm_state == "COMPLETED":
                job["state"] = COMPLETED
                _log("Job {0} ({1}) COMPLETED".format(job["slurm_job_id"], job["name"]))
            elif slurm_state in SACCT_FAILURE_STATES:
                job["state"] = FAILED
                _log(
                    "Job {0} ({1}) FAILED with state {2} exit {3}".format(
                        job["slurm_job_id"], job["name"], slurm_state, exit_code
                    )
                )
            # PENDING / RUNNING / REQUEUED etc.: stay SUBMITTED
            continue
        # No sacct record yet (accounting lag): rely on squeue presence.
        if squeue_ids is not None and job["slurm_job_id"] in squeue_ids:
            job["missing_polls"] = 0
            continue
        job["missing_polls"] += 1
        _log(
            "WARNING: job {0} ({1}) absent from sacct and squeue "
            "({2}/{3} polls)".format(
                job["slurm_job_id"],
                job["name"],
                job["missing_polls"],
                MAX_MISSING_POLLS,
            )
        )
        if job["missing_polls"] >= MAX_MISSING_POLLS:
            job["state"] = FAILED
            job["slurm_state"] = "LOST"
            _log(
                "Job {0} ({1}) marked FAILED: untrackable via sacct/squeue".format(
                    job["slurm_job_id"], job["name"]
                )
            )


def propagate_skips(state, dependency_mode, deps_by_index):
    """afterok: jobs whose dependencies failed (transitively) become SKIPPED."""
    if dependency_mode != "afterok":
        return
    states_by_index = {job["index"]: job for job in state["jobs"]}
    changed = True
    while changed:
        changed = False
        for job in state["jobs"]:
            if job["state"] != WAITING:
                continue
            dep_states = [
                states_by_index[dep]["state"] for dep in deps_by_index[job["index"]]
            ]
            if any(dep_state in FAILED_LIKE_STATES for dep_state in dep_states):
                job["state"] = SKIPPED
                _log(
                    "Job {0} SKIPPED (dependency failed)".format(job["name"])
                )
                changed = True


def ready_jobs(state, dependency_mode, deps_by_index):
    """WAITING jobs whose dependencies are satisfied, in plan order."""
    states_by_index = {job["index"]: job for job in state["jobs"]}
    ready = []
    for job in state["jobs"]:
        if job["state"] != WAITING:
            continue
        dep_states = [
            states_by_index[dep]["state"] for dep in deps_by_index[job["index"]]
        ]
        if dependency_mode == "afterok":
            satisfied = all(dep_state == COMPLETED for dep_state in dep_states)
        else:  # afterany
            satisfied = all(dep_state in TERMINAL_STATES for dep_state in dep_states)
        if satisfied:
            ready.append(job)
    return ready


def submit_job(job, plan_job, remote_base):
    """Submit one job. Returns 'ok', 'limit' (retry next cycle), or 'error'."""
    log_dir = os.path.join(remote_base, plan_job["log_dir"])
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:
        _log("WARNING: could not create log dir {0}: {1}".format(log_dir, exc))
    result = _run(plan_job["sbatch_argv"], cwd=remote_base)
    if result.returncode == 0:
        match = re.search(r"Submitted batch job (\d+)", result.stdout)
        if match:
            job["state"] = SUBMITTED
            job["slurm_job_id"] = match.group(1)
            job["slurm_state"] = "PENDING"
            _log("Submitted {0} as job {1}".format(job["name"], job["slurm_job_id"]))
            return "ok"
        _log(
            "ERROR: could not parse job id for {0} from: {1}".format(
                job["name"], result.stdout.strip()
            )
        )
    stderr = result.stderr.strip()
    if "AssocMaxSubmitJobLimit" in stderr or "MaxSubmit" in stderr:
        _log(
            "Submit limit hit for {0}; will retry next cycle ({1})".format(
                job["name"], stderr
            )
        )
        return "limit"
    job["submit_retries"] += 1
    _log(
        "ERROR: sbatch failed for {0} (attempt {1}/{2}): {3}".format(
            job["name"], job["submit_retries"], MAX_SUBMIT_RETRIES, stderr
        )
    )
    if job["submit_retries"] >= MAX_SUBMIT_RETRIES:
        job["state"] = SUBMIT_FAILED
        _log("Job {0} marked SUBMIT_FAILED".format(job["name"]))
    return "error"


def finalize_if_done(state, state_path):
    """If every job is terminal, write the final state and return an exit code."""
    if not all(job["state"] in TERMINAL_STATES for job in state["jobs"]):
        return None
    failed = [job for job in state["jobs"] if job["state"] in FAILED_LIKE_STATES]
    state["status"] = "failed" if failed else "finished"
    state["finished_at"] = _now_iso()
    write_state(state, state_path)
    counts = {}
    for job in state["jobs"]:
        counts[job["state"]] = counts.get(job["state"], 0) + 1
    _log(
        "Workflow {0}: {1} ({2})".format(
            state["run_id"],
            state["status"],
            ", ".join(
                "{0}={1}".format(key, value) for key, value in sorted(counts.items())
            ),
        )
    )
    return 1 if failed else 0


def run_cycle(plan, state):
    """One scheduling cycle. Returns True if any submission was attempted."""
    deps_by_index = {job["index"]: job["deps"] for job in plan["jobs"]}
    plan_jobs_by_index = {job["index"]: job for job in plan["jobs"]}

    squeue_ids = poll_squeue(plan["user"])
    refresh_submitted_jobs(state, squeue_ids)
    propagate_skips(state, plan["dependency_mode"], deps_by_index)

    ready = ready_jobs(state, plan["dependency_mode"], deps_by_index)
    if not ready:
        return False
    if squeue_ids is None:
        _log("Skipping submissions this cycle: queue state unknown")
        return False

    budget = plan["max_concurrent"] - len(squeue_ids)
    submitted_now = 0
    for job in ready:
        if budget - submitted_now <= 0:
            break
        outcome = submit_job(job, plan_jobs_by_index[job["index"]], plan["remote_base"])
        if outcome == "ok":
            submitted_now += 1
        elif outcome == "limit":
            break
    return submitted_now > 0


def _handle_sigterm(signum, frame):  # noqa: ARG001
    global _terminate_requested
    _terminate_requested = True


def _sleep_unless_terminated(seconds):
    """Sleep in 1s slices so SIGTERM is honored promptly (PEP 475 would
    otherwise resume a plain time.sleep after the handler runs)."""
    for _ in range(max(1, int(seconds))):
        if _terminate_requested:
            return
        time.sleep(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="cluster-kit workflow orchestrator")
    parser.add_argument("plan_file", help="Path to plan.json")
    args = parser.parse_args(argv)

    plan_path = os.path.abspath(args.plan_file)
    run_dir = os.path.dirname(plan_path)
    state_path = os.path.join(run_dir, "state.json")

    plan = load_plan(plan_path)
    state = init_or_resume_state(plan, state_path)
    signal.signal(signal.SIGTERM, _handle_sigterm)

    _log(
        "Orchestrating {0} ({1} jobs, max_concurrent={2}, poll={3}s, pid={4})".format(
            plan["run_id"],
            len(plan["jobs"]),
            plan["max_concurrent"],
            plan["poll_interval"],
            os.getpid(),
        )
    )

    while True:
        run_cycle(plan, state)
        exit_code = finalize_if_done(state, state_path)
        if exit_code is not None:
            return exit_code
        write_state(state, state_path)
        _sleep_unless_terminated(plan["poll_interval"])
        if _terminate_requested:
            state["status"] = "cancelled"
            state["finished_at"] = _now_iso()
            write_state(state, state_path)
            _log("Terminated by SIGTERM")
            return 143


if __name__ == "__main__":
    sys.exit(main())
