"""Tests for the login-node workflow orchestrator (scheduler logic)."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cluster_kit.workflow import orchestrator as orch


def make_plan(deps_by_job: list[list[int]], **overrides) -> dict:
    plan = {
        "schema_version": 1,
        "workflow_name": "test",
        "run_id": "test_20260611-120000",
        "created_at": "2026-06-11T12:00:00+02:00",
        "user": "testuser",
        "remote_base": "/remote/base",
        "dependency_mode": "afterok",
        "max_concurrent": 4,
        "poll_interval": 1,
        "jobs": [
            {
                "index": index,
                "name": f"job{index}",
                "stage": "stage",
                "deps": deps,
                "log_dir": "_logs_/workflows/test/stage",
                "sbatch_argv": ["sbatch", f"--job-name=job{index}"],
            }
            for index, deps in enumerate(deps_by_job)
        ],
    }
    plan.update(overrides)
    return plan


class FakeSlurm:
    """Stands in for orchestrator._run: simulates squeue/sacct/sbatch."""

    def __init__(self) -> None:
        self.queue: set[str] = set()  # job ids visible in squeue
        self.sacct: dict[str, tuple[str, str]] = {}  # id -> (state, exit)
        self.submitted_argvs: list[list[str]] = []
        self.next_id = 100
        self.sbatch_stderr: str | None = None  # force sbatch failure

    def __call__(self, argv, cwd=None):
        if argv[0] == "squeue":
            return SimpleNamespace(
                returncode=0, stdout="\n".join(sorted(self.queue)), stderr=""
            )
        if argv[0] == "sacct":
            requested = argv[2].split(",")
            lines = [
                f"{job_id}|{state}|{exit_code}"
                for job_id, (state, exit_code) in self.sacct.items()
                if job_id in requested
            ]
            return SimpleNamespace(returncode=0, stdout="\n".join(lines), stderr="")
        if argv[0] == "sbatch":
            self.submitted_argvs.append(list(argv))
            if self.sbatch_stderr is not None:
                return SimpleNamespace(
                    returncode=1, stdout="", stderr=self.sbatch_stderr
                )
            job_id = str(self.next_id)
            self.next_id += 1
            self.queue.add(job_id)
            return SimpleNamespace(
                returncode=0, stdout=f"Submitted batch job {job_id}", stderr=""
            )
        raise AssertionError(f"unexpected command: {argv}")

    def finish(self, job_id: str, state: str = "COMPLETED", exit_code: str = "0:0"):
        self.queue.discard(job_id)
        self.sacct[job_id] = (state, exit_code)


@pytest.fixture
def slurm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeSlurm:
    fake = FakeSlurm()
    monkeypatch.setattr(orch, "_run", fake)
    return fake


def _state_for(plan: dict, tmp_path: Path) -> tuple[dict, str]:
    state_path = str(tmp_path / "state.json")
    return orch.init_or_resume_state(plan, state_path), state_path


def _job(state: dict, index: int) -> dict:
    return state["jobs"][index]


# ---------------------------------------------------------------------------
# Readiness and dependency gating
# ---------------------------------------------------------------------------


def test_dependent_job_waits_for_completion(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[], [0]])
    state, _ = _state_for(plan, tmp_path)

    orch.run_cycle(plan, state)
    assert _job(state, 0)["state"] == orch.SUBMITTED
    assert _job(state, 1)["state"] == orch.WAITING
    assert len(slurm.submitted_argvs) == 1

    # Dependency still running: nothing new is submitted.
    orch.run_cycle(plan, state)
    assert len(slurm.submitted_argvs) == 1

    slurm.finish(_job(state, 0)["slurm_job_id"])
    orch.run_cycle(plan, state)
    assert _job(state, 0)["state"] == orch.COMPLETED
    assert _job(state, 1)["state"] == orch.SUBMITTED


def test_afterok_failure_propagates_transitively(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    # job1 depends on job0, job2 on job1; job3 is independent.
    plan = make_plan([[], [0], [1], []])
    state, state_path = _state_for(plan, tmp_path)

    orch.run_cycle(plan, state)  # submits job0 and job3
    assert len(slurm.submitted_argvs) == 2

    slurm.finish(_job(state, 0)["slurm_job_id"], state="FAILED", exit_code="1:0")
    orch.run_cycle(plan, state)
    assert _job(state, 0)["state"] == orch.FAILED
    assert _job(state, 1)["state"] == orch.SKIPPED
    assert _job(state, 2)["state"] == orch.SKIPPED
    assert _job(state, 3)["state"] == orch.SUBMITTED  # independent branch lives on

    slurm.finish(_job(state, 3)["slurm_job_id"])
    orch.run_cycle(plan, state)
    exit_code = orch.finalize_if_done(state, state_path)
    assert exit_code == 1
    assert state["status"] == "failed"


def test_afterany_unblocks_on_failure(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[], [0]], dependency_mode="afterany")
    state, _ = _state_for(plan, tmp_path)

    orch.run_cycle(plan, state)
    slurm.finish(_job(state, 0)["slurm_job_id"], state="FAILED", exit_code="1:0")
    orch.run_cycle(plan, state)

    assert _job(state, 0)["state"] == orch.FAILED
    assert _job(state, 1)["state"] == orch.SUBMITTED


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------


def test_throttle_counts_all_user_jobs(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[], [], [], [], [], []])  # 6 independent jobs
    slurm.queue.update({"888", "999"})  # manually submitted jobs share the budget

    state, _ = _state_for(plan, tmp_path)
    orch.run_cycle(plan, state)
    assert len(slurm.submitted_argvs) == 2  # 4 - 2 foreign

    orch.run_cycle(plan, state)  # queue now full (2 foreign + 2 ours)
    assert len(slurm.submitted_argvs) == 2

    slurm.queue.discard("888")
    slurm.queue.discard("999")
    orch.run_cycle(plan, state)
    assert len(slurm.submitted_argvs) == 4  # two more slots freed


def test_no_submission_when_queue_at_limit(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[]])
    slurm.queue.update({"1", "2", "3", "4"})

    state, _ = _state_for(plan, tmp_path)
    orch.run_cycle(plan, state)

    assert slurm.submitted_argvs == []
    assert _job(state, 0)["state"] == orch.WAITING


# ---------------------------------------------------------------------------
# Submission errors
# ---------------------------------------------------------------------------


def test_assoc_limit_error_is_retried_without_penalty(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    plan = make_plan([[]])
    state, _ = _state_for(plan, tmp_path)

    slurm.sbatch_stderr = (
        "sbatch: error: AssocMaxSubmitJobLimit\n"
        "sbatch: error: Batch job submission failed"
    )
    orch.run_cycle(plan, state)
    assert _job(state, 0)["state"] == orch.WAITING
    assert _job(state, 0)["submit_retries"] == 0

    slurm.sbatch_stderr = None
    orch.run_cycle(plan, state)
    assert _job(state, 0)["state"] == orch.SUBMITTED


def test_persistent_sbatch_error_marks_submit_failed(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    plan = make_plan([[], [0]])
    state, _ = _state_for(plan, tmp_path)

    slurm.sbatch_stderr = "sbatch: error: Invalid partition"
    for _ in range(orch.MAX_SUBMIT_RETRIES):
        orch.run_cycle(plan, state)

    assert _job(state, 0)["state"] == orch.SUBMIT_FAILED
    orch.run_cycle(plan, state)
    assert _job(state, 1)["state"] == orch.SKIPPED


# ---------------------------------------------------------------------------
# sacct parsing and lost jobs
# ---------------------------------------------------------------------------


def test_cancelled_by_user_counts_as_failure(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[]])
    state, _ = _state_for(plan, tmp_path)

    orch.run_cycle(plan, state)
    job_id = _job(state, 0)["slurm_job_id"]
    slurm.queue.discard(job_id)
    slurm.sacct[job_id] = ("CANCELLED by 12345", "0:0")

    orch.run_cycle(plan, state)
    assert _job(state, 0)["state"] == orch.FAILED
    assert _job(state, 0)["slurm_state"] == "CANCELLED"


def test_sacct_lag_tolerated_while_job_in_squeue(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    plan = make_plan([[]])
    state, _ = _state_for(plan, tmp_path)

    orch.run_cycle(plan, state)  # job in queue, not yet in sacct
    for _ in range(orch.MAX_MISSING_POLLS + 1):
        orch.run_cycle(plan, state)

    assert _job(state, 0)["state"] == orch.SUBMITTED
    assert _job(state, 0)["missing_polls"] == 0


def test_job_lost_from_both_sources_marked_failed(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    plan = make_plan([[]])
    state, _ = _state_for(plan, tmp_path)

    orch.run_cycle(plan, state)
    slurm.queue.discard(_job(state, 0)["slurm_job_id"])  # vanishes without sacct trace

    for _ in range(orch.MAX_MISSING_POLLS):
        orch.run_cycle(plan, state)

    assert _job(state, 0)["state"] == orch.FAILED
    assert _job(state, 0)["slurm_state"] == "LOST"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def test_state_roundtrip_and_resume(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[], [0]])
    state, state_path = _state_for(plan, tmp_path)

    orch.run_cycle(plan, state)
    orch.write_state(state, state_path)

    resumed, _ = _state_for(plan, tmp_path)
    assert _job(resumed, 0)["state"] == orch.SUBMITTED
    assert _job(resumed, 0)["slurm_job_id"] == _job(state, 0)["slurm_job_id"]
    assert _job(resumed, 1)["state"] == orch.WAITING

    # Resumed orchestrator picks up where the crashed one left off.
    slurm.finish(_job(resumed, 0)["slurm_job_id"])
    orch.run_cycle(plan, resumed)
    assert _job(resumed, 1)["state"] == orch.SUBMITTED


def test_finalize_success(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[]])
    state, state_path = _state_for(plan, tmp_path)

    orch.run_cycle(plan, state)
    assert orch.finalize_if_done(state, state_path) is None

    slurm.finish(_job(state, 0)["slurm_job_id"])
    orch.run_cycle(plan, state)
    assert orch.finalize_if_done(state, state_path) == 0
    assert state["status"] == "finished"

    saved = json.loads(Path(state_path).read_text())
    assert saved["status"] == "finished"
    assert saved["finished_at"] is not None


# ---------------------------------------------------------------------------
# Portability guard
# ---------------------------------------------------------------------------


def test_orchestrator_is_stdlib_only() -> None:
    """The shipped daemon must run on a bare login-node python3."""
    source_path = Path(orch.__file__)
    tree = ast.parse(source_path.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    disallowed = imported - sys.stdlib_module_names - {"__future__"}
    assert not disallowed, f"non-stdlib imports in orchestrator.py: {disallowed}"
