"""Tests for the resources snapshot assembly."""

import json

from cluster_kit.cli import _resources_snapshot
from cluster_kit.tui.backend.available_resources import _default_row
from cluster_kit.tui.backend.queue_parser import JobInfo


def test_snapshot_shape_and_json_serializable():
    nodes = [_default_row("HPCOM-01")]
    jobs = [
        JobInfo(
            job_id="42",
            name="alpha",
            user="me",
            partition="cpu_shared",
            state="PD",
            time="0:00",
            nodes="1",
            reason="Resources",
        )
    ]

    snapshot = _resources_snapshot(nodes, jobs)

    assert snapshot["nodes"][0]["node_name"] == "HPCOM-01"
    assert snapshot["nodes"][0]["available_cpus"] == 72
    assert snapshot["queue"][0]["job_id"] == "42"
    assert snapshot["queue"][0]["reason"] == "Resources"
    assert snapshot["partitions"]["cpu_shared"] == {
        "max_cpus": 32,
        "max_mem": "240G",
        "max_time": "24:00:00",
    }
    json.dumps(snapshot)  # must be JSON-serializable end to end
