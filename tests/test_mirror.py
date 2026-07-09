"""Tests for the cluster⇄PC data mirror (sync/mirror.py)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cluster_kit.sync import mirror
from cluster_kit.sync.mirror import (
    MirrorError,
    _rsync_cmd,
    load_manifest,
    mirror_dataset,
    read_mirror_state,
    run_mirror,
)

MANIFEST = """
cluster_from_pc: j-vill36@192.168.1.61
datasets:
  whale_outputs:
    cluster: /mnt/beegfs/scripts_whales/output/processed
    pc: /home/user/GitHub/whales/output/processed
    exclude: ["*.tmp"]
"""


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    path = tmp_path / "mirror.yaml"
    path.write_text(MANIFEST)
    return path


@pytest.fixture(autouse=True)
def state_path(tmp_path: Path, monkeypatch):
    path = tmp_path / "mirror_state.json"
    monkeypatch.setattr(mirror, "MIRROR_STATE_PATH", path)
    return path


class TestManifest:
    def test_valid(self, manifest_path: Path):
        data = load_manifest(manifest_path)
        assert data["cluster_from_pc"] == "j-vill36@192.168.1.61"
        assert "whale_outputs" in data["datasets"]

    def test_missing_file(self, tmp_path: Path):
        with pytest.raises(MirrorError, match="not found"):
            load_manifest(tmp_path / "nope.yaml")

    def test_missing_cluster_from_pc(self, tmp_path: Path):
        path = tmp_path / "m.yaml"
        path.write_text("datasets:\n  a:\n    cluster: /a\n    pc: /b\n")
        with pytest.raises(MirrorError, match="cluster_from_pc"):
            load_manifest(path)

    def test_dataset_missing_path(self, tmp_path: Path):
        path = tmp_path / "m.yaml"
        path.write_text("cluster_from_pc: u@h\ndatasets:\n  a:\n    cluster: /a\n")
        with pytest.raises(MirrorError, match="'a'"):
            load_manifest(path)


class TestRsyncCmd:
    def test_basic(self):
        cmd = _rsync_cmd("u@h:/a/", "/b/", [], dry_run=False)
        assert cmd == (
            "rsync -az --update -e 'ssh -o BatchMode=yes' u@h:/a/ /b/"
        )

    def test_excludes_and_dry_run(self):
        cmd = _rsync_cmd("/b/", "u@h:/a/", ["*.tmp"], dry_run=True)
        assert "--dry-run -v" in cmd
        assert "--exclude '*.tmp'" in cmd


class TestDryRunDataset:
    """Dry runs stay synchronous (fast: proportional to file count, not data)."""

    SPEC = {
        "cluster": "/mnt/beegfs/out/",
        "pc": "/home/u/out",
        "exclude": [],
    }

    def _run(self, results=None):
        commands: list[str] = []

        def fake_run_remote(command, **kw):
            commands.append(command)
            rc = 0 if results is None else results[len(commands) - 1]
            return SimpleNamespace(returncode=rc, stdout="", stderr="boom")

        with patch.object(mirror, "run_remote", fake_run_remote):
            ok = mirror_dataset(
                "whale_outputs",
                self.SPEC,
                cluster_from_pc="u@cluster",
                pc_config=None,
                dry_run=True,
            )
        return ok, commands

    def test_two_union_passes(self):
        ok, commands = self._run()
        assert ok is True
        assert len(commands) == 2
        assert commands[0].startswith("mkdir -p /home/u/out && rsync")
        assert "u@cluster:/mnt/beegfs/out/ /home/u/out/" in commands[0]
        assert "/home/u/out/ u@cluster:/mnt/beegfs/out/" in commands[1]
        assert all("--update" in c and "--dry-run" in c for c in commands)
        assert all("--delete" not in c for c in commands)

    def test_failed_pass_stops_and_never_writes_state(self, state_path: Path):
        ok, commands = self._run(results=[1])
        assert ok is False
        assert len(commands) == 1
        assert not state_path.exists()

    def test_dry_run_never_writes_state(self, state_path: Path):
        self._run()
        assert not state_path.exists()


class TestRealMirrorDataset:
    """Real transfers submit a detached PC job and poll it to completion."""

    SPEC = {
        "cluster": "/mnt/beegfs/out/",
        "pc": "/home/u/out",
        "exclude": [],
    }

    def _run(self, *, states, submit_side_effect=None):
        from cluster_kit.jobs.manager import JobHandle

        handle = JobHandle(
            job_id="mirror-whale_outputs_x",
            job_dir="/home/u/.cluster_kit/jobs/mirror-whale_outputs_x",
            log_path="/home/u/.cluster_kit/jobs/mirror-whale_outputs_x/log",
            command="mkdir -p ...",
        )
        submit_calls: list[str] = []

        def fake_submit(command, *, name, config):
            submit_calls.append(command)
            if submit_side_effect:
                raise submit_side_effect
            return handle

        status_iter = iter(states)

        def fake_job_status(job_id, *, config):
            return next(status_iter)

        with (
            patch.object(mirror, "list_jobs", return_value=[]),
            patch.object(mirror, "submit", fake_submit),
            patch.object(mirror, "job_status", fake_job_status),
            patch.object(mirror, "read_log", return_value="tail of log"),
            patch.object(mirror, "time") as mock_time,
        ):
            mock_time.monotonic.side_effect = range(0, 10_000, 1)
            mock_time.sleep = lambda *_: None
            ok = mirror_dataset(
                "whale_outputs",
                self.SPEC,
                cluster_from_pc="u@cluster",
                pc_config=None,
            )
        return ok, submit_calls

    def test_submits_combined_two_pass_command(self):
        ok, submit_calls = self._run(states=[{"state": "COMPLETED", "rc": "0"}])
        assert ok is True
        assert len(submit_calls) == 1
        command = submit_calls[0]
        assert command.startswith("mkdir -p /home/u/out && rsync")
        assert " && rsync" in command.split("&&", 1)[1]
        assert "u@cluster:/mnt/beegfs/out/ /home/u/out/" in command
        assert "/home/u/out/ u@cluster:/mnt/beegfs/out/" in command

    def test_polls_while_running_then_succeeds(self):
        ok, _ = self._run(
            states=[
                {"state": "RUNNING"},
                {"state": "RUNNING"},
                {"state": "COMPLETED", "rc": "0"},
            ]
        )
        assert ok is True
        state = read_mirror_state()
        assert state["whale_outputs"]["ok"] is True
        assert state["whale_outputs"]["last_success"]

    def test_failed_job_writes_error_state_with_log_tail(self):
        ok, _ = self._run(states=[{"state": "FAILED", "rc": "1"}])
        assert ok is False
        state = read_mirror_state()
        assert state["whale_outputs"]["ok"] is False
        assert "tail of log" in state["whale_outputs"]["detail"]
        assert "last_success" not in state["whale_outputs"]

    def test_died_job_is_treated_as_failure(self):
        ok, _ = self._run(states=[{"state": "DIED"}])
        assert ok is False
        assert read_mirror_state()["whale_outputs"]["ok"] is False

    def test_poll_error_retries_instead_of_failing(self):
        from cluster_kit.jobs.manager import JobError

        calls = {"n": 0}

        def fake_job_status(job_id, *, config):
            calls["n"] += 1
            if calls["n"] == 1:
                raise JobError("ssh blip")
            return {"state": "COMPLETED", "rc": "0"}

        from cluster_kit.jobs.manager import JobHandle

        handle = JobHandle(job_id="x", job_dir="/d", log_path="/d/log", command="cmd")
        with (
            patch.object(mirror, "list_jobs", return_value=[]),
            patch.object(mirror, "submit", return_value=handle),
            patch.object(mirror, "job_status", fake_job_status),
            patch.object(mirror, "time") as mock_time,
        ):
            mock_time.monotonic.side_effect = range(0, 10_000, 1)
            mock_time.sleep = lambda *_: None
            ok = mirror_dataset(
                "whale_outputs",
                self.SPEC,
                cluster_from_pc="u@cluster",
                pc_config=None,
            )
        assert ok is True
        assert calls["n"] == 2

    def test_reuses_already_running_job_instead_of_resubmitting(self):
        running_jobs = [
            {"job_id": "mirror-whale_outputs_earlier", "name": "mirror-whale_outputs",
             "state": "RUNNING"},
            {"job_id": "unrelated_job", "name": "unrelated", "state": "RUNNING"},
        ]
        with (
            patch.object(mirror, "list_jobs", return_value=running_jobs),
            patch.object(mirror, "submit") as fake_submit,
            patch.object(
                mirror, "job_status", return_value={"state": "COMPLETED", "rc": "0"}
            ) as fake_status,
            patch.object(mirror, "time") as mock_time,
        ):
            mock_time.monotonic.side_effect = range(0, 10_000, 1)
            mock_time.sleep = lambda *_: None
            ok = mirror_dataset(
                "whale_outputs",
                self.SPEC,
                cluster_from_pc="u@cluster",
                pc_config=None,
            )
        assert ok is True
        fake_submit.assert_not_called()
        assert fake_status.call_args.args[0] == "mirror-whale_outputs_earlier"

    def test_timeout_does_not_overwrite_state_with_false_failure(
        self, state_path: Path
    ):
        from cluster_kit.jobs.manager import JobHandle

        handle = JobHandle(job_id="x", job_dir="/d", log_path="/d/log", command="cmd")
        mirror._write_state("whale_outputs", True, "")
        prior_state = read_mirror_state()

        with (
            patch.object(mirror, "list_jobs", return_value=[]),
            patch.object(mirror, "submit", return_value=handle),
            patch.object(mirror, "job_status", return_value={"state": "RUNNING"}),
            patch.object(mirror, "time") as mock_time,
        ):
            # First monotonic() call sets the deadline; make the very next
            # one already exceed it so the loop gives up immediately.
            mock_time.monotonic.side_effect = [0, 999_999]
            mock_time.sleep = lambda *_: None
            ok = mirror_dataset(
                "whale_outputs",
                self.SPEC,
                cluster_from_pc="u@cluster",
                pc_config=None,
            )
        assert ok is False
        # State from before the timeout must survive untouched — we
        # genuinely don't know the job's outcome, so we must not record a
        # false failure over a possibly-still-successful prior state.
        assert read_mirror_state() == prior_state


class TestRunMirror:
    def test_unknown_dataset(self, manifest_path: Path):
        with pytest.raises(MirrorError, match="not in manifest"):
            with patch.object(mirror, "load_config"):
                run_mirror(manifest_path, dataset="nope")

    def test_mirrors_all_datasets(self, manifest_path: Path):
        with (
            patch.object(mirror, "load_config"),
            patch.object(mirror, "ensure_reachable"),
            patch.object(
                mirror, "mirror_dataset", return_value=True
            ) as mocked,
        ):
            assert run_mirror(manifest_path) is True
        assert mocked.call_count == 1
        assert mocked.call_args.args[0] == "whale_outputs"


class TestState:
    def test_missing_or_corrupt_state_is_empty(self, state_path: Path):
        assert read_mirror_state() == {}
        state_path.write_text("{not json")
        assert read_mirror_state() == {}

    def test_state_round_trip(self, state_path: Path):
        mirror._write_state("a", True, "")
        mirror._write_state("b", False, "err")
        state = json.loads(state_path.read_text())
        assert state["a"]["ok"] is True
        assert state["b"]["ok"] is False and state["b"]["detail"] == "err"
