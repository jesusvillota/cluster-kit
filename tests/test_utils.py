"""Tests for cluster_kit.utils and cluster_kit.sync.transfer (path parsing)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from cluster_kit.sync.transfer import (
    ParsedPath,
    parse_path,
    _is_valid_host,
    detect_direction,
    TransferDirection,
)
from cluster_kit.utils.rsync import RsyncRunner, ScpRunner
from cluster_kit.utils.ssh import ClusterConnection


# ---------------------------------------------------------------------------
# ParsedPath
# ---------------------------------------------------------------------------


class TestParsedPath:
    def test_local_path(self):
        p = ParsedPath(host=None, path="/tmp/file.txt", is_remote=False)
        assert p.full_path == "/tmp/file.txt"
        assert not p.is_remote

    def test_remote_path(self):
        p = ParsedPath(host="cluster", path="/remote/file.txt", is_remote=True)
        assert p.full_path == "cluster:/remote/file.txt"
        assert p.is_remote


# ---------------------------------------------------------------------------
# parse_path
# ---------------------------------------------------------------------------


class TestParsePath:
    def test_remote_path(self):
        result = parse_path("cluster:/mnt/data/file.txt")
        assert result.host == "cluster"
        assert result.path == "/mnt/data/file.txt"
        assert result.is_remote is True

    def test_remote_with_user(self):
        result = parse_path("user@cluster:/home/user/file.txt")
        assert result.host == "user@cluster"
        assert result.path == "/home/user/file.txt"
        assert result.is_remote is True

    def test_local_absolute(self):
        result = parse_path("/home/user/file.txt")
        assert result.host is None
        assert result.path == "/home/user/file.txt"
        assert result.is_remote is False

    def test_local_relative(self):
        result = parse_path("relative/path/file.txt")
        assert result.host is None
        assert result.is_remote is False

    def test_local_with_colons(self):
        result = parse_path("./local:path")
        assert result.host is None
        assert result.is_remote is False

    def test_tilde_expansion(self):
        result = parse_path("~/some/path")
        assert result.host is None
        assert str(Path("~").expanduser()) in result.path
        assert result.is_remote is False


# ---------------------------------------------------------------------------
# _is_valid_host
# ---------------------------------------------------------------------------


class TestIsValidHost:
    def test_simple_host(self):
        assert _is_valid_host("cluster") is True

    def test_user_at_host(self):
        assert _is_valid_host("user@cluster") is True

    def test_ip_address(self):
        assert _is_valid_host("192.168.1.1") is True

    def test_hyphenated_host(self):
        assert _is_valid_host("my-host-name") is True

    def test_underscored_host(self):
        assert _is_valid_host("my_host") is True

    def test_empty_string(self):
        assert _is_valid_host("") is False

    def test_starts_with_slash(self):
        assert _is_valid_host("/path") is False

    def test_starts_with_dot(self):
        assert _is_valid_host("./relative") is False

    def test_starts_with_tilde(self):
        assert _is_valid_host("~/home") is False

    def test_contains_slash(self):
        assert _is_valid_host("host/path") is False


# ---------------------------------------------------------------------------
# detect_direction
# ---------------------------------------------------------------------------


class TestDetectDirection:
    def test_local_to_cluster(self):
        src = ParsedPath(host=None, path="/local", is_remote=False)
        dst = ParsedPath(host="cluster", path="/remote", is_remote=True)
        assert detect_direction(src, dst) == TransferDirection.LOCAL_TO_CLUSTER

    def test_cluster_to_local(self):
        src = ParsedPath(host="cluster", path="/remote", is_remote=True)
        dst = ParsedPath(host=None, path="/local", is_remote=False)
        assert detect_direction(src, dst) == TransferDirection.CLUSTER_TO_LOCAL

    def test_local_to_local(self):
        src = ParsedPath(host=None, path="/local1", is_remote=False)
        dst = ParsedPath(host=None, path="/local2", is_remote=False)
        assert detect_direction(src, dst) == TransferDirection.LOCAL_TO_LOCAL

    def test_remote_to_remote_raises(self):
        src = ParsedPath(host="cluster1", path="/r1", is_remote=True)
        dst = ParsedPath(host="cluster2", path="/r2", is_remote=True)
        with pytest.raises(ValueError, match="Cannot transfer between two remote"):
            detect_direction(src, dst)


# ---------------------------------------------------------------------------
# RsyncRunner
# ---------------------------------------------------------------------------


class TestRsyncRunner:
    def test_build_command_preview_basic(self):
        runner = RsyncRunner()
        cmd = runner.build_command_preview("/src", "/dst")
        assert "rsync" in cmd
        assert "/src" in cmd
        assert "/dst" in cmd

    def test_build_command_preview_verbose(self):
        runner = RsyncRunner(verbose=True)
        cmd = runner.build_command_preview("/src", "/dst")
        assert "-v" in cmd

    def test_build_command_preview_dry_run(self):
        runner = RsyncRunner(dry_run=True)
        cmd = runner.build_command_preview("/src", "/dst")
        assert "--dry-run" in cmd

    def test_build_command_preview_delete(self):
        runner = RsyncRunner(delete=True)
        cmd = runner.build_command_preview("/src", "/dst")
        assert "--delete" in cmd

    def test_build_command_preview_includes_excludes(self):
        runner = RsyncRunner()
        cmd = runner.build_command_preview(
            "/src", "/dst",
            includes=["*.pdf"],
            excludes=["*.tmp"],
        )
        assert "--include" in cmd
        assert "*.pdf" in cmd
        assert "--exclude" in cmd
        assert "*.tmp" in cmd

    def test_build_command_preview_always_excludes_pycache(self):
        runner = RsyncRunner()
        cmd = runner.build_command_preview("/src", "/dst")
        assert "__pycache__" in cmd
        assert "*.pyc" in cmd
        assert "*.pyo" in cmd

    @patch("subprocess.run")
    def test_sync_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        runner = RsyncRunner()
        result = runner.sync("/src", "/dst")
        assert result is True

    @patch("subprocess.run")
    def test_sync_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        runner = RsyncRunner()
        result = runner.sync("/src", "/dst")
        assert result is False


# ---------------------------------------------------------------------------
# ScpRunner
# ---------------------------------------------------------------------------


class TestScpRunner:
    def test_dry_run_returns_true(self, capsys):
        runner = ScpRunner(dry_run=True)
        result = runner.sync("/src", "/dst")
        assert result is True
        captured = capsys.readouterr()
        assert "dry-run" in captured.out.lower()

    @patch("subprocess.run")
    def test_sync_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        runner = ScpRunner()
        result = runner.sync("/src", "cluster:/dst")
        assert result is True

    @patch("subprocess.run")
    def test_sync_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="permission denied")
        runner = ScpRunner()
        result = runner.sync("/src", "cluster:/dst")
        assert result is False


# ---------------------------------------------------------------------------
# ClusterConnection (mocked SSH)
# ---------------------------------------------------------------------------


class TestClusterConnection:
    @patch("cluster_kit.utils.ssh.get_cluster_host", return_value="test-cluster")
    @patch("cluster_kit.utils.ssh.get_ssh_timeout", return_value=5)
    @patch("subprocess.run")
    def test_connection_success(self, mock_run, mock_timeout, mock_host):
        mock_run.return_value = MagicMock(returncode=0)
        result = ClusterConnection.test_connection(verbose=False)
        assert result is True

    @patch("cluster_kit.utils.ssh.get_cluster_host", return_value="test-cluster")
    @patch("cluster_kit.utils.ssh.get_ssh_timeout", return_value=5)
    @patch("subprocess.run")
    def test_connection_failure(self, mock_run, mock_timeout, mock_host):
        mock_run.return_value = MagicMock(returncode=255)
        result = ClusterConnection.test_connection(verbose=False)
        assert result is False

    @patch("cluster_kit.utils.ssh.get_cluster_host", return_value="test-cluster")
    @patch("cluster_kit.utils.ssh.get_ssh_timeout", return_value=5)
    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 5))
    def test_connection_timeout(self, mock_run, mock_timeout, mock_host):
        result = ClusterConnection.test_connection(verbose=False)
        assert result is False
