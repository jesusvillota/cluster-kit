"""Tests for the notify queue-summary builder."""

from cluster_kit.notify import build_summary
from cluster_kit.tui.backend.queue_parser import JobInfo


def _job(job_id, name, partition, state, time, reason=""):
    return JobInfo(
        job_id=job_id,
        name=name,
        user="me",
        partition=partition,
        state=state,
        time=time,
        nodes="1",
        reason=reason,
    )


def test_build_summary_counts_and_rows():
    jobs = [
        _job("1", "alpha", "cpu_shared", "R", "1:00"),
        _job("2", "beta", "cpu_large", "PD", "0:00", reason="Priority"),
    ]
    summary = build_summary(jobs, "me")
    lines = summary.splitlines()

    assert lines[0] == "🐋 cluster queue (me): 1 running, 1 pending"
    assert lines[1] == "• 1 alpha [cpu_shared] R 1:00"
    # pending rows carry the reason so the phone tells you *why* it waits
    assert lines[2] == "• 2 beta [cpu_large] PD Priority 0:00"


def test_build_summary_empty():
    assert build_summary([], "me") == "🐋 cluster queue (me): 0 running, 0 pending"
