"""Tests for the shared remote deployment lock."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest

from cluster_kit.sync.lock import (
    RemoteDeployLock,
    RemoteDeployLockError,
    deploy_lock_path,
)


class FakeProc:
    def __init__(self, ready: str = "cluster-kit-deploy-lock-acquired\n"):
        self.stdin = StringIO()
        self.stdout = StringIO(ready)
        self.stderr = StringIO("")
        self.killed = False
        self.terminated = False
        self.waited = False

    def wait(self, timeout=None):  # noqa: ARG002
        self.waited = True
        return 0

    def kill(self):
        self.killed = True

    def terminate(self):
        self.terminated = True


def test_deploy_lock_path_is_remote_base_scoped() -> None:
    assert deploy_lock_path("/remote/a") == "/remote/a/.cluster_kit/deploy.lock"
    assert deploy_lock_path("/remote/b") == "/remote/b/.cluster_kit/deploy.lock"


def test_remote_deploy_lock_holds_ssh_process_until_exit() -> None:
    proc = FakeProc()
    with patch("cluster_kit.sync.lock.subprocess.Popen", return_value=proc) as popen:
        with RemoteDeployLock(
            host="user@cluster",
            remote_base="/remote/a",
            purpose="test",
        ):
            assert popen.call_args.args[0][0] == "ssh"
            assert "user@cluster" in popen.call_args.args[0]
            assert "ConnectTimeout=30" in popen.call_args.args[0]
            assert "/remote/a/.cluster_kit/deploy.lock" in popen.call_args.args[0][-1]
            assert not proc.stdin.closed

    assert proc.stdin.closed
    assert proc.waited


def test_remote_deploy_lock_reports_remote_failure() -> None:
    proc = FakeProc(ready="")
    proc.stderr = StringIO("python3 missing")
    with patch("cluster_kit.sync.lock.subprocess.Popen", return_value=proc):
        with pytest.raises(RemoteDeployLockError, match="python3 missing"):
            with RemoteDeployLock(
                host="user@cluster",
                remote_base="/remote/a",
                purpose="test",
            ):
                pass
    assert proc.killed
