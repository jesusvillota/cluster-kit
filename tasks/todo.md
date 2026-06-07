# Workflow Runner Implementation

- [x] Extend launcher submission with SLURM dependency support.
- [x] Add generic command submission for workflow jobs.
- [x] Add YAML/TOML workflow parser supporting `chain` and `stages` modes.
- [x] Wire `cluster-kit workflow run <file>` into the CLI.
- [x] Add parser, CLI, and dependency-chain tests.
- [x] Run ruff and pytest.

## Review

Implemented `workflow run` with YAML-first workflows, chain mode, stages mode,
SLURM dependencies, dry-run support, README usage examples, and tests.
Verification: `uv run ruff check` and `uv run pytest` both pass.
