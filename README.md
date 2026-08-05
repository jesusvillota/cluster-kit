# Cluster Kit

CLI toolkit for cluster management, code synchronization, and SLURM job submission.

Besides SLURM clusters, cluster-kit can target plain SSH machines (e.g. a
Windows PC running WSL behind a VPN) through the **ssh executor**: detached
jobs (`cluster-kit job ...`), synchronous commands (`cluster-kit exec ...`),
git-based code sync, and workflows without sbatch. See
[docs/ssh-executor.md](docs/ssh-executor.md) and
[docs/pc-ssh-setup.md](docs/pc-ssh-setup.md).

## Installation

```bash
# Clone the repository
git clone https://github.com/your-org/cluster-kit.git
cd cluster-kit

# Install in editable mode
pip install -e .

# Or with development dependencies
pip install -e ".[dev]"
```

**Requirements:** Python 3.10+

## Configuration

Copy the example environment file and fill in your cluster details:

```bash
cp .env.example .env
```

Edit `.env` with your cluster connection settings. See [Environment Variables](#environment-variables) for the full reference.

Verify your configuration:

```bash
cluster-kit --config
```

## Quick Start

```bash
# 1. Check your cluster configuration
cluster-kit --config

# 2. Sync your source code to the cluster
cluster-kit sync code

# 3. Submit a script as a SLURM job
cluster-kit launch my_script.py --partition cpu_shared --slurm-cpus 16

# 4. Monitor jobs with the interactive TUI
cluster-kit tui

# 5. Pull visualization outputs back from the cluster
cluster-kit sync outputs --visualization
```

## Commands

### `--config`

Display the current cluster configuration and validate settings.

```bash
cluster-kit --config
```

Output shows host, user, remote base path, SSH key, timeout, and sync exclude patterns. Warnings appear for any validation issues.

### `sync`

File synchronization commands with three subcommands.

#### `sync code`

Push source code to the cluster via rsync over SSH. Excludes patterns defined in `CLUSTER_SYNC_EXCLUDE`.

```bash
# Sync code to cluster
cluster-kit sync code

# Preview changes without syncing
cluster-kit sync code --dry-run

# Verbose output
cluster-kit sync code --verbose
```

| Flag | Description |
|---|---|
| `--dry-run` | Preview changes without syncing |
| `--verbose` | Show detailed rsync output |

#### `sync outputs`

Pull output files from the cluster to your local machine.

```bash
# Sync visualization outputs (default)
cluster-kit sync outputs

# Sync all outputs
cluster-kit sync outputs --all

# Sync only processed data
cluster-kit sync outputs --processed

# Sync specific formats
cluster-kit sync outputs --formats pdf,png,tex

# Delete local files not on cluster
cluster-kit sync outputs --delete

# Show directory tree after sync
cluster-kit sync outputs --show-tree
```

| Flag | Description |
|---|---|
| `--all` | Sync all output types |
| `--visualization` | Sync visualization outputs only (default) |
| `--processed` | Sync processed data only |
| `--formats` | Comma-separated file formats (pdf,png,tex,csv,json,parquet,yaml) |
| `--dry-run` | Preview without syncing |
| `--delete` | Remove local files not present on cluster |
| `--verbose` | Show detailed output |
| `--show-tree` | Display directory tree after sync |

#### `sync cp`

Copy files between local and cluster using SCP.

```bash
# Copy local file to cluster
cluster-kit sync cp data.csv user@cluster:/remote/path/

# Copy from cluster to local
cluster-kit sync cp user@cluster:/remote/output.pdf ./

# Copy directory recursively
cluster-kit sync cp -r ./results user@cluster:/remote/results/

# Preview the operation
cluster-kit sync cp --dry-run ./file.txt user@cluster:/path/
```

| Flag | Description |
|---|---|
| `-r, --recursive` | Copy directories recursively |
| `--dry-run` | Preview the copy operation |
| `-v, --verbose` | Show detailed output |

**Path format:** Use `user@cluster:/absolute/path` for cluster paths. Local paths work as normal.

### `tui`

Launch the interactive terminal UI for cluster management. Shows running SLURM jobs, queue status, and cluster health.

```bash
# Standard TUI
cluster-kit tui

# Phone-optimized layout
cluster-kit tui --phone

# Custom refresh interval (10 seconds)
cluster-kit tui --refresh 10

# Show only your jobs
cluster-kit tui --user-only
```

| Flag | Description |
|---|---|
| `--phone` | Optimize layout for phone screens |
| `--refresh N` | Refresh interval in seconds (default: 60) |
| `--user-only` | Show only the current user's jobs |

### `launch`

Submit a Python script as a SLURM job on the cluster.

```bash
# Submit with defaults (cpu_shared, 16 CPUs, 64G, 4 hours)
cluster-kit launch process_data.py

# Custom resource allocation
cluster-kit launch train_model.py \
    --partition gpu_compute \
    --slurm-cpus 32 \
    --slurm-mem 122G \
    --slurm-time 24:00:00

# Auto-sync code before launching
cluster-kit launch analyze.py --sync

# Run locally instead of submitting to cluster
cluster-kit launch test.py --run-from local
```

| Flag | Default | Description |
|---|---|---|
| `script` | (required) | Path to the Python script to submit |
| `--run-from` | `cluster` | Execution target: `local` or `cluster` |
| `--partition` | `cpu_shared` | SLURM partition |
| `--slurm-cpus` | `16` | CPUs per task |
| `--slurm-mem` | `64G` | Memory per job |
| `--slurm-time` | `04:00:00` | Wall-clock time limit |
| `--sync` | `False` | Auto-sync code before submitting |

### `workflow run`

Launch a YAML-defined sequence of raw `uv run` commands as SLURM jobs. Cluster
Kit pre-renders every job's sbatch command into an execution plan, uploads it to
the cluster, and starts a detached orchestrator on the login node, so your local
machine does not need to poll or stay connected. The orchestrator submits each
job once its dependencies have completed, keeping the user's total queued job
count (pending + running, including jobs submitted outside the workflow) below
`max_concurrent` — so workflows with many more jobs than the association
MaxSubmit/MaxJobs limit run end to end without `AssocMaxSubmitJobLimit` errors.
TOML is still supported, but YAML is the recommended format.

```bash
cluster-kit workflow run abnormal-volume.yaml

# Validate the plan without submitting jobs
cluster-kit workflow run abnormal-volume.yaml --dry-run

# Check progress of the latest run / a specific run
cluster-kit workflow status --latest
cluster-kit workflow status abnormal-volume_20260611-153000 --log

# Kill the orchestrator and scancel its active jobs
cluster-kit workflow cancel --latest
```

Each run gets a directory `<remote_base>/.cluster_kit/workflows/<run_id>/` on
the cluster holding `plan.json`, `state.json` (updated every poll cycle),
`orchestrator.log`, and `orchestrator.pid`. If the orchestrator dies, re-running
`python3 <run_dir>/orchestrator.py <run_dir>/plan.json` on the login node
resumes from the saved state.

If you define `jobs:` at the top level, the workflow runs in chain mode. If you
define `stages:`, the workflow runs in stages mode.

```yaml
name: abnormal-volume
dependency: afterok
sync: true
max_concurrent: 4   # optional: max queued jobs at any time (default 4)
poll_interval: 30   # optional: orchestrator poll cadence in seconds

stages:
  - name: build-panels
    parallel: true
    jobs:
      - name: panel-2015-2019
        command: |
          uv run src/build_panel.py \
            --years 2015 to 2019 \
            --run-from cluster --partition cpu_long

      - name: panel-2020-2022
        command: |
          uv run src/build_panel.py \
            --years 2020 to 2022 \
            --run-from cluster --partition cpu_large

  - name: plots
    parallel: true
    jobs:
      - name: plot-comparison
        command: |
          uv run src/plot.py --run-from cluster --partition cpu_express
```
```

| Flag | Default | Description |
|---|---|---|
| `workflow_file` | (required) | YAML or TOML workflow definition |
| `--dry-run` | `False` | Validate and preview without submitting |
| `--project-root` | from file | Override local project root for sync |
| `--no-sync` | `False` | Skip pre-submission code sync |
| `--dependency` | from file | Override `afterok` or `afterany` |
| `--max-concurrent` | file → `$CLUSTER_MAX_JOBS` → 4 | Max queued jobs at any time |
| `--poll-interval` | file → 30 | Orchestrator poll cadence (seconds) |

### `serve`

Manage a ttyd server for remote phone access to the cluster TUI.

```bash
# Start phone access server
cluster-kit serve start

# Start with phone-optimized UI
cluster-kit serve start --phone-ui

# Check server status
cluster-kit serve status

# Stop the server
cluster-kit serve stop

# Custom port
cluster-kit serve start --port 8080
```

| Subcommand | Description |
|---|---|
| `start` | Start tmux + ttyd phone access |
| `status` | Show server status |
| `stop` | Stop the server |

| Flag | Default | Description |
|---|---|---|
| `--port` | `7681` | ttyd port for phone access |
| `--session-name` | `cluster-kit-phone` | tmux session name |
| `--phone-ui` | `False` | Start phone-oriented TUI |
| `--qa-safe-mode` | `False` | Route operations through QA-safe stubs |

## Environment Variables

All variables are loaded from `.env` (via `python-dotenv`) or set directly in your shell.

### Core Settings

| Variable | Default | Required | Description |
|---|---|---|---|
| `CLUSTER_HOST` | `cluster` | No | SSH alias or hostname for the cluster |
| `CLUSTER_USER` | `os.getenv('USER')` | No | Username on the remote cluster |
| `CLUSTER_REMOTE_BASE` | *(none)* | **Yes** | Absolute path to project root on cluster (see [Worktree isolation](#worktree-isolation)) |
| `CLUSTER_SSH_KEY` | `~/.ssh/id_ed25519_cluster` | No | Path to SSH private key |
| `CLUSTER_SSH_TIMEOUT` | `30` | No | SSH connection timeout (1-300 seconds) |
| `CLUSTER_SYNC_EXCLUDE` | `__pycache__,*.pyc,*.pyo` | No | Comma-separated rsync exclude patterns |
| `CLUSTER_EXECUTOR` | `slurm` | No | Job backend: `slurm` (sbatch) or `ssh` (detached processes on a plain machine) |
| `CLUSTER_SYNC_MODE` | `rsync` | No | Code sync: `rsync` (push working tree) or `git` (remote clone pulls from GitHub) |

### Worktree Isolation

`sync code` is destructive — it removes `src/` and `runnables/` on the cluster before
rsyncing with `--delete`. With several agent sessions working in parallel git worktrees of
the same repo, a sync from one worktree would delete the code another worktree's queued
jobs are about to run.

So when the current directory sits inside a **linked git worktree**, the worktree name is
appended to `CLUSTER_REMOTE_BASE`:

```
~/GitHub/whales                        → /mnt/.../scripts_whales
~/worktrees/whales/price-pressure      → /mnt/.../scripts_whales__price-pressure
```

Nothing to configure — detection is the `.git` file-vs-directory distinction that git
itself uses. The main checkout is unaffected, and `CLUSTER_SYNC_MODE=git` is exempt (it
targets a single remote checkout at a fixed path).

The worktree deployment **owns only what it syncs** (`src/`, `runnables/`, plus its own
`_logs_/` and `.cluster_kit/`). Everything else at the canonical remote root — conda envs,
`output/`, `data/`, and machine-local files like `.env` — is symlinked back on first sync,
so nothing is rebuilt or re-mirrored and results still accumulate in one place.

`PROJECT_DIR` is exported into every job, so worker scripts should resolve their base
directory from it (`BASE_DIR="${PROJECT_DIR:-<fallback>}"`) rather than hardcoding a path.

Use `get_canonical_remote_base()` when you need the shared path regardless of worktree.

### Multi-Cluster Profiles

Set `CLUSTER_ENV` to select a profile. The toolkit looks for `CLUSTER_{PROFILE}_*` prefixed variables, falling back to unprefixed `CLUSTER_*`.

| Variable | Description |
|---|---|
| `CLUSTER_ENV` | Active profile name (e.g., `dev`, `prod`, `staging`) |

Example `.env` with multiple profiles:

```bash
# Default cluster
CLUSTER_HOST=cluster
CLUSTER_USER=j-vill36
CLUSTER_REMOTE_BASE=/mnt/slurm-beegfs/Users/j-vill36/scripts_whales

# Development profile (activated with CLUSTER_ENV=dev)
CLUSTER_DEV_HOST=dev-cluster
CLUSTER_DEV_USER=devuser
CLUSTER_DEV_REMOTE_BASE=/home/devuser/project
CLUSTER_DEV_SSH_KEY=~/.ssh/dev_cluster_key
```

### Phone Access (serve)

| Variable | Default | Description |
|---|---|---|
| `CLUSTER_KIT_PHONE_PORT` | `7681` | ttyd port for phone access |
| `CLUSTER_KIT_PHONE_SESSION_NAME` | `cluster-kit-phone` | tmux session name |
| `CLUSTER_KIT_PHONE_COMMAND` | *(none)* | Custom command to run inside tmux |
| `CLUSTER_KIT_QA_SAFE_MODE` | `0` | Enable QA-safe stub mode (`1` to enable) |

## Project Structure

```
cluster-kit/
├── src/cluster_kit/
│   ├── cli.py              # CLI entry point and argument parsing
│   ├── config.py           # Configuration loading and validation
│   ├── common/             # Shared utilities
│   ├── sync/               # File synchronization (code, outputs, cp)
│   ├── tui/                # Textual-based terminal UI
│   └── launch/             # SLURM job submission
├── tests/                  # Test suite
├── .env.example            # Environment variable template
└── pyproject.toml          # Project metadata and dependencies
```

## Troubleshooting

### SSH connection fails

- Verify `CLUSTER_HOST` is resolvable and in `~/.ssh/config`
- Check `CLUSTER_SSH_KEY` exists and has permissions `600` or `400`:
  ```bash
  chmod 600 ~/.ssh/id_ed25519_cluster
  ```
- Test connectivity: `ssh -i $CLUSTER_SSH_KEY $CLUSTER_USER@$CLUSTER_HOST echo ok`

### `CLUSTER_REMOTE_BASE is required but not set`

This variable has no default. Set it to the absolute path of your project on the cluster:

```bash
CLUSTER_REMOTE_BASE=/mnt/slurm-beegfs/Users/your-user/project
```

### Sync excludes not working

`CLUSTER_SYNC_EXCLUDE` must be comma-separated with **no spaces**:

```bash
# Correct
CLUSTER_SYNC_EXCLUDE=__pycache__,*.pyc,*.pyo,.git

# Wrong (spaces around commas)
CLUSTER_SYNC_EXCLUDE=__pycache__, *.pyc, *.pyo
```

### TUI shows no jobs

- Ensure you have SSH access to the cluster
- Use `--user-only` if you want to hide other users' jobs
- Increase `--refresh` if the cluster is slow to respond

### `serve start` fails

- Ensure `tmux` and `ttyd` are installed on the cluster
- Check the port is not already in use: `cluster-kit serve status`
- Use `--qa-safe-mode` for testing without affecting running jobs

### Configuration validation

Run `cluster-kit --config` to see your current settings and any validation warnings. Common issues:

- `CLUSTER_REMOTE_BASE` must be an absolute path (starts with `/`)
- `CLUSTER_SSH_TIMEOUT` must be between 1 and 300
- `CLUSTER_SYNC_EXCLUDE` must not contain spaces around commas

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Lint
ruff check .
ruff format .
```
