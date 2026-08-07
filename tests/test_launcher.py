"""Tests for SLURM launcher submission helpers."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from cluster_kit.launch.launcher import maybe_launch, submit_command


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
    assert "python" in captured[0]
    assert "src/demo.py" in captured[0]
    assert "alpha beta" in captured[0]


def test_submit_command_falls_back_to_packaged_worker(tmp_path: Path) -> None:
    """No repo-local worker means the centralized one, not a failed submit."""
    project_root = tmp_path / "project"
    project_root.mkdir()

    with (
        patch(
            "cluster_kit.launch.launcher.get_remote_base",
            return_value="/remote/project",
        ),
        patch("cluster_kit.launch.launcher._ssh_run"),
        patch(
            "cluster_kit.launch.launcher._ssh_submit", return_value="12345"
        ) as submit,
    ):
        job_id = submit_command(
            "uv run src/demo.py",
            project_root=project_root,
            sync=False,
        )

    assert job_id == "12345"
    assert "/remote/project/.cluster_kit/worker.slurm" in submit.call_args[0][0]


def test_submit_command_returns_none_when_explicit_worker_missing(
    tmp_path: Path,
) -> None:
    """An explicit worker_script that does not exist is still an error."""
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
            worker_script="runnables/slurm/nope.slurm",
            sync=False,
        )

    assert job_id is None
    submit.assert_not_called()


# ---------------------------------------------------------------------------
# maybe_launch: generic fan-out
# ---------------------------------------------------------------------------


def _fanout_args(**overrides):
    ns = argparse.Namespace(
        run_from="cluster",
        partition="cpu_express",
        qos=None,
        slurm_cpus=4,
        slurm_mem="8G",
        slurm_time="01:00:00",
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


@contextmanager
def _capture_submissions(tmp_path: Path):
    """Run maybe_launch against stubs, collecting the sbatch commands issued."""
    submitted: list[str] = []
    with (
        patch(
            "cluster_kit.launch.launcher.get_remote_base", return_value="/remote/proj"
        ),
        patch(
            "cluster_kit.launch.launcher.get_texlive_root", return_value=""
        ),
        patch("cluster_kit.launch.launcher._ssh_run"),
        patch(
            "cluster_kit.launch.launcher._ssh_submit",
            side_effect=lambda cmd: submitted.append(cmd) or "999",
        ),
        patch(
            "cluster_kit.launch.launcher._find_project_root", return_value=tmp_path
        ),
        patch(
            "cluster_kit.launch.launcher._confirm_and_prepare_cluster_submission",
            return_value=False,
        ),
        patch(
            "cluster_kit.launch.launcher._strip_launcher_flags_from_argv",
            return_value=["--years", "2020"],
        ),
    ):
        yield submitted


def test_fan_out_submits_one_job_per_value(tmp_path: Path) -> None:
    with _capture_submissions(tmp_path) as submitted:
        handled = maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(),
            fan_out=["alpha", "beta", "gamma"],
            fan_out_flag="--whale-definitions",
        )

    assert handled is True
    assert len(submitted) == 3
    for value, cmd in zip(["alpha", "beta", "gamma"], submitted):
        assert f"--whale-definitions {value}" in cmd
        # Each job is separately named so logs do not collide.
        assert f"--job-name=process_{value}" in cmd
        # Every job goes through the centralized worker.
        assert "/remote/proj/.cluster_kit/worker.slurm" in cmd
        assert "PROJECT_DIR=/remote/proj" in cmd
        assert "CLUSTER_REMOTE_BASE=/remote/proj" in cmd
    # No SLURM array involved.
    assert not any("--array" in cmd for cmd in submitted)


def test_no_fan_out_submits_exactly_one_job(tmp_path: Path) -> None:
    with _capture_submissions(tmp_path) as submitted:
        maybe_launch(str(tmp_path / "src" / "process.py"), _fanout_args())

    assert len(submitted) == 1
    assert "--job-name=process" in submitted[0]
    assert "--whale-definitions" not in submitted[0]


def test_single_fan_out_value_needs_no_special_casing(tmp_path: Path) -> None:
    with _capture_submissions(tmp_path) as submitted:
        maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(),
            fan_out=["only"],
            fan_out_flag="--defs",
        )

    assert len(submitted) == 1
    assert "--defs only" in submitted[0]


def test_fan_out_without_flag_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fan_out_flag"):
        maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(),
            fan_out=["alpha"],
        )


def test_fan_out_value_slugged_into_job_name(tmp_path: Path) -> None:
    """Values with shell-hostile characters must not corrupt --job-name."""
    with _capture_submissions(tmp_path) as submitted:
        maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(),
            fan_out=["size p99/raw"],
            fan_out_flag="--defs",
        )

    assert "--job-name=process_size-p99-raw" in submitted[0]


def test_submission_does_not_hardcode_a_conda_env(tmp_path: Path) -> None:
    """The old --wrap path activated conda_envs/cluster-kit for every project."""
    with _capture_submissions(tmp_path) as submitted:
        maybe_launch(str(tmp_path / "src" / "process.py"), _fanout_args())

    assert "conda_envs/cluster-kit" not in submitted[0]
    assert "--wrap" not in submitted[0]
