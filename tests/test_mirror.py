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


class TestMirrorDataset:
    SPEC = {
        "cluster": "/mnt/beegfs/out/",
        "pc": "/home/u/out",
        "exclude": [],
    }

    def _run(self, results=None, **kwargs):
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
                **kwargs,
            )
        return ok, commands

    def test_two_union_passes(self):
        ok, commands = self._run()
        assert ok is True
        assert len(commands) == 2
        # pass 1: cluster→pc, with mkdir; trailing slashes both ends
        assert commands[0].startswith("mkdir -p /home/u/out && rsync")
        assert "u@cluster:/mnt/beegfs/out/ /home/u/out/" in commands[0]
        # pass 2: pc→cluster
        assert "/home/u/out/ u@cluster:/mnt/beegfs/out/" in commands[1]
        assert all("--update" in c and "--delete" not in c for c in commands)

    def test_success_writes_state(self):
        self._run()
        state = read_mirror_state()
        assert state["whale_outputs"]["ok"] is True
        assert state["whale_outputs"]["last_success"]

    def test_failed_pass_writes_error_state_and_stops(self):
        ok, commands = self._run(results=[1])
        assert ok is False
        assert len(commands) == 1
        state = read_mirror_state()
        assert state["whale_outputs"]["ok"] is False
        assert "boom" in state["whale_outputs"]["detail"]
        assert "last_success" not in state["whale_outputs"]

    def test_dry_run_never_writes_state(self, state_path: Path):
        ok, commands = self._run(dry_run=True)
        assert ok is True
        assert all("--dry-run" in c for c in commands)
        assert not state_path.exists()


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
