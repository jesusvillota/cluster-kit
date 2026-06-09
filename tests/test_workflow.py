"""Tests for YAML/TOML workflow parsing and dependency submission."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cluster_kit.workflow import WorkflowError, parse_workflow_file, submit_workflow


def _write_workflow(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text(content)
    return path


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
    assert plan.worker_script == "runnables/slurm/worker.slurm"
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
    assert plan.worker_script == "runnables/slurm/worker.slurm"
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


def test_submit_chain_uses_previous_job_dependency(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: chain-demo
sync: false

jobs:
  - command: |
      uv run src/one.py --run-from cluster --partition cpu_shared

  - command: |
      uv run src/two.py --run-from cluster --partition cpu_large
''',
    )
    submitted: list[dict[str, object]] = []

    def fake_submit(command: str, **kwargs: object) -> str:
        submitted.append({"command": command, **kwargs})
        return str(100 + len(submitted))

    with patch("cluster_kit.workflow.runner.submit_command", side_effect=fake_submit):
        job_ids = submit_workflow(workflow)

    assert job_ids == ["101", "102"]
    assert submitted[0]["dependency"] is None
    assert submitted[1]["dependency"] == "afterok:101"
    assert submitted[0]["worker_script"] == "runnables/slurm/worker.slurm"
    assert submitted[1]["worker_script"] == "runnables/slurm/worker.slurm"
    assert submitted[0]["command"] == "uv run src/one.py"
    assert submitted[1]["partition"] == "cpu_large"


def test_submit_stages_parallel_jobs_use_previous_stage_dependencies(
    tmp_path: Path,
) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: staged-demo
dependency: afterok
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
    submitted: list[dict[str, object]] = []

    def fake_submit(command: str, **kwargs: object) -> str:
        submitted.append({"command": command, **kwargs})
        return str(200 + len(submitted))

    with patch("cluster_kit.workflow.runner.submit_command", side_effect=fake_submit):
        job_ids = submit_workflow(workflow)

    assert job_ids == ["201", "202", "203"]
    assert submitted[0]["dependency"] is None
    assert submitted[1]["dependency"] is None
    assert submitted[2]["dependency"] == "afterok:201:202"
    assert submitted[0]["worker_script"] == "runnables/slurm/worker.slurm"


def test_submit_workflow_uses_custom_worker_script(tmp_path: Path) -> None:
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
    submitted: list[dict[str, object]] = []

    def fake_submit(command: str, **kwargs: object) -> str:
        submitted.append({"command": command, **kwargs})
        return "401"

    with patch("cluster_kit.workflow.runner.submit_command", side_effect=fake_submit):
        submit_workflow(workflow)

    assert submitted[0]["worker_script"] == "runnables/slurm/custom_worker.slurm"


def test_submit_stages_sequential_stage_chains_within_stage(tmp_path: Path) -> None:
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
    submitted: list[dict[str, object]] = []

    def fake_submit(command: str, **kwargs: object) -> str:
        submitted.append({"command": command, **kwargs})
        return str(300 + len(submitted))

    with patch("cluster_kit.workflow.runner.submit_command", side_effect=fake_submit):
        submit_workflow(workflow)

    assert submitted[0]["dependency"] is None
    assert submitted[1]["dependency"] == "afterok:301"
    assert submitted[0]["worker_script"] == "runnables/slurm/worker.slurm"


def test_submit_sequential_stage_keeps_previous_stage_dependency(
    tmp_path: Path,
) -> None:
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
    submitted: list[dict[str, object]] = []

    def fake_submit(command: str, **kwargs: object) -> str:
        submitted.append({"command": command, **kwargs})
        return str(500 + len(submitted))

    with patch("cluster_kit.workflow.runner.submit_command", side_effect=fake_submit):
        job_ids = submit_workflow(workflow)

    assert job_ids == ["501", "502", "503", "504"]
    assert submitted[0]["dependency"] is None
    assert submitted[1]["dependency"] is None
    assert submitted[2]["dependency"] == "afterok:501:502"
    assert submitted[3]["dependency"] == "afterok:503"


def test_dry_run_returns_synthetic_ids_without_submission(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
sync: true

jobs:
  - command: |
      uv run src/a.py --run-from cluster
''',
    )

    with patch("cluster_kit.workflow.runner.submit_command") as submit:
        job_ids = submit_workflow(workflow, dry_run=True)

    assert job_ids == ["dry-1-1"]
    submit.assert_not_called()


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
