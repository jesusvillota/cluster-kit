"""Tests for SLURM launcher submission helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from cluster_kit.launch.launcher import submit_command


def test_submit_command_uses_project_worker_script(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    worker = project_root / "runnables" / "slurm" / "worker.slurm"
    worker.parent.mkdir(parents=True)
    worker.write_text("#!/bin/bash\n")

    captured: list[str] = []

    def fake_ssh_run(command: str) -> None:
        return None

    def fake_ssh_submit(command: str) -> str:
        captured.append(command)
        return "12345"

    with (
        patch(
            "cluster_kit.launch.launcher.get_remote_base",
            return_value="/remote/project",
        ),
        patch("cluster_kit.launch.launcher._ssh_run", side_effect=fake_ssh_run),
        patch(
            "cluster_kit.launch.launcher._ssh_submit",
            side_effect=fake_ssh_submit,
        ),
    ):
        job_id = submit_command(
            "uv run src/demo.py --name 'alpha beta'",
            project_root=project_root,
            sync=False,
        )

    assert job_id == "12345"
    assert captured
    assert "--wrap" not in captured[0]
    assert "/remote/project/runnables/slurm/worker.slurm" in captured[0]
    assert "PROJECT_DIR=/remote/project" in captured[0]
    assert "uv" in captured[0]
    assert "run" in captured[0]
    assert "src/demo.py" in captured[0]


def test_submit_command_returns_none_when_worker_missing(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()

    with (
        patch(
            "cluster_kit.launch.launcher.get_remote_base",
            return_value="/remote/project",
        ),
        patch("cluster_kit.launch.launcher._ssh_run"),
        patch("cluster_kit.launch.launcher._ssh_submit") as submit,
    ):
        job_id = submit_command(
            "uv run src/demo.py",
            project_root=project_root,
            sync=False,
        )

    assert job_id is None
    submit.assert_not_called()
