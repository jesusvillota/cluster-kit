# Worker-Backed Workflow Submission

- [x] Resolve `runnables/slurm/worker.slurm` under `project_root` by default.
- [x] Support workflow-level and CLI worker-script overrides.
- [x] Route `submit_command` and `submit_job` through worker scripts.
- [x] Add tests for default worker lookup, overrides, and submission output.
- [x] Run `ruff` and `pytest`.

## Review

Implemented worker-script-backed SLURM submission so workflow jobs and direct
launches both use the project’s `worker.slurm` bootstrap by default. The
workflow CLI now accepts `--worker-script` for explicit overrides. Verification:
`uv run ruff check` and `uv run pytest` both pass.
