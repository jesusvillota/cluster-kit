"""Tests for remote cluster_kit provisioning in the code deployer (sync/code.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cluster_kit.sync import code as sync_code_mod
from cluster_kit.sync.code import CodeDeployer


@pytest.fixture
def deployer(monkeypatch, tmp_path):
    monkeypatch.setattr(sync_code_mod, "_find_project_root", lambda *a, **k: tmp_path)
    monkeypatch.setattr(sync_code_mod, "get_remote_base", lambda: "/remote/project")
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
        assert calls == ["local", "cache", "remove", "sync", "provision", "verify"]
