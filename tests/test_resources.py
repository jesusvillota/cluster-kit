"""Tests for the resources snapshot assembly."""

import json

from cluster_kit.cli import _resources_snapshot
from cluster_kit.tui.backend.available_resources import _default_row, parse_sinfo_output
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
    assert snapshot["nodes"][0]["node_state"] == "unknown"
    assert snapshot["nodes"][0]["available_cpus"] == 0
    assert snapshot["queue"][0]["job_id"] == "42"
    assert snapshot["queue"][0]["reason"] == "Resources"
    assert snapshot["partitions"]["cpu_shared"] == {
        "max_cpus": 32,
        "max_mem": "240G",
        "max_time": "24:00:00",
    }
    json.dumps(snapshot)  # must be JSON-serializable end to end


def test_down_node_has_no_available_resources():
    for state in ("down*", "down+not_responding"):
        rows = {
            row.node_name: row
            for row in parse_sinfo_output(
                "HPCOM-01|idle|0/72/0/72|0|gpu:0\n"
                f"HPCOM-05|{state}|0/64/0/64|0|gpu:0\n"
            )
        }

        assert rows["HPCOM-01"].node_state == "idle"
        assert rows["HPCOM-01"].available_cpus == 72
        assert rows["HPCOM-05"].node_state == state
        assert rows["HPCOM-05"].available_cpus == 0
        assert rows["HPCOM-05"].available_memory_gb == 0
        assert rows["HPCOM-05"].available_gpus == 0
