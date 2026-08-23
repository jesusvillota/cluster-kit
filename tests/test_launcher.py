"""Tests for SLURM launcher submission helpers."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cluster_kit.launch.launcher import (
    add_launcher_args,
    maybe_launch,
    submit_command,
)


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
        assert "CLUSTER_DEPLOY_LOCK_PATH=/remote/proj/.cluster_kit/deploy.lock" in cmd
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


# ---------------------------------------------------------------------------
# maybe_launch: PC (detached, non-SLURM) submission
# ---------------------------------------------------------------------------


class _FakePCConfig:
    host = "pc"
    remote_base = "/home/u/proj"
    executor = "ssh"


@contextmanager
def _capture_pc(tmp_path: Path, executor: str = "ssh"):
    """Stub the PC submission chain, collecting (command, name) per job."""
    cfg = _FakePCConfig()
    cfg.executor = executor
    submitted: list[tuple[str, str]] = []

    handle = SimpleNamespace(job_id="j1", log_path="/home/u/proj/log")

    with (
        patch("cluster_kit.launch.launcher._load_pc_config", return_value=cfg),
        patch("cluster_kit.launch.launcher._find_project_root", return_value=tmp_path),
        patch(
            "cluster_kit.launch.launcher._confirm_and_prepare_pc_submission",
            return_value=False,
        ),
        patch(
            "cluster_kit.launch.launcher._strip_launcher_flags_from_argv",
            return_value=["--years", "2020"],
        ),
        patch("cluster_kit.utils.ssh.ensure_reachable"),
        patch(
            "cluster_kit.jobs.submit",
            side_effect=lambda cmd, name, config: submitted.append((cmd, name))
            or handle,
        ),
    ):
        yield submitted


def test_pc_submission_is_handled(tmp_path: Path) -> None:
    with _capture_pc(tmp_path) as submitted:
        handled = maybe_launch(
            str(tmp_path / "src" / "process.py"), _fanout_args(run_from="pc")
        )

    assert handled is True
    assert len(submitted) == 1
    command, name = submitted[0]
    assert command.startswith("uv run python src/process.py")
    assert name == "process"


def test_pc_submission_fans_out(tmp_path: Path) -> None:
    """Fan-out has the same shape on the PC as on the cluster."""
    with _capture_pc(tmp_path) as submitted:
        maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(run_from="pc"),
            fan_out=["aa", "bb"],
            fan_out_flag="--defs",
        )

    assert [name for _, name in submitted] == ["process_aa", "process_bb"]
    assert "--defs aa" in submitted[0][0]
    assert "--defs bb" in submitted[1][0]


def test_pc_submission_ignores_slurm_flags(tmp_path: Path) -> None:
    with _capture_pc(tmp_path) as submitted:
        maybe_launch(
            str(tmp_path / "src" / "process.py"), _fanout_args(run_from="pc")
        )

    command = submitted[0][0]
    for flag in ("--partition", "--slurm-cpus", "--slurm-mem", "sbatch"):
        assert flag not in command


def test_pc_aborts_when_git_sync_declined(tmp_path: Path) -> None:
    cfg = _FakePCConfig()
    with (
        patch("cluster_kit.launch.launcher._load_pc_config", return_value=cfg),
        patch("cluster_kit.launch.launcher._find_project_root", return_value=tmp_path),
        patch(
            "cluster_kit.launch.launcher._confirm_and_prepare_pc_submission",
            return_value=True,
        ),
        patch("cluster_kit.utils.ssh.ensure_reachable"),
        patch("cluster_kit.jobs.submit") as submit_job,
    ):
        handled = maybe_launch(
            str(tmp_path / "src" / "process.py"), _fanout_args(run_from="pc")
        )

    assert handled is True
    submit_job.assert_not_called()


def test_pc_profile_must_use_ssh_executor(tmp_path: Path) -> None:
    """A non-ssh PC profile is a config error, not a silent SLURM fallback."""
    from cluster_kit.launch.launcher import _load_pc_config

    cfg = _FakePCConfig()
    cfg.executor = "slurm"
    with (
        patch("cluster_kit.config.load_config", return_value=cfg),
        patch("cluster_kit.config.validate_config_strict"),
        pytest.raises(SystemExit),
    ):
        _load_pc_config(tmp_path)


def test_run_from_pc_is_an_accepted_choice() -> None:
    parser = argparse.ArgumentParser()
    add_launcher_args(parser)
    assert parser.parse_args(["--run-from", "pc"]).run_from == "pc"


def test_fan_out_flag_is_not_duplicated(tmp_path: Path) -> None:
    """The caller's own --defs a,b,c must not survive alongside the per-job value."""
    with (
        patch(
            "cluster_kit.launch.launcher._strip_launcher_flags_from_argv",
            return_value=["--defs", "aa,bb", "--years", "2020"],
        ),
        _capture_submissions(tmp_path) as submitted,
    ):
        maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(),
            fan_out=["aa", "bb"],
            fan_out_flag="--defs",
        )

    for cmd in submitted:
        assert cmd.count("--defs") == 1, cmd
    assert "--defs aa" in submitted[0]
    assert "--defs bb" in submitted[1]
    # Unrelated script args survive.
    assert "--years 2020" in submitted[0]


