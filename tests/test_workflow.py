"""Tests for workflow parsing, execution-plan building, and orchestrated runs."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, patch

import pytest

from cluster_kit.config import ConfigError
from cluster_kit.workflow import (
    WorkflowError,
    build_execution_plan,
    parse_workflow_file,
    submit_workflow,
)
from cluster_kit.workflow.plan import (
    compute_dependency_graph,
    resolve_max_concurrent,
    resolve_poll_interval,
)

REMOTE_BASE = "/remote/base"


class _FakeDeployLock:
    calls: list[tuple[str, str]] = []

    def __init__(self, *, host: str, remote_base: str, purpose: str):
        self.host = host
        self.remote_base = remote_base
        self.purpose = purpose

    def __enter__(self):
        self.calls.append(("enter", self.remote_base))
        return self

    def __exit__(self, exc_type, exc, tb):
        self.calls.append(("exit", self.remote_base))


def _write_workflow(tmp_path: Path, content: str, *, worker: bool = True) -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text(content)
    if worker:
        worker_path = tmp_path / "runnables" / "slurm" / "worker.slurm"
        worker_path.parent.mkdir(parents=True, exist_ok=True)
        worker_path.write_text("#!/bin/bash\n")
    return path


def _patch_config(executor: str = "slurm"):
    return patch.multiple(
        "cluster_kit.workflow.plan",
        get_remote_base=MagicMock(return_value=PurePosixPath(REMOTE_BASE)),
        get_cluster_user=MagicMock(return_value="testuser"),
        get_executor=MagicMock(return_value=executor),
    )


def _build(workflow: Path, **kwargs):
    plan = parse_workflow_file(workflow)
    with _patch_config():
        return build_execution_plan(plan, **kwargs)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_yaml_chain_workflow_strips_launcher_flags(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: demo
sync: false

jobs:
  - name: First job
    command: |
      uv run src/process.py --value "quoted value" \
        --run-from cluster \
        --partition cpu_long
''',
    )

    plan = parse_workflow_file(workflow)

    assert plan.mode == "chain"
    assert len(plan.stages) == 1
    # Unset means the worker cluster-kit deploys, not a repo-local path.
    assert plan.worker_script is None
    job = plan.stages[0].jobs[0]
    assert job.name == "First_job"
    assert job.partition == "cpu_long"
    assert job.submit_command == "uv run src/process.py --value 'quoted value'"


def test_parse_yaml_stages_without_mode_defaults_to_stages(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: staged-demo
sync: false

stages:
  - name: build
    parallel: true
    jobs:
      - name: panel
        command: |
          uv run src/build.py --run-from cluster
''',
    )

    plan = parse_workflow_file(workflow)

    assert plan.mode == "stages"
    # Unset means the worker cluster-kit deploys, not a repo-local path.
    assert plan.worker_script is None
    assert plan.stages[0].name == "build"
    assert plan.stages[0].jobs[0].submit_command == "uv run src/build.py"


def test_parse_yaml_custom_worker_script(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: custom-worker
worker_script: runnables/slurm/custom_worker.slurm
sync: false

jobs:
  - command: |
      uv run src/custom.py --run-from cluster
''',
    )

    plan = parse_workflow_file(workflow)

    assert plan.worker_script == "runnables/slurm/custom_worker.slurm"


def test_rejects_non_uv_run_command(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
jobs:
  - command: |
      python script.py
''',
    )

    with pytest.raises(WorkflowError, match="uv run"):
        parse_workflow_file(workflow)


def test_parse_throttle_keys(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: throttled
max_concurrent: 3
poll_interval: 15
sync: false

jobs:
  - command: |
      uv run src/a.py --run-from cluster
''',
    )

    plan = parse_workflow_file(workflow)

    assert plan.max_concurrent == 3
    assert plan.poll_interval == 15


def test_parse_rejects_invalid_max_concurrent(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
max_concurrent: 0

jobs:
  - command: |
      uv run src/a.py --run-from cluster
''',
    )

    with pytest.raises(WorkflowError, match="max_concurrent"):
        parse_workflow_file(workflow)


def test_parse_toml_workflow_still_works(tmp_path: Path) -> None:
    path = tmp_path / "workflow.toml"
    path.write_text(
        '''
name = "compat-demo"
mode = "chain"

[[jobs]]
command = "uv run src/compat.py --run-from cluster"
'''
    )

    plan = parse_workflow_file(path)

    assert plan.mode == "chain"
    assert plan.stages[0].jobs[0].submit_command == "uv run src/compat.py"


# ---------------------------------------------------------------------------
# Dependency graph
# ---------------------------------------------------------------------------


def _deps(workflow: Path) -> list[list[int]]:
    plan = parse_workflow_file(workflow)
    return [deps for _, _, deps in compute_dependency_graph(plan)]


def test_chain_jobs_depend_on_previous_job(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: chain-demo
sync: false

jobs:
  - command: |
      uv run src/one.py --run-from cluster

  - command: |
      uv run src/two.py --run-from cluster

  - command: |
      uv run src/three.py --run-from cluster
''',
    )

    assert _deps(workflow) == [[], [0], [1]]


def test_parallel_stage_jobs_depend_on_full_previous_stage(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: staged-demo
sync: false

stages:
  - name: build
    parallel: true
    jobs:
      - command: |
          uv run src/build_a.py --run-from cluster

      - command: |
          uv run src/build_b.py --run-from cluster

  - name: plot
    parallel: true
    jobs:
      - command: |
          uv run src/plot.py --run-from cluster
''',
    )

    assert _deps(workflow) == [[], [], [0, 1]]


def test_sequential_stage_chains_within_stage(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: sequential-stage
sync: false

stages:
  - name: serial
    parallel: false
    jobs:
      - command: |
          uv run src/a.py --run-from cluster

      - command: |
          uv run src/b.py --run-from cluster
''',
    )

    assert _deps(workflow) == [[], [0]]


def test_sequential_stage_keeps_previous_stage_dependency(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: mixed-stages
sync: false

stages:
  - name: build
    parallel: true
    jobs:
      - command: |
          uv run src/build_a.py --run-from cluster

      - command: |
          uv run src/build_b.py --run-from cluster

  - name: report
    parallel: false
    jobs:
      - command: |
          uv run src/report_a.py --run-from cluster

      - command: |
          uv run src/report_b.py --run-from cluster
''',
    )

    assert _deps(workflow) == [[], [], [0, 1], [2]]


# ---------------------------------------------------------------------------
# Execution plan
# ---------------------------------------------------------------------------


def test_execution_plan_renders_full_sbatch_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLUSTER_EMAIL", raising=False)
    monkeypatch.delenv("CLUSTER_MAX_JOBS", raising=False)
    workflow = _write_workflow(
        tmp_path,
        '''
name: demo
sync: false

stages:
  - name: build
    jobs:
      - name: panel
        command: |
          uv run src/process.py --years 2015 --run-from cluster --partition cpu_long

  - name: viz
    jobs:
      - name: plot-result
        command: |
          uv run src/plot_result.py --run-from cluster --partition cpu_express
''',
    )

    exec_plan = _build(workflow)

    assert exec_plan["schema_version"] == 2
    assert exec_plan["executor"] == "slurm"
    assert exec_plan["workflow_name"] == "demo"
    assert exec_plan["run_id"].startswith("demo_")
    assert exec_plan["user"] == "testuser"
    assert exec_plan["remote_base"] == REMOTE_BASE
    assert exec_plan["dependency_mode"] == "afterok"
    assert exec_plan["max_concurrent"] == 4
    assert exec_plan["poll_interval"] == 30

    build_job, plot_job = exec_plan["jobs"]
    assert build_job["deps"] == []
    assert plot_job["deps"] == [0]
    assert build_job["log_dir"] == "_logs_/workflows/demo/build"

    argv = build_job["sbatch_argv"]
    assert argv[0] == "sbatch"
    assert "--partition=cpu_long" in argv
    assert "--qos=cpu_long" in argv
    assert "--cpus-per-task=32" in argv
    assert "--mem=160G" in argv
    assert "--time=168:00:00" in argv
    assert "--job-name=panel" in argv
    assert "--output=_logs_/workflows/demo/build/%x_%j.out" in argv
    assert (
        f"--export=ALL,PROJECT_DIR={REMOTE_BASE},"
        f"CLUSTER_REMOTE_BASE={REMOTE_BASE},"
        f"CLUSTER_DEPLOY_LOCK_PATH={REMOTE_BASE}/.cluster_kit/deploy.lock"
        in argv
    )
    assert not any(arg.startswith("--dependency") for arg in argv)
    worker_pos = argv.index(f"{REMOTE_BASE}/runnables/slurm/worker.slurm")
    assert argv[worker_pos + 1 :] == ["python", "src/process.py", "--years", "2015"]

    plot_argv = plot_job["sbatch_argv"]
    assert (
        f"--export=ALL,TEXLIVE=1,PROJECT_DIR={REMOTE_BASE},"
        f"CLUSTER_REMOTE_BASE={REMOTE_BASE},"
        f"CLUSTER_DEPLOY_LOCK_PATH={REMOTE_BASE}/.cluster_kit/deploy.lock"
        in plot_argv
    )


def test_execution_plan_ssh_executor_renders_commands(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: pc-demo
sync: false

stages:
  - name: build
    jobs:
      - name: panel
        command: |
          uv run src/process.py --years 2015 --run-from pc --partition cpu_long

  - name: viz
    jobs:
      - name: plot-result
        command: |
          uv run src/plot_result.py --run-from pc
''',
        worker=False,  # ssh executor needs no worker.slurm
    )

    plan = parse_workflow_file(workflow)
    with _patch_config(executor="ssh"):
        exec_plan = build_execution_plan(plan)

    assert exec_plan["executor"] == "ssh"
    build_job, plot_job = exec_plan["jobs"]
    assert "sbatch_argv" not in build_job
    assert build_job["command"] == "uv run src/process.py --years 2015"
    assert plot_job["command"] == "uv run src/plot_result.py"
    assert plot_job["deps"] == [0]


def test_execution_plan_uses_custom_worker_script(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: custom-worker-demo
worker_script: runnables/slurm/custom_worker.slurm
sync: false

jobs:
  - command: |
      uv run src/a.py --run-from cluster
''',
    )
    custom_worker = tmp_path / "runnables" / "slurm" / "custom_worker.slurm"
    custom_worker.write_text("#!/bin/bash\n")

    exec_plan = _build(workflow)

    argv = exec_plan["jobs"][0]["sbatch_argv"]
    assert f"{REMOTE_BASE}/runnables/slurm/custom_worker.slurm" in argv


def test_max_concurrent_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: throttled
max_concurrent: 3
sync: false

jobs:
  - command: |
      uv run src/a.py --run-from cluster
''',
    )
    plan = parse_workflow_file(workflow)

    monkeypatch.setenv("CLUSTER_MAX_JOBS", "2")
    assert resolve_max_concurrent(plan, 1) == 1  # CLI wins
    assert resolve_max_concurrent(plan, None) == 3  # then YAML
    plan_no_yaml = parse_workflow_file(
        _write_workflow(
            tmp_path,
            '''
jobs:
  - command: |
      uv run src/a.py --run-from cluster
''',
        )
    )
    assert resolve_max_concurrent(plan_no_yaml, None) == 2  # then env
    monkeypatch.delenv("CLUSTER_MAX_JOBS")
    assert resolve_max_concurrent(plan_no_yaml, None) == 4  # then default

    assert resolve_poll_interval(plan, 10) == 10
    assert resolve_poll_interval(plan_no_yaml, None) == 30


# ---------------------------------------------------------------------------
# submit_workflow
# ---------------------------------------------------------------------------


def test_submit_workflow_launches_orchestrator(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: launch-demo
sync: false

jobs:
  - command: |
      uv run src/a.py --run-from cluster
''',
    )
    launched: list[dict] = []

    def fake_launch(exec_plan: dict) -> str:
        launched.append(exec_plan)
        run_id = exec_plan["run_id"]
        return f"{exec_plan['remote_base']}/.cluster_kit/workflows/{run_id}"

    with (
        _patch_config(),
        patch("cluster_kit.config.get_cluster_host", return_value="user@cluster"),
        patch("cluster_kit.sync.lock.RemoteDeployLock", _FakeDeployLock),
        patch("cluster_kit.workflow.remote.launch_orchestrator", fake_launch),
    ):
        _FakeDeployLock.calls = []
        run_id = submit_workflow(workflow)

    assert len(launched) == 1
    assert run_id == launched[0]["run_id"]
    assert run_id.startswith("launch-demo_")
    assert len(launched[0]["jobs"]) == 1
    assert launched[0]["deploy_lock_path"] == f"{REMOTE_BASE}/.cluster_kit/deploy.lock"
    assert _FakeDeployLock.calls == [("enter", REMOTE_BASE), ("exit", REMOTE_BASE)]


def test_submit_workflow_no_sync_launches_inside_deploy_lock(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: lock-demo
sync: false

jobs:
  - command: |
      uv run src/a.py --run-from cluster
''',
    )
    calls: list[str] = []

    class Lock(_FakeDeployLock):
        def __enter__(self):
            calls.append(f"lock:{self.remote_base}")
            return self

        def __exit__(self, exc_type, exc, tb):
            calls.append("unlock")

    def fake_launch(exec_plan: dict) -> str:
        calls.append("launch")
        return "run-dir"

    with (
        _patch_config(),
        patch("cluster_kit.config.get_cluster_host", return_value="user@cluster"),
        patch("cluster_kit.sync.lock.RemoteDeployLock", Lock),
        patch("cluster_kit.workflow.remote.launch_orchestrator", fake_launch),
    ):
        submit_workflow(workflow)

    assert calls == [f"lock:{REMOTE_BASE}", "launch", "unlock"]


def test_submit_workflow_syncs_before_launch(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: sync-demo
sync: true

jobs:
  - command: |
      uv run src/a.py --run-from cluster
''',
    )
    calls: list[str] = []

    deployer = MagicMock()
    deployer.deploy.side_effect = lambda: calls.append("sync") or True

    def fake_launch(exec_plan: dict) -> str:
        calls.append("launch")
        return "run-dir"

    with (
        _patch_config(),
        patch("cluster_kit.config.get_sync_mode", return_value="rsync"),
        patch("cluster_kit.workflow.runner.CodeDeployer", return_value=deployer),
        patch("cluster_kit.workflow.remote.launch_orchestrator", fake_launch),
    ):
        submit_workflow(workflow)

    assert calls == ["sync", "launch"]


def test_submit_workflow_git_sync_mode_uses_git_syncer(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: git-sync-demo
sync: true

jobs:
  - command: |
      uv run src/a.py --run-from pc
''',
        worker=False,
    )
    calls: list[str] = []

    syncer = MagicMock()
    syncer.sync.side_effect = lambda: calls.append("git-sync") or True

    def fake_launch(exec_plan: dict) -> str:
        calls.append("launch")
        return "run-dir"

    with (
        _patch_config(executor="ssh"),
        patch("cluster_kit.config.get_sync_mode", return_value="git"),
        patch("cluster_kit.sync.git_sync.GitSyncer", return_value=syncer),
        patch("cluster_kit.workflow.remote.launch_orchestrator", fake_launch),
    ):
        submit_workflow(workflow)

    assert calls == ["git-sync", "launch"]


def test_dry_run_makes_no_remote_calls(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
sync: true

jobs:
  - command: |
      uv run src/a.py --run-from cluster
''',
    )

    with (
        _patch_config(),
        patch("cluster_kit.workflow.remote.launch_orchestrator") as launch,
        patch("cluster_kit.workflow.runner.CodeDeployer") as deployer,
        patch("cluster_kit.sync.lock.RemoteDeployLock") as lock,
    ):
        result = submit_workflow(workflow, dry_run=True)

    assert result == "dry-run"
    launch.assert_not_called()
    deployer.assert_not_called()
    lock.assert_not_called()


def test_dry_run_works_without_cluster_config(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
sync: false

jobs:
  - command: |
      uv run src/a.py --run-from cluster
''',
    )

    def raise_config_error() -> None:
        raise ConfigError("no config")

    with patch.multiple(
        "cluster_kit.workflow.plan",
        get_remote_base=MagicMock(side_effect=raise_config_error),
        get_cluster_user=MagicMock(side_effect=raise_config_error),
    ):
        result = submit_workflow(workflow, dry_run=True)

    assert result == "dry-run"


def test_workflow_builds_without_a_repo_local_worker(tmp_path: Path) -> None:
    """A repo that deleted its worker must still build a plan.

    The default used to be the literal legacy path, which made every workflow
    look like an explicit override; the centralized-worker fallback then never
    engaged and such repos could not run workflows at all.
    """
    workflow = _write_workflow(
        tmp_path,
        """
name: no-worker
sync: false

jobs:
  - command: |
      uv run src/a.py --run-from cluster
""",
        worker=False,
    )

    exec_plan = _build(workflow)

    argv = exec_plan["jobs"][0]["sbatch_argv"]
    assert f"{REMOTE_BASE}/.cluster_kit/worker.slurm" in argv


def test_explicit_worker_script_still_wins(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        """
name: custom
worker_script: runnables/slurm/custom.slurm
sync: false

jobs:
  - command: |
      uv run src/a.py --run-from cluster
""",
    )
    custom = tmp_path / "runnables" / "slurm" / "custom.slurm"
    custom.write_text("#!/bin/bash\n")

    argv = _build(workflow)["jobs"][0]["sbatch_argv"]
    assert f"{REMOTE_BASE}/runnables/slurm/custom.slurm" in argv
