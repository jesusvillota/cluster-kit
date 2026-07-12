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

# Phone Log Horizontal Scroll

- [x] Add compact-only left/right log scroll buttons.
- [x] Route the buttons to horizontal `RichLog` scrolling.
- [x] Extend focused log-scroll tests.
- [x] Run focused verification.
- [x] Restart the phone UI.

## Review

Added phone-only `←` and `→` log controls, routed them to horizontal
`RichLog` scrolling, and preserved full-width long log lines with
`shrink=False`. Verification: `uv run pytest tests/test_tui_log_scroll.py` and
`uv run ruff check src/cluster_kit/tui/widgets/log_viewer.py tests/test_tui_log_scroll.py`
both pass. Restarted `cluster-kit serve --phone-ui`; status reports running at
`http://127.0.0.1:7681`.

# Phone Log Button Layout

- [x] Update compact log scroll controls to the requested two-row layout.
- [x] Remove phone Refresh, Log, and Sync buttons from the action dock.
- [x] Preserve log loading after removing the Log button.
- [x] Update focused tests for layout and routing.
- [x] Run focused verification.

## Review

Implemented the phone log button distribution as two compact scroll rows plus
the two-button action dock. Verification: `uv run pytest tests/test_tui_log_scroll.py`,
`uv run pytest tests/test_tui_entrypoints.py tests/test_tui_queue_selection.py`,
and `uv run ruff check src/cluster_kit/tui/app_phone.py src/cluster_kit/tui/widgets/log_viewer.py src/cluster_kit/tui/styles.py tests/test_tui_log_scroll.py`
all pass.

# Phone View-Specific Action Dock

- [x] Restore `Refresh`, `Log`, `Cancel`, and `Sync` controls in Queue view.
- [x] Hide the bottom action dock in Nodes view.
- [x] Keep only `Out/Err` and `Cancel` in Logs view.
- [x] Stop auto-loading logs on queue selection now that the Log button exists.
- [x] Update focused tests for per-view dock visibility.
- [x] Run focused verification.

## Review

Corrected the phone dock to be view-specific instead of globally reduced.
Verification: `uv run pytest tests/test_tui_log_scroll.py`,
`uv run pytest tests/test_tui_entrypoints.py tests/test_tui_queue_selection.py`,
and `uv run ruff check src/cluster_kit/tui/app_phone.py src/cluster_kit/tui/widgets/log_viewer.py src/cluster_kit/tui/styles.py tests/test_tui_log_scroll.py`
all pass.

# State-Aware Resource Availability

- [x] Query and expose each node's live Slurm state.
- [x] Zero availability for down, unavailable, and unknown nodes.
- [x] Update resource-selection guidance in OpenCode and Claude.
- [x] Verify focused lint, tests, and the live resource probe.
- [ ] Commit, push, and upgrade the whales dependency.

## Review

`StateComplete` is now included in each resource row. Only `idle` and `mixed`
nodes report usable capacity; all other states, including `down*` and
`down+not_responding`, report zero availability. Focused lint and resource
tests pass, and the live probe reports HPCOM-05 as `down+not_responding` with
zero available CPUs, memory, and GPUs. The full suite has three pre-existing
configuration failures; full lint has one pre-existing line-length violation
in `src/cluster_kit/tui/phone_access.py`.
