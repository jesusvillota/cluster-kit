"""Tests for detached-job management (jobs package + jobctl)."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cluster_kit.config import ClusterConfig
from cluster_kit.jobs import JobError, cancel, submit
from cluster_kit.jobs.jobctl import job_info
from cluster_kit.jobs.manager import generate_job_id, render_job_script


def _config(executor: str = "ssh") -> ClusterConfig:
    return ClusterConfig(
        host="pc",
        user="wsluser",
        remote_base=PurePosixPath("/home/wsluser/GitHub/project"),
        ssh_key=Path("~/.ssh/id_ed25519_pc"),
        ssh_timeout=10,
        sync_exclude="__pycache__",
        executor=executor,
        sync_mode="git",
    )


# ---------------------------------------------------------------------------
# Job id + wrapper rendering
# ---------------------------------------------------------------------------


class TestJobIdAndScript:
    def test_job_id_scheme(self):
        job_id = generate_job_id("process data!")
        assert re.fullmatch(r"process_data._\d{8}-\d{6}_[0-9a-f]{4}", job_id)

    def test_job_ids_are_unique_within_a_second(self):
        ids = {generate_job_id("x") for _ in range(20)}
        assert len(ids) == 20

    def test_wrapper_writes_pid_rc_and_path(self):
        script = render_job_script(
            "/base/.cluster_kit/jobs/x_1", "/base", "uv run src/a.py --flag"
        )
        lines = script.splitlines()
        assert lines[0] == "#!/bin/bash"
        # The wrapper records its own $$ (setsid forks; $! is unreliable).
        assert "echo $$ > /base/.cluster_kit/jobs/x_1/pid" in script
        assert 'export PATH="$HOME/.local/bin:$PATH"' in script
        assert "cd /base" in script
        assert "uv run src/a.py --flag" in script
        assert "echo $? > /base/.cluster_kit/jobs/x_1/rc" in script
        # pid is captured before the command runs, rc after.
        assert lines.index("uv run src/a.py --flag") > lines.index(
            "echo $$ > /base/.cluster_kit/jobs/x_1/pid"
        )


# ---------------------------------------------------------------------------
# submit / cancel remote command construction
# ---------------------------------------------------------------------------


class TestSubmit:
    def test_submit_requires_ssh_executor(self):
        with pytest.raises(JobError, match="ssh-executor"):
            submit("uv run src/a.py", config=_config(executor="slurm"))

    def test_submit_rejects_empty_command(self):
        with pytest.raises(JobError, match="empty"):
            submit("   ", config=_config())

    def test_submit_builds_detached_launch(self):
        remote_calls: list[str] = []
        uploads: list[list[str]] = []

        def fake_run_remote(command, **kwargs):
            remote_calls.append(command)
            if "rev-parse" in command:
                return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
            if "--version" in command:
                return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def fake_scp(paths, remote_dir, config):
            uploads.append([Path(p).name for p in paths])

        with (
            patch("cluster_kit.jobs.manager.ensure_reachable"),
            patch("cluster_kit.jobs.manager.run_remote", fake_run_remote),
            patch("cluster_kit.jobs.manager._scp", fake_scp),
        ):
            handle = submit("uv run src/process.py --years 2015", config=_config())

        assert handle.job_id.startswith("process_")
        assert handle.job_dir.startswith(
            "/home/wsluser/GitHub/project/.cluster_kit/jobs/"
        )
        assert handle.log_path == f"{handle.job_dir}/log"
        assert ["job.sh", "meta.json"] in uploads

        launch = next(cmd for cmd in remote_calls if "setsid nohup" in cmd)
        assert launch.startswith("bash -c ")
        assert "job.sh" in launch
        assert "< /dev/null" in launch
        assert ">> " in launch and "2>&1" in launch

    def test_submit_fails_when_mkdir_fails(self):
        def fake_run_remote(command, **kwargs):
            if command.startswith("mkdir"):
                return SimpleNamespace(returncode=1, stdout="", stderr="denied")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("cluster_kit.jobs.manager.ensure_reachable"),
            patch("cluster_kit.jobs.manager.run_remote", fake_run_remote),
            pytest.raises(JobError, match="job dir"),
        ):
            submit("uv run src/a.py", config=_config())


class TestCancel:
    def test_cancel_kills_whole_process_group(self):
        remote_calls: list[str] = []

        def fake_run_remote(command, **kwargs):
            remote_calls.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("cluster_kit.jobs.manager.run_remote", fake_run_remote):
            assert cancel("job_x", config=_config()) is True

        command = remote_calls[0]
        assert 'kill -TERM -- "-$pid"' in command
        assert "kill -KILL" not in command
        assert "touch" in command and "CANCELLED" in command

    def test_cancel_force_adds_sigkill(self):
        remote_calls: list[str] = []

        def fake_run_remote(command, **kwargs):
            remote_calls.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch("cluster_kit.jobs.manager.run_remote", fake_run_remote):
            cancel("job_x", force=True, config=_config())

        assert 'kill -KILL -- "-$pid"' in remote_calls[0]


# ---------------------------------------------------------------------------
# jobctl status classification (runs locally against tmp dirs)
# ---------------------------------------------------------------------------


class TestJobctlClassification:
    def _make_job(self, tmp_path: Path, job_id: str, **files: str) -> Path:
        job_dir = tmp_path / job_id
        job_dir.mkdir()
        (job_dir / "meta.json").write_text(
            json.dumps({"name": "demo", "command": "uv run src/a.py"})
        )
        for filename, content in files.items():
            (job_dir / filename).write_text(content)
        return job_dir

    def test_completed(self, tmp_path: Path):
        self._make_job(tmp_path, "j1", rc="0")
        info = job_info(str(tmp_path), "j1")
        assert info["state"] == "COMPLETED"
        assert info["rc"] == "0"

    def test_failed(self, tmp_path: Path):
        self._make_job(tmp_path, "j1", rc="2")
        assert job_info(str(tmp_path), "j1")["state"] == "FAILED"

    def test_cancelled_marker_wins(self, tmp_path: Path):
        self._make_job(tmp_path, "j1", rc="1", CANCELLED="")
        assert job_info(str(tmp_path), "j1")["state"] == "CANCELLED"

    def test_running_when_pid_alive(self, tmp_path: Path):
        import os

        self._make_job(tmp_path, "j1", pid=str(os.getpid()))
        with patch("cluster_kit.jobs.jobctl._pid_alive", return_value=True):
            assert job_info(str(tmp_path), "j1")["state"] == "RUNNING"

    def test_died_when_no_rc_and_no_process(self, tmp_path: Path):
        self._make_job(tmp_path, "j1", pid="999999")
        with patch("cluster_kit.jobs.jobctl._pid_alive", return_value=False):
            assert job_info(str(tmp_path), "j1")["state"] == "DIED"

    def test_unknown_job(self, tmp_path: Path):
        assert job_info(str(tmp_path), "missing")["state"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# jobctl version-check caching
# ---------------------------------------------------------------------------


class TestJobctlCache:
    def test_version_probe_runs_once_per_host(self):
        from cluster_kit.jobs import manager
        from cluster_kit.jobs.manager import list_jobs

        manager._jobctl_verified.clear()
        remote_calls: list[str] = []

        def fake_run_remote(command, **kwargs):
            remote_calls.append(command)
            if "--version" in command:
                return SimpleNamespace(
                    returncode=0, stdout=f"{manager.JOBCTL_VERSION}\n", stderr=""
                )
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")

        with patch("cluster_kit.jobs.manager.run_remote", fake_run_remote):
            list_jobs(config=_config())
            list_jobs(config=_config())

        manager._jobctl_verified.clear()
        probes = [c for c in remote_calls if "--version" in c]
        assert len(probes) == 1


# ---------------------------------------------------------------------------
# jobctl portability
# ---------------------------------------------------------------------------


def test_jobctl_is_stdlib_only():
    import ast
    import sys

    from cluster_kit.jobs import jobctl

    tree = ast.parse(Path(jobctl.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    disallowed = imported - sys.stdlib_module_names - {"__future__"}
    assert not disallowed, f"non-stdlib imports in jobctl.py: {disallowed}"
