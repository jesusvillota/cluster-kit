# Cluster Kit

CLI toolkit for cluster management, code synchronization, and SLURM job submission.

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

# Show all users' jobs
cluster-kit tui --all-users
```

| Flag | Description |
|---|---|
| `--phone` | Optimize layout for phone screens |
| `--refresh N` | Refresh interval in seconds (default: 5) |
| `--all-users` | Show jobs for all cluster users |

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
| `CLUSTER_REMOTE_BASE` | *(none)* | **Yes** | Absolute path to project root on cluster |
| `CLUSTER_SSH_KEY` | `~/.ssh/id_ed25519_cluster` | No | Path to SSH private key |
| `CLUSTER_SSH_TIMEOUT` | `30` | No | SSH connection timeout (1-300 seconds) |
| `CLUSTER_SYNC_EXCLUDE` | `__pycache__,*.pyc,*.pyo` | No | Comma-separated rsync exclude patterns |

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
- Use `--all-users` if jobs are running under a different user
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