def test_fan_out_flag_equals_form_also_stripped(tmp_path: Path) -> None:
    with (
        patch(
            "cluster_kit.launch.launcher._strip_launcher_flags_from_argv",
            return_value=["--defs=aa,bb", "--years", "2020"],
        ),
        _capture_submissions(tmp_path) as submitted,
    ):
        maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(),
            fan_out=["aa"],
            fan_out_flag="--defs",
        )

    assert "--defs=aa,bb" not in submitted[0]
    assert "--defs aa" in submitted[0]


# ---------------------------------------------------------------------------
# maybe_launch: local fan-out
# ---------------------------------------------------------------------------


def _popen_stub(returncodes):
    """Popen replacement returning canned exit codes in order."""
    calls = []
    codes = list(returncodes)

    class _P:
        def __init__(self, cmd):
            calls.append(cmd)
            self.returncode = codes.pop(0)

        def wait(self):
            return self.returncode

    return _P, calls


def test_local_fan_out_spawns_one_subprocess_per_value(tmp_path: Path) -> None:
    P, calls = _popen_stub([0, 0])
    with (
        patch("cluster_kit.launch.launcher.subprocess.Popen", P),
        patch(
            "cluster_kit.launch.launcher._strip_launcher_flags_from_argv",
            return_value=["--years", "2020"],
        ),
    ):
        handled = maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(run_from="local"),
            fan_out=["aa", "bb"],
            fan_out_flag="--defs",
        )

    assert handled is True
    assert len(calls) == 2
    for value, cmd in zip(["aa", "bb"], calls):
        assert "--defs" in cmd and value in cmd
        # Children must not fan out again.
        assert cmd[cmd.index("--mode") + 1] == "sequential"
        assert cmd[cmd.index("--run-from") + 1] == "local"


def test_local_fan_out_exits_nonzero_when_a_child_fails(tmp_path: Path) -> None:
    P, _ = _popen_stub([0, 1])
    with (
        patch("cluster_kit.launch.launcher.subprocess.Popen", P),
        patch(
            "cluster_kit.launch.launcher._strip_launcher_flags_from_argv",
            return_value=[],
        ),
        pytest.raises(SystemExit) as exc,
    ):
        maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(run_from="local"),
            fan_out=["aa", "bb"],
            fan_out_flag="--defs",
        )

    assert exc.value.code == 1


def test_local_sequential_mode_runs_in_process(tmp_path: Path) -> None:
    """Sequential is the default: the script handles its own definitions."""
    with patch("cluster_kit.launch.launcher.subprocess.Popen") as popen:
        handled = maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(run_from="local", mode="sequential"),
            fan_out=["aa", "bb"],
            fan_out_flag="--defs",
        )

    assert handled is False
    popen.assert_not_called()


def test_local_without_mode_flag_fans_out(tmp_path: Path) -> None:
    """No --mode means the caller's explicit fan_out is honoured, not ignored."""
    P, calls = _popen_stub([0, 0])
    with (
        patch("cluster_kit.launch.launcher.subprocess.Popen", P),
        patch(
            "cluster_kit.launch.launcher._strip_launcher_flags_from_argv",
            return_value=[],
        ),
    ):
        handled = maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(run_from="local"),
            fan_out=["aa", "bb"],
            fan_out_flag="--defs",
        )

    assert handled is True
    assert len(calls) == 2


