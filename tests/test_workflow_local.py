"""Tests for foreground local workflow execution."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cluster_kit.workflow import (
    WorkflowError,
    parse_local_workflow_file,
    run_local_workflow,
)


def _write_workflow(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "workflow.yaml"
    path.write_text(content)
    return path


def test_parse_local_workflow_accepts_shell_command(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: local-demo

stages:
  - name: stock-quotes-ingestion
    jobs:
      - name: stock-quotes-full-parquet
        command: |
          export PATH="$HOME/.local/bin:$PATH" && \\
          uv run src/stocks/data_processing/build_stock_quotes_parquet.py \\
            --years 2014 to 2026 \\
            --mail-me log
''',
    )

    plan = parse_local_workflow_file(workflow, project_root=tmp_path)

    assert plan.name == "local-demo"
    assert plan.project_root == tmp_path.resolve()
    assert plan.stages[0].name == "stock-quotes-ingestion"
    assert plan.stages[0].job.name == "stock-quotes-full-parquet"
    assert plan.stages[0].job.command.startswith("export PATH=")
    assert "&&" in plan.stages[0].job.command
    assert "uv run src/stocks/data_processing/build_stock_quotes_parquet.py" in (
        plan.stages[0].job.command
    )


def test_parse_local_workflow_accepts_top_level_jobs(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
name: chain-demo

jobs:
  - name: first
    command: echo first
  - name: second
    command: echo second
''',
    )

    plan = parse_local_workflow_file(workflow, project_root=tmp_path)

    assert [stage.name for stage in plan.stages] == ["job-1", "job-2"]
    assert [stage.job.command for stage in plan.stages] == ["echo first", "echo second"]


def test_parse_local_workflow_rejects_multiple_jobs_per_stage(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
stages:
  - name: build
    jobs:
      - command: echo one
      - command: echo two
''',
    )

    with pytest.raises(WorkflowError, match="exactly 1 job"):
        parse_local_workflow_file(workflow, project_root=tmp_path)


def test_parse_local_workflow_rejects_missing_command(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
jobs:
  - name: missing
''',
    )

    with pytest.raises(WorkflowError, match="missing command"):
        parse_local_workflow_file(workflow, project_root=tmp_path)


def test_run_local_workflow_executes_in_order_and_cwd(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
jobs:
  - name: first
    command: echo first
  - name: second
    command: echo second
''',
    )
    calls: list[str] = []

    def fake_run(
        command: str, *, shell: bool, cwd: Path
    ) -> subprocess.CompletedProcess:
        calls.append(command)
        assert shell is True
        assert cwd == tmp_path.resolve()
        return subprocess.CompletedProcess(command, 0)

    with patch("cluster_kit.workflow.local.subprocess.run", side_effect=fake_run):
        exit_code = run_local_workflow(workflow, project_root=tmp_path)

    assert exit_code == 0
    assert calls == ["echo first", "echo second"]


def test_run_local_workflow_stops_on_failure(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
jobs:
  - name: first
    command: echo first
  - name: second
    command: echo second
''',
    )
    run = MagicMock(
        side_effect=[
            subprocess.CompletedProcess("echo first", 2),
            subprocess.CompletedProcess("echo second", 0),
        ]
    )

    with patch("cluster_kit.workflow.local.subprocess.run", run):
        exit_code = run_local_workflow(workflow, project_root=tmp_path)

    assert exit_code == 2
    run.assert_called_once()


def test_run_local_workflow_dry_run_does_not_execute(tmp_path: Path) -> None:
    workflow = _write_workflow(
        tmp_path,
        '''
jobs:
  - name: first
    command: echo first
''',
    )

    with patch("cluster_kit.workflow.local.subprocess.run") as run:
        exit_code = run_local_workflow(workflow, dry_run=True, project_root=tmp_path)

    assert exit_code == 0
    run.assert_not_called()
