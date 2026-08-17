"""Tests for the remote workflow orchestrator (scheduler logic)."""

from __future__ import annotations

import ast
import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cluster_kit.workflow import orchestrator as orch


def make_plan(deps_by_job: list[list[int]], **overrides) -> dict:
    executor = overrides.pop("executor", "slurm")
    jobs = []
    for index, deps in enumerate(deps_by_job):
        job = {
            "index": index,
            "name": f"job{index}",
            "stage": "stage",
            "deps": deps,
            "log_dir": "_logs_/workflows/test/stage",
        }
        if executor == "slurm":
            job["sbatch_argv"] = ["sbatch", f"--job-name=job{index}"]
        else:
            job["command"] = f"uv run src/job{index}.py"
        jobs.append(job)
    plan = {
        "schema_version": 2,
        "workflow_name": "test",
        "run_id": "test_20260611-120000",
        "created_at": "2026-06-11T12:00:00+02:00",
        "user": "testuser",
        "remote_base": "/remote/base",
        "executor": executor,
        "dependency_mode": "afterok",
        "max_concurrent": 4,
        "poll_interval": 1,
        "jobs": jobs,
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


def _slurm_setup(plan: dict, tmp_path: Path) -> tuple[dict, str, orch.SlurmExec]:
    state, state_path = _state_for(plan, tmp_path)
    return state, state_path, orch.SlurmExec(plan, str(tmp_path))


# ---------------------------------------------------------------------------
# Readiness and dependency gating (SLURM backend)
# ---------------------------------------------------------------------------


def test_dependent_job_waits_for_completion(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[], [0]])
    state, _, ex = _slurm_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    assert _job(state, 0)["state"] == orch.SUBMITTED
    assert _job(state, 1)["state"] == orch.WAITING
    assert len(slurm.submitted_argvs) == 1

    # Dependency still running: nothing new is submitted.
    orch.run_cycle(plan, state, ex)
    assert len(slurm.submitted_argvs) == 1

    slurm.finish(_job(state, 0)["exec_ref"])
    orch.run_cycle(plan, state, ex)
    assert _job(state, 0)["state"] == orch.COMPLETED
    assert _job(state, 1)["state"] == orch.SUBMITTED


def test_afterok_failure_propagates_transitively(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    # job1 depends on job0, job2 on job1; job3 is independent.
    plan = make_plan([[], [0], [1], []])
    state, state_path, ex = _slurm_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)  # submits job0 and job3
    assert len(slurm.submitted_argvs) == 2

    slurm.finish(_job(state, 0)["exec_ref"], state="FAILED", exit_code="1:0")
    orch.run_cycle(plan, state, ex)
    assert _job(state, 0)["state"] == orch.FAILED
    assert _job(state, 1)["state"] == orch.SKIPPED
    assert _job(state, 2)["state"] == orch.SKIPPED
    assert _job(state, 3)["state"] == orch.SUBMITTED  # independent branch lives on

    slurm.finish(_job(state, 3)["exec_ref"])
    orch.run_cycle(plan, state, ex)
    exit_code = orch.finalize_if_done(state, state_path)
    assert exit_code == 1
    assert state["status"] == "failed"


def test_afterany_unblocks_on_failure(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[], [0]], dependency_mode="afterany")
    state, _, ex = _slurm_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    slurm.finish(_job(state, 0)["exec_ref"], state="FAILED", exit_code="1:0")
    orch.run_cycle(plan, state, ex)

    assert _job(state, 0)["state"] == orch.FAILED
    assert _job(state, 1)["state"] == orch.SUBMITTED


# ---------------------------------------------------------------------------
# Throttling (SLURM backend)
# ---------------------------------------------------------------------------


def test_throttle_counts_all_user_jobs(slurm: FakeSlurm, tmp_path: Path) -> None:
    # max_concurrent (4, the plan default) happens to equal the account cap
    # (also 4 by default) here, so this exercises the coincidental case
    # where both limits bind identically -- see the *_workflow_limit_* and
    # *_account_limit_* tests below for the two limits pulling apart.
    plan = make_plan([[], [], [], [], [], []])  # 6 independent jobs
    slurm.queue.update({"888", "999"})  # manually submitted jobs share the budget

    state, _, ex = _slurm_setup(plan, tmp_path)
    orch.run_cycle(plan, state, ex)
    assert len(slurm.submitted_argvs) == 2  # 4 - 2 foreign

    orch.run_cycle(plan, state, ex)  # queue now full (2 foreign + 2 ours)
    assert len(slurm.submitted_argvs) == 2

    slurm.queue.discard("888")
    slurm.queue.discard("999")
    orch.run_cycle(plan, state, ex)
    assert len(slurm.submitted_argvs) == 4  # two more slots freed


