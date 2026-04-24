"""Tests for cluster_kit.cli module."""

from __future__ import annotations

import argparse
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

from cluster_kit.cli import build_parser, main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_cluster_env():
    keys = [k for k in os.environ if k.startswith("CLUSTER_")]
    saved = {k: os.environ.pop(k) for k in keys}
    os.environ["CLUSTER_REMOTE_BASE"] = "/tmp/test"
    os.environ["CLUSTER_USER"] = "testuser"
    yield
    for k in keys:
        os.environ.pop(k, None)
    for k, v in saved.items():
        os.environ[k] = v


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


class TestBuildParser:
    def test_parser_returns_argument_parser(self):
        parser = build_parser()
        assert isinstance(parser, argparse.ArgumentParser)

    def test_version_flag(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--version"])
        assert exc.value.code == 0
        captured = capsys.readouterr()
        assert "cluster-kit" in captured.out

    def test_config_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--config"])
        assert args.config is True

    def test_no_command_shows_help(self, capsys):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_unknown_command_exits_error(self, capsys):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["unknown-command"])
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# sync subcommands
# ---------------------------------------------------------------------------


class TestSyncCodeParser:
    def test_sync_code_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "code"])
        assert args.dry_run is False
        assert args.verbose is False

    def test_sync_code_dry_run(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "code", "--dry-run"])
        assert args.dry_run is True

    def test_sync_code_verbose(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "code", "--verbose"])
        assert args.verbose is True


class TestSyncOutputsParser:
    def test_defaults_to_visualization(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "outputs"])
        assert args.mode is None

    def test_all_flag(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "outputs", "--all"])
        assert args.mode == "all"

    def test_processed_flag(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "outputs", "--processed"])
        assert args.mode == "processed"

    def test_formats_arg(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "outputs", "--formats", "pdf,png"])
        assert args.formats == "pdf,png"

    def test_delete_flag(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "outputs", "--delete"])
        assert args.delete is True

    def test_show_tree_flag(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "outputs", "--show-tree"])
        assert args.show_tree is True

    def test_mutually_exclusive_mode(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["sync", "outputs", "--all", "--processed"])


class TestSyncCpParser:
    def test_required_positional_args(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "cp", "src.txt", "dst.txt"])
        assert args.src == "src.txt"
        assert args.dst == "dst.txt"

    def test_recursive_flag(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "cp", "-r", "src/", "dst/"])
        assert args.recursive is True

    def test_dry_run_flag(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "cp", "--dry-run", "src", "dst"])
        assert args.dry_run is True


# ---------------------------------------------------------------------------
# launch subcommand
# ---------------------------------------------------------------------------


class TestLaunchParser:
    def test_required_script_arg(self):
        parser = build_parser()
        args = parser.parse_args(["launch", "script.py"])
        assert args.script == "script.py"

    def test_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["launch", "script.py"])
        assert args.run_from == "cluster"
        assert args.partition == "cpu_shared"
        assert args.slurm_cpus == 16
        assert args.slurm_mem == "64G"
        assert args.slurm_time == "04:00:00"
        assert args.sync is False

    def test_custom_resources(self):
        parser = build_parser()
        args = parser.parse_args([
            "launch", "script.py",
            "--partition", "gpu_compute",
            "--slurm-cpus", "32",
            "--slurm-mem", "122G",
            "--slurm-time", "24:00:00",
            "--sync",
        ])
        assert args.partition == "gpu_compute"
        assert args.slurm_cpus == 32
        assert args.slurm_mem == "122G"
        assert args.slurm_time == "24:00:00"
        assert args.sync is True

    def test_run_from_local(self):
        parser = build_parser()
        args = parser.parse_args(["launch", "script.py", "--run-from", "local"])
        assert args.run_from == "local"


# ---------------------------------------------------------------------------
# tui subcommand
# ---------------------------------------------------------------------------


class TestTuiParser:
    def test_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["tui"])
        assert args.phone is False
        assert args.refresh == 5
        assert args.all_users is False

    def test_phone_flag(self):
        parser = build_parser()
        args = parser.parse_args(["tui", "--phone"])
        assert args.phone is True

    def test_refresh_arg(self):
        parser = build_parser()
        args = parser.parse_args(["tui", "--refresh", "10"])
        assert args.refresh == 10

    def test_all_users_flag(self):
        parser = build_parser()
        args = parser.parse_args(["tui", "--all-users"])
        assert args.all_users is True


# ---------------------------------------------------------------------------
# serve subcommand
# ---------------------------------------------------------------------------


class TestServeParser:
    def test_start_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["serve", "start"])
        assert args.serve_command == "start"

    def test_status_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["serve", "status"])
        assert args.serve_command == "status"

    def test_stop_subcommand(self):
        parser = build_parser()
        args = parser.parse_args(["serve", "stop"])
        assert args.serve_command == "stop"

    def test_start_with_port(self):
        parser = build_parser()
        args = parser.parse_args(["serve", "start", "--port", "8080"])
        assert args.port == 8080

    def test_start_with_phone_ui(self):
        parser = build_parser()
        args = parser.parse_args(["serve", "start", "--phone-ui"])
        assert args.phone_ui is True

    def test_start_with_qa_safe_mode(self):
        parser = build_parser()
        args = parser.parse_args(["serve", "start", "--qa-safe-mode"])
        assert args.qa_safe_mode is True


# ---------------------------------------------------------------------------
# main() integration (mocked)
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_config_calls_config_cmd(self, capsys):
        with patch("cluster_kit.cli._cmd_config") as mock_config:
            with patch.object(sys, "argv", ["cluster-kit", "--config"]):
                with pytest.raises(SystemExit) as exc:
                    main()
                assert exc.value.code == 0
                mock_config.assert_called_once()

    def test_main_no_command_prints_help(self, capsys):
        with patch.object(sys, "argv", ["cluster-kit"]):
            with pytest.raises(SystemExit) as exc:
                main()
            assert exc.value.code == 0
            captured = capsys.readouterr()
            assert "usage" in captured.out.lower()