def test_sequential_mode_suppresses_fan_out_on_cluster(tmp_path: Path) -> None:
    """--mode sequential means one job; the script loops over argv itself."""
    with _capture_submissions(tmp_path) as submitted:
        maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(mode="sequential"),
            fan_out=["aa", "bb", "cc"],
            fan_out_flag="--defs",
        )

    assert len(submitted) == 1
    assert "--job-name=process" in submitted[0]


def test_array_mode_fans_out_on_cluster(tmp_path: Path) -> None:
    with _capture_submissions(tmp_path) as submitted:
        maybe_launch(
            str(tmp_path / "src" / "process.py"),
            _fanout_args(mode="array"),
            fan_out=["aa", "bb", "cc"],
            fan_out_flag="--defs",
        )

    assert len(submitted) == 3


# ---------------------------------------------------------------------------
# Pre-submit sync guard
#
# Ported from whales, whose wrapper owned this flow before it moved upstream.
# ---------------------------------------------------------------------------


@contextmanager
def _guard(interactive=True, answer="yes", sync_ok=True, sync_exc=None):
    from cluster_kit.launch import launcher as L

    prompt = patch("rich.prompt.Prompt.ask", return_value=answer)
    if isinstance(answer, BaseException) or (
        isinstance(answer, type) and issubclass(answer, BaseException)
    ):
        prompt = patch("rich.prompt.Prompt.ask", side_effect=answer)

    sync = (
        patch.object(L, "_run_cluster_sync", side_effect=sync_exc)
        if sync_exc
        else patch.object(L, "_run_cluster_sync", return_value=sync_ok)
    )
    with (
        patch.object(L, "_is_interactive_terminal", return_value=interactive),
        prompt,
        sync as sync_mock,
    ):
        yield sync_mock


def _abort(**kw) -> tuple[bool, object]:
    from cluster_kit.launch.launcher import _confirm_and_prepare_cluster_submission

    with _guard(**kw) as sync_mock:
        return _confirm_and_prepare_cluster_submission(Path("/proj")), sync_mock


def test_sync_guard_syncs_then_submits_when_accepted() -> None:
    aborted, sync_mock = _abort(answer="yes")
    assert aborted is False
    sync_mock.assert_called_once()


def test_sync_guard_declines_and_still_submits() -> None:
    aborted, sync_mock = _abort(answer="no")
    assert aborted is False
    sync_mock.assert_not_called()


def test_sync_guard_aborts_when_sync_fails() -> None:
    """A failed sync must stop submission, not run stale code on the cluster."""
    aborted, _ = _abort(answer="yes", sync_ok=False)
    assert aborted is True


def test_sync_guard_interrupt_aborts_without_falling_through() -> None:
    aborted, sync_mock = _abort(answer=KeyboardInterrupt)
    assert aborted is True
    sync_mock.assert_not_called()


def test_sync_guard_eof_aborts_without_falling_through() -> None:
    aborted, _ = _abort(answer=EOFError)
    assert aborted is True


def test_sync_guard_non_interactive_skips_prompt_and_proceeds() -> None:
    aborted, sync_mock = _abort(interactive=False)
    assert aborted is False
    sync_mock.assert_not_called()


def test_sync_runs_before_remote_log_dir_creation(tmp_path: Path) -> None:
    """Ordering matters: the log dir must not be made against an unsynced tree."""
    order: list[str] = []
    from cluster_kit.launch import launcher as L

    with (
        patch.object(L, "get_remote_base", return_value="/remote/proj"),
        patch.object(L, "get_texlive_root", return_value=""),
        patch.object(L, "_find_project_root", return_value=tmp_path),
        patch.object(
            L,
            "_confirm_and_prepare_cluster_submission",
            side_effect=lambda root: order.append("sync") or False,
        ),
        patch.object(L, "_ssh_run", side_effect=lambda cmd: order.append("mkdir")),
        patch.object(L, "_ssh_submit", side_effect=lambda cmd: order.append("submit")),
        patch.object(L, "_strip_launcher_flags_from_argv", return_value=[]),
    ):
        maybe_launch(str(tmp_path / "src" / "p.py"), _fanout_args())

    assert order == ["sync", "mkdir", "submit"]
