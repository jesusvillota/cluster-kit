#!/usr/bin/env python3
"""Remote job inspector for cluster-kit detached jobs (shipped to the remote).

Lives at ``<remote_base>/.cluster_kit/jobctl.py`` on the remote machine and is
re-uploaded by the client whenever ``JOBCTL_VERSION`` changes. Prints JSON so
the client never parses ad-hoc shell output.

This file must stay importable on a bare remote python3: standard library
only, no cluster_kit imports, Python 3.8-compatible syntax.

Usage:
    python3 jobctl.py list <jobs_root>
    python3 jobctl.py status <jobs_root> <job_id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys

JOBCTL_VERSION = 1

# Job states
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
DIED = "DIED"


def _read(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None


def _pid_alive(pid, job_dir):
    """True when *pid* is alive AND still the wrapper for *job_dir*.

    The /proc cmdline cross-check guards against pid reuse after a reboot;
    on systems without /proc the liveness signal alone is used.
    """
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    cmdline_path = "/proc/{0}/cmdline".format(pid)
    if not os.path.exists(cmdline_path):
        return True
    try:
        with open(cmdline_path, "rb") as handle:
            cmdline = handle.read().decode("utf-8", "replace")
    except OSError:
        return True
    return job_dir in cmdline


def job_info(jobs_root, job_id):
    """Classify one job directory into a JSON-friendly dict."""
    job_dir = os.path.join(jobs_root, job_id)
    if not os.path.isdir(job_dir):
        return {"job_id": job_id, "state": "UNKNOWN", "error": "no such job"}

    meta = {}
    meta_raw = _read(os.path.join(job_dir, "meta.json"))
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
        except ValueError:
            pass

    rc_raw = _read(os.path.join(job_dir, "rc"))
    pid_raw = _read(os.path.join(job_dir, "pid"))

    if os.path.exists(os.path.join(job_dir, "CANCELLED")):
        state, rc = CANCELLED, rc_raw
    elif rc_raw is not None:
        state = COMPLETED if rc_raw == "0" else FAILED
        rc = rc_raw
    elif pid_raw is not None and _pid_alive(int(pid_raw), job_dir):
        state, rc = RUNNING, None
    else:
        # No exit code and no live wrapper: the machine (or WSL VM) most
        # likely shut down or slept while the job was running.
        state, rc = DIED, None

    return {
        "job_id": job_id,
        "state": state,
        "rc": rc,
        "pid": pid_raw,
        "name": meta.get("name"),
        "command": meta.get("command"),
        "created_at": meta.get("created_at"),
        "git_sha": meta.get("git_sha"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="cluster-kit remote job inspector")
    parser.add_argument("--version", action="version", version=str(JOBCTL_VERSION))
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list")
    list_parser.add_argument("jobs_root")

    status_parser = sub.add_parser("status")
    status_parser.add_argument("jobs_root")
    status_parser.add_argument("job_id")

    args = parser.parse_args(argv)

    if args.command == "list":
        if not os.path.isdir(args.jobs_root):
            print(json.dumps([]))
            return 0
        job_ids = sorted(
            entry
            for entry in os.listdir(args.jobs_root)
            if os.path.isdir(os.path.join(args.jobs_root, entry))
        )
        print(json.dumps([job_info(args.jobs_root, job_id) for job_id in job_ids]))
        return 0

    print(json.dumps(job_info(args.jobs_root, args.job_id)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
