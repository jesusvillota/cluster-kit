"""Tests for remote cluster_kit provisioning in the code deployer (sync/code.py)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cluster_kit.sync import code as sync_code_mod
from cluster_kit.sync.code import CodeDeployer


@pytest.fixture
def deployer(monkeypatch, tmp_path):
    monkeypatch.setattr(sync_code_mod, "_find_project_root", lambda *a, **k: tmp_path)
    monkeypatch.setattr(sync_code_mod, "get_remote_base", lambda: "/remote/project")
    monkeypatch.setattr(
        sync_code_mod, "get_canonical_remote_base", lambda: "/remote/project"
    )
    monkeypatch.setattr(sync_code_mod, "get_cluster_host", lambda: "user@cluster")
    return CodeDeployer()


class TestProvisionRemoteClusterKit:
    def test_skips_when_no_remote_venv(self, deployer):
        probe = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(sync_code_mod.subprocess, "run", return_value=probe) as run:
            assert deployer.provision_remote_cluster_kit() is True
        # Only the probe ran — no cleanup, no rsync.
        assert run.call_count == 1
        assert ".venv/lib/python*/site-packages" in run.call_args[0][0][2]

    def test_mirrors_package_when_remote_venv_exists(self, deployer):
        site = "/remote/project/.venv/lib/python3.13/site-packages"
        probe = SimpleNamespace(returncode=0, stdout=f"{site}\n", stderr="")
        cleanup = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(
                sync_code_mod.subprocess, "run", side_effect=[probe, cleanup]
            ) as run,
            patch.object(sync_code_mod.RsyncRunner, "sync", return_value=True) as rsync,
        ):
            assert deployer.provision_remote_cluster_kit() is True

        # Stale package dir and dist-info of any version are removed first.
        cleanup_cmd = run.call_args_list[1][0][0][2]
        expected = f"rm -rf {site}/cluster_kit {site}/cluster_kit-*.dist-info"
        assert cleanup_cmd == expected
        # The local installed package dir is mirrored into site-packages
        # (no trailing slash on source so the leaf dir is created).
        source, dest = rsync.call_args[0][0], rsync.call_args[0][1]
        assert source.endswith("cluster_kit")
        assert dest == f"user@cluster:{site}/"

    def test_fails_when_rsync_fails(self, deployer):
        site = "/remote/project/.venv/lib/python3.13/site-packages"
        probe = SimpleNamespace(returncode=0, stdout=f"{site}\n", stderr="")
        cleanup = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(sync_code_mod.subprocess, "run", side_effect=[probe, cleanup]),
            patch.object(sync_code_mod.RsyncRunner, "sync", return_value=False),
        ):
            assert deployer.provision_remote_cluster_kit() is False

    def test_fails_when_cleanup_fails(self, deployer):
        site = "/remote/project/.venv/lib/python3.13/site-packages"
        probe = SimpleNamespace(returncode=0, stdout=f"{site}\n", stderr="")
        cleanup = SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
        with patch.object(
            sync_code_mod.subprocess, "run", side_effect=[probe, cleanup]
        ):
            assert deployer.provision_remote_cluster_kit() is False

    def test_deploy_calls_provisioning_between_sync_and_verify(self, deployer):
        calls: list[str] = []
        with (
            patch.object(
                sync_code_mod.ClusterConnection, "test_connection", return_value=True
            ),
            patch.object(
                CodeDeployer,
                "verify_local_directories",
                lambda self: calls.append("local") or True,
            ),
            patch.object(
                CodeDeployer,
                "clean_local_cache_step",
                lambda self: calls.append("cache") or 0,
            ),
            patch.object(
                CodeDeployer,
                "provision_remote_base",
                lambda self: calls.append("base") or True,
            ),
            patch.object(
                CodeDeployer,
                "remove_remote_directories",
                lambda self: calls.append("remove") or True,
            ),
            patch.object(
                CodeDeployer,
                "sync_directories",
                lambda self: calls.append("sync") or True,
            ),
            patch.object(
                CodeDeployer,
                "provision_remote_cluster_kit",
                lambda self: calls.append("provision") or True,
            ),
            patch.object(
                CodeDeployer,
                "verify_deployment",
                lambda self: calls.append("verify") or True,
            ),
        ):
            assert deployer.deploy() is True
        assert calls == [
            "local",
            "cache",
            "base",
            "remove",
            "sync",
            "provision",
            "verify",
        ]


class TestProvisionRemoteBase:
    def test_noop_outside_worktree(self, deployer):
        with patch.object(sync_code_mod.subprocess, "run") as run:
            assert deployer.provision_remote_base() is True
        run.assert_not_called()

    def test_shares_everything_except_synced_dirs(
        self, deployer, monkeypatch, tmp_path
    ):
        """Run the generated script for real against a fake remote root."""
        canonical = tmp_path / "project"
        base = tmp_path / "project__my-feature"
        for name in ("src", "runnables", "conda_envs", "output", "data", "_logs_"):
            (canonical / name).mkdir(parents=True)
        (canonical / ".env").write_text("X=1\n")
        (canonical / "THIS_IS.py").write_text("INT_DISK=1\n")

        monkeypatch.setattr(
            sync_code_mod, "get_canonical_remote_base", lambda: str(canonical)
        )
        deployer._remote_base = str(base)

        real_run = subprocess.run
        with patch.object(
            sync_code_mod.subprocess,
            "run",
            side_effect=lambda argv, **kw: real_run(
                ["sh", "-c", argv[2]], capture_output=True, text=True
            ),
        ):
            assert deployer.provision_remote_base() is True

        # Machine-local and expensive state is shared...
        for name in ("conda_envs", "output", "data", ".env", "THIS_IS.py"):
            link = base / name
            assert link.is_symlink(), f"{name} should be a symlink"
            assert Path(os.readlink(link)) == canonical / name
        # ...but the synced dirs and the log tree stay per-worktree.
        for name in ("src", "runnables", "_logs_"):
            assert not (base / name).exists(), f"{name} must not be linked"

    def test_provisioning_is_idempotent(self, deployer, monkeypatch, tmp_path):
        canonical = tmp_path / "project"
        (canonical / "conda_envs").mkdir(parents=True)
        base = tmp_path / "project__my-feature"
        monkeypatch.setattr(
            sync_code_mod, "get_canonical_remote_base", lambda: str(canonical)
        )
        deployer._remote_base = str(base)

        real_run = subprocess.run
        with patch.object(
            sync_code_mod.subprocess,
            "run",
            side_effect=lambda argv, **kw: real_run(
                ["sh", "-c", argv[2]], capture_output=True, text=True
            ),
        ):
            assert deployer.provision_remote_base() is True
            before = sorted(p.name for p in base.iterdir())
            assert deployer.provision_remote_base() is True
        assert sorted(p.name for p in base.iterdir()) == before == ["conda_envs"]

    def test_returns_false_on_ssh_failure(self, deployer, monkeypatch):
        monkeypatch.setattr(
            sync_code_mod, "get_canonical_remote_base", lambda: "/remote/project"
        )
        deployer._remote_base = "/remote/project__my-feature"
        bad = SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
        with patch.object(sync_code_mod.subprocess, "run", return_value=bad):
            assert deployer.provision_remote_base() is False