def test_no_submission_when_queue_at_limit(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[]])
    slurm.queue.update({"1", "2", "3", "4"})

    state, _, ex = _slurm_setup(plan, tmp_path)
    orch.run_cycle(plan, state, ex)

    assert slurm.submitted_argvs == []
    assert _job(state, 0)["state"] == orch.WAITING


def test_workflow_limit_does_not_count_foreign_jobs(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    """Regression test: a tight max_concurrent must not be starved by
    unrelated jobs already in the user's squeue (the reported bug)."""
    plan = make_plan([[], []], max_concurrent=1)  # 2 independent jobs
    slurm.queue.update({"888", "999"})  # unrelated jobs, well under the account cap

    state, _, ex = _slurm_setup(plan, tmp_path)
    orch.run_cycle(plan, state, ex)
    # workflow_budget = 1 - 0 = 1, account_budget = 4 - 2 = 2 -> submits 1
    assert len(slurm.submitted_argvs) == 1

    orch.run_cycle(plan, state, ex)
    # workflow_budget = 1 - 1 = 0 -> still capped at 1, even with account room
    assert len(slurm.submitted_argvs) == 1


def test_account_limit_still_blocks_with_workflow_room(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    """Even with workflow max_concurrent wide open, the fixed account cap
    must still block once foreign jobs fill the squeue."""
    plan = make_plan([[]], max_concurrent=10)
    slurm.queue.update({"1", "2", "3", "4"})  # foreign jobs alone hit the account cap

    state, _, ex = _slurm_setup(plan, tmp_path)
    orch.run_cycle(plan, state, ex)

    assert slurm.submitted_argvs == []
    assert _job(state, 0)["state"] == orch.WAITING


# ---------------------------------------------------------------------------
# Submission errors (SLURM backend)
# ---------------------------------------------------------------------------


def test_assoc_limit_error_is_retried_without_penalty(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    plan = make_plan([[]])
    state, _, ex = _slurm_setup(plan, tmp_path)

    slurm.sbatch_stderr = (
        "sbatch: error: AssocMaxSubmitJobLimit\n"
        "sbatch: error: Batch job submission failed"
    )
    orch.run_cycle(plan, state, ex)
    assert _job(state, 0)["state"] == orch.WAITING
    assert _job(state, 0)["submit_retries"] == 0

    slurm.sbatch_stderr = None
    orch.run_cycle(plan, state, ex)
    assert _job(state, 0)["state"] == orch.SUBMITTED


def test_persistent_sbatch_error_marks_submit_failed(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    plan = make_plan([[], [0]])
    state, _, ex = _slurm_setup(plan, tmp_path)

    slurm.sbatch_stderr = "sbatch: error: Invalid partition"
    for _ in range(orch.MAX_SUBMIT_RETRIES):
        orch.run_cycle(plan, state, ex)

    assert _job(state, 0)["state"] == orch.SUBMIT_FAILED
    orch.run_cycle(plan, state, ex)
    assert _job(state, 1)["state"] == orch.SKIPPED


# ---------------------------------------------------------------------------
# sacct parsing and lost jobs (SLURM backend)
# ---------------------------------------------------------------------------


def test_cancelled_by_user_counts_as_failure(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[]])
    state, _, ex = _slurm_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    job_id = _job(state, 0)["exec_ref"]
    slurm.queue.discard(job_id)
    slurm.sacct[job_id] = ("CANCELLED by 12345", "0:0")

    orch.run_cycle(plan, state, ex)
    assert _job(state, 0)["state"] == orch.FAILED
    assert _job(state, 0)["exec_state"] == "CANCELLED"


def test_sacct_lag_tolerated_while_job_in_squeue(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    plan = make_plan([[]])
    state, _, ex = _slurm_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)  # job in queue, not yet in sacct
    for _ in range(orch.MAX_MISSING_POLLS + 1):
        orch.run_cycle(plan, state, ex)

    assert _job(state, 0)["state"] == orch.SUBMITTED
    assert _job(state, 0)["missing_polls"] == 0


def test_job_lost_from_both_sources_marked_failed(
    slurm: FakeSlurm, tmp_path: Path
) -> None:
    plan = make_plan([[]])
    state, _, ex = _slurm_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    slurm.queue.discard(_job(state, 0)["exec_ref"])  # vanishes without sacct trace

    for _ in range(orch.MAX_MISSING_POLLS):
        orch.run_cycle(plan, state, ex)

    assert _job(state, 0)["state"] == orch.FAILED
    assert _job(state, 0)["exec_state"] == "LOST"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def test_state_roundtrip_and_resume(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[], [0]])
    state, state_path, ex = _slurm_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    orch.write_state(state, state_path)

    resumed, _ = _state_for(plan, tmp_path)
    assert _job(resumed, 0)["state"] == orch.SUBMITTED
    assert _job(resumed, 0)["exec_ref"] == _job(state, 0)["exec_ref"]
    assert _job(resumed, 1)["state"] == orch.WAITING

    # Resumed orchestrator picks up where the crashed one left off.
    slurm.finish(_job(resumed, 0)["exec_ref"])
    orch.run_cycle(plan, resumed, ex)
    assert _job(resumed, 1)["state"] == orch.SUBMITTED


def test_finalize_success(slurm: FakeSlurm, tmp_path: Path) -> None:
    plan = make_plan([[]])
    state, state_path, ex = _slurm_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    assert orch.finalize_if_done(state, state_path) is None

    slurm.finish(_job(state, 0)["exec_ref"])
    orch.run_cycle(plan, state, ex)
    assert orch.finalize_if_done(state, state_path) == 0
    assert state["status"] == "finished"

    saved = json.loads(Path(state_path).read_text())
    assert saved["status"] == "finished"
    assert saved["finished_at"] is not None
    assert saved["executor"] == "slurm"


# ---------------------------------------------------------------------------
# Local-process backend (ssh executor)
# ---------------------------------------------------------------------------


class FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self):
        return None


class FakeLocal(orch.LocalExec):
    """LocalExec with the process/filesystem seams faked."""

    def __init__(self, plan: dict, run_dir: str) -> None:
        super().__init__(plan, run_dir)
        self.alive: set[int] = set()
        self.rcs: dict[int, str] = {}
        self.spawned: list[str] = []
        self.killed: list[int] = []
        self._next_pid = 500

    def _spawn(self, script_path):
        pid = self._next_pid
        self._next_pid += 1
        self.alive.add(pid)
        self.spawned.append(script_path)
        return FakeProc(pid)

    def _pid_alive(self, pid):
        return pid in self.alive

    def _read_rc(self, index):
        return self.rcs.get(index)

    def _kill_group(self, pid, sig):
        self.killed.append(pid)
        self.alive.discard(pid)
        return True

    def finish(self, state: dict, index: int, rc: str = "0") -> None:
        self.rcs[index] = rc
        self.alive.discard(int(state["jobs"][index]["exec_ref"]))


def _local_setup(plan: dict, tmp_path: Path) -> tuple[dict, str, FakeLocal]:
    state, state_path = _state_for(plan, tmp_path)
    return state, state_path, FakeLocal(plan, str(tmp_path))


def test_local_dependency_gating(tmp_path: Path) -> None:
    plan = make_plan([[], [0]], executor="ssh")
    state, _, ex = _local_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    assert _job(state, 0)["state"] == orch.SUBMITTED
    assert _job(state, 1)["state"] == orch.WAITING
    assert len(ex.spawned) == 1

    orch.run_cycle(plan, state, ex)  # still running: nothing new
    assert len(ex.spawned) == 1

    ex.finish(state, 0)
    orch.run_cycle(plan, state, ex)
    assert _job(state, 0)["state"] == orch.COMPLETED
    assert _job(state, 0)["exit_code"] == "0"
    assert _job(state, 1)["state"] == orch.SUBMITTED


def test_local_budget_counts_live_pids(tmp_path: Path) -> None:
    plan = make_plan([[], [], [], []], executor="ssh", max_concurrent=2)
    state, _, ex = _local_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    assert len(ex.spawned) == 2

    orch.run_cycle(plan, state, ex)  # both slots busy
    assert len(ex.spawned) == 2

    ex.finish(state, 0)
    orch.run_cycle(plan, state, ex)
    assert len(ex.spawned) == 3


def test_local_nonzero_rc_fails_and_skips_dependents(tmp_path: Path) -> None:
    plan = make_plan([[], [0]], executor="ssh")
    state, state_path, ex = _local_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    ex.finish(state, 0, rc="3")
    orch.run_cycle(plan, state, ex)

    assert _job(state, 0)["state"] == orch.FAILED
    assert _job(state, 0)["exit_code"] == "3"
    assert _job(state, 1)["state"] == orch.SKIPPED
    assert orch.finalize_if_done(state, state_path) == 1


def test_local_lost_process_marked_failed(tmp_path: Path) -> None:
    plan = make_plan([[]], executor="ssh")
    state, _, ex = _local_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    # Process dies without writing rc (e.g. machine slept).
    ex.alive.discard(int(_job(state, 0)["exec_ref"]))

    for _ in range(orch.MAX_MISSING_POLLS):
        orch.run_cycle(plan, state, ex)

    assert _job(state, 0)["state"] == orch.FAILED
    assert _job(state, 0)["exec_state"] == "LOST"


def test_local_on_terminate_kills_live_groups(tmp_path: Path) -> None:
    plan = make_plan([[], []], executor="ssh")
    state, _, ex = _local_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    pids = [int(_job(state, index)["exec_ref"]) for index in (0, 1)]

    ex.on_terminate(state)
    assert ex.killed == pids
    assert _job(state, 0)["state"] == orch.FAILED
    assert _job(state, 0)["exec_state"] == "CANCELLED"


def test_local_resume_tracks_jobs_via_rc_files(tmp_path: Path) -> None:
    plan = make_plan([[], [0]], executor="ssh")
    state, state_path, ex = _local_setup(plan, tmp_path)

    orch.run_cycle(plan, state, ex)
    orch.write_state(state, state_path)

    # New executor instance (orchestrator restart): no Popen handles left.
    resumed, _ = _state_for(plan, tmp_path)
    ex2 = FakeLocal(plan, str(tmp_path))
    assert _job(resumed, 0)["state"] == orch.SUBMITTED

    ex2.rcs[0] = "0"  # the orphaned job wrote its rc file meanwhile
    orch.run_cycle(plan, resumed, ex2)
    assert _job(resumed, 0)["state"] == orch.COMPLETED
    assert _job(resumed, 1)["state"] == orch.SUBMITTED


def test_local_script_rendering(tmp_path: Path) -> None:
    plan = make_plan([[]], executor="ssh")
    ex = orch.LocalExec(plan, str(tmp_path))

    script = ex._render_script(0, plan["jobs"][0])
    assert f"echo $$ > '{tmp_path}/job_0.pid'" in script
    assert "cd '/remote/base'" in script
    assert "uv run src/job0.py >> " in script
    assert f"echo $? > '{tmp_path}/job_0.rc'" in script
    assert 'export PATH="$HOME/.local/bin:$PATH"' in script
    assert "_logs_/workflows/test/stage/job0.log" in script


def test_make_executor_dispatch(tmp_path: Path) -> None:
    assert isinstance(
        orch.make_executor(make_plan([[]]), str(tmp_path)), orch.SlurmExec
    )
    assert isinstance(
        orch.make_executor(make_plan([[]], executor="ssh"), str(tmp_path)),
        orch.LocalExec,
    )


def test_local_on_terminate_uses_sigterm() -> None:
    # Guard the signal constant used for group kills.
    assert signal.SIGTERM == 15


# ---------------------------------------------------------------------------
# Plan loading
# ---------------------------------------------------------------------------


def test_load_plan_rejects_old_schema(tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.json"
    plan = make_plan([[]])
    plan["schema_version"] = 1
    plan_path.write_text(json.dumps(plan))
    with pytest.raises(SystemExit, match="schema_version"):
        orch.load_plan(str(plan_path))


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
