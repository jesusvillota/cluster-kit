"""Tests for cluster_kit.cli module."""

from __future__ import annotations

import argparse
import os
import sys
from unittest.mock import patch

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
# workflow subcommand
# ---------------------------------------------------------------------------


class TestWorkflowParser:
    def test_workflow_run_required_file(self):
        parser = build_parser()
        args = parser.parse_args(["workflow", "run", "workflow.toml"])
        assert args.workflow_command == "run"
        assert args.workflow_file == "workflow.toml"
        assert args.dry_run is False
        assert args.project_root is None
        assert args.no_sync is False
        assert args.dependency is None
        assert args.worker_script is None

    def test_workflow_run_options(self):
        parser = build_parser()
        args = parser.parse_args([
            "workflow",
            "run",
            "workflow.toml",
            "--dry-run",
            "--project-root",
            "/tmp/project",
            "--no-sync",
            "--dependency",
            "afterany",
            "--worker-script",
            "runnables/slurm/custom_worker.slurm",
        ])
        assert args.dry_run is True
        assert args.project_root == "/tmp/project"
        assert args.no_sync is True
        assert args.dependency == "afterany"
        assert args.worker_script == "runnables/slurm/custom_worker.slurm"

    def test_workflow_run_local_options(self):
        parser = build_parser()
        args = parser.parse_args([
            "workflow",
            "run-local",
            "workflow.yaml",
            "--dry-run",
            "--project-root",
            "/tmp/project",
        ])
        assert args.workflow_command == "run-local"
        assert args.workflow_file == "workflow.yaml"
        assert args.dry_run is True
        assert args.project_root == "/tmp/project"

    def test_workflow_invalid_dependency_exits(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "workflow",
                "run",
                "workflow.toml",
                "--dependency",
                "invalid",
            ])


# ---------------------------------------------------------------------------
# tui subcommand
# ---------------------------------------------------------------------------


class TestTuiParser:
    def test_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["tui"])
        assert args.phone is False
        assert args.refresh == 60
        assert args.all_users is True

    def test_phone_flag(self):
        parser = build_parser()
        args = parser.parse_args(["tui", "--phone"])
        assert args.phone is True

    def test_refresh_arg(self):
        parser = build_parser()
        args = parser.parse_args(["tui", "--refresh", "10"])
        assert args.refresh == 10

    def test_user_only_flag(self):
        parser = build_parser()
        args = parser.parse_args(["tui", "--user-only"])
        assert args.all_users is False


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


# ---------------------------------------------------------------------------
# job / exec subcommands and profile flag
# ---------------------------------------------------------------------------


class TestJobParser:
    def test_job_submit(self):
        parser = build_parser()
        args = parser.parse_args(
            ["job", "submit", "uv run src/a.py", "--name", "panel"]
        )
        assert args.job_command == "submit"
        assert args.command == "uv run src/a.py"
        assert args.name == "panel"

    def test_job_list(self):
        parser = build_parser()
        args = parser.parse_args(["job", "list"])
        assert args.job_command == "list"

    def test_job_status(self):
        parser = build_parser()
        args = parser.parse_args(["job", "status", "panel_20260612-100000_ab12"])
        assert args.job_command == "status"
        assert args.job_id == "panel_20260612-100000_ab12"

    def test_job_logs_defaults(self):
        parser = build_parser()
        args = parser.parse_args(["job", "logs", "x"])
        assert args.lines == 50
        assert args.follow is False

    def test_job_logs_follow(self):
        parser = build_parser()
        args = parser.parse_args(["job", "logs", "x", "-f", "-n", "200"])
        assert args.follow is True
        assert args.lines == 200

    def test_job_cancel_force(self):
        parser = build_parser()
        args = parser.parse_args(["job", "cancel", "x", "--force"])
        assert args.job_command == "cancel"
        assert args.force is True

    def test_job_requires_subcommand(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["job"])


class TestExecParser:
    def test_exec_command(self):
        parser = build_parser()
        args = parser.parse_args(["exec", "duckdb -c 'select 42'"])
        assert args.remote_command == "duckdb -c 'select 42'"


class TestProfileFlag:
    def test_profile_flag_parses(self):
        parser = build_parser()
        args = parser.parse_args(["-p", "pc", "job", "list"])
        assert args.profile == "pc"

    def test_profile_sets_cluster_env(self):
        os.environ["CLUSTER_PC_REMOTE_BASE"] = "/home/wsluser/project"
        os.environ["CLUSTER_PC_EXECUTOR"] = "ssh"
        with patch("cluster_kit.cli._cmd_config") as mock_config:
            with patch.object(
                sys, "argv", ["cluster-kit", "--profile", "pc", "--config"]
            ):
                with pytest.raises(SystemExit):
                    main()
                mock_config.assert_called_once()
        assert os.environ["CLUSTER_ENV"] == "pc"


class TestSyncCodeForceFlag:
    def test_force_default_false(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "code"])
        assert args.force is False

    def test_force_flag(self):
        parser = build_parser()
        args = parser.parse_args(["sync", "code", "--force"])
        assert args.force is True
