# Workflow Dependency Submission

- [x] Submit all workflow stages up front using SLURM dependencies.
- [x] Preserve sequential dependencies inside `parallel: false` stages.
- [x] Update tests for previous-stage dependency fan-in.
- [x] Run `uv run ruff check` and `uv run pytest`.

## Review

Implemented workflow submission as an up-front SLURM dependency graph. The
local runner no longer polls `squeue`/`sacct` between stages; stage ordering is
enforced by `--dependency=afterok:...` or `--dependency=afterany:...` during
submission. Verification: `uv run ruff check` and `uv run pytest` both pass.
