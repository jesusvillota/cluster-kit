# Generic Launcher Design for cluster-kit

## Problem Statement

The current `launcher.py` in the whales codebase is tightly coupled to whale-specific concepts:
- Hardcoded whale definition handling (`--whale-definitions`, `--whale-definition`)
- Environment variables `WHALE_DEF` and `WHALE_DEFS` injected into SLURM jobs
- Array mode specifically designed for parallel whale definition processing
- Worker script assumes whale-definition CLI flags

We need a **generic launcher** that:
- Supports any Python script without whale-specific assumptions
- Uses environment variables for customization instead of hardcoded whale concepts
- Maintains the same ease of use (single decorator/function call)
- Auto-detects project root and configuration
- Supports both single jobs and array jobs via user-provided configuration

---

## Whales-Specific Code Inventory

### 1. Functions to Remove or Make Generic

| Function | Whales-Specific? | Notes |
|----------|------------------|-------|
| `_resolve_whale_defs()` | **YES** | Extracts whale definitions from args |
| `_resolve_whale_defs_payload()` | **YES** | Returns original whale-definition CLI payload |
| `_resolve_whale_definition_flag()` | **YES** | Returns `--whale-definitions` or `--whale-definition` flag |
| `_run_local_parallel()` | **YES** | Spawns subprocesses with `--whale-definitions` |
| `_submit_single()` | **PARTIAL** | Injects `WHALE_DEF` env var and `--whale-definitions` flag |
| `_submit_single_preserving_whale_defs()` | **YES** | Preserves full whale-definition payload |
| `_submit_array()` | **PARTIAL** | Injects `WHALE_DEFS` env var for array indexing |
| `_handle_cluster_submission()` | **PARTIAL** | Routes to whale-specific submission logic |

### 2. Functions That Are Already Generic or Can Be Made Generic

| Function | Status | Generic Action |
|----------|--------|----------------|
| `add_launcher_args()` | **NEEDS UPDATE** | Remove whale-specific `--mode` description |
| `maybe_launch()` | **NEEDS UPDATE** | Replace whale_defs with generic env_vars dict |
| `maybe_launch_generic()` | **EXISTS** | Already generic, rename to `maybe_launch()` |
| `_handle_cluster_submission_generic()` | **EXISTS** | Already generic, keep as is |
| `_submit_single_generic()` | **EXISTS** | Already generic, keep as is |
| `resolve_slurm_resources()` | **GENERIC** | Keep as-is |
| `_build_sbatch_base()` | **GENERIC** | Keep as-is |
| `_derive_job_name()` | **GENERIC** | Keep as-is |
| `_derive_log_dir()` | **GENERIC** | Keep as-is |
| `_needs_texlive()` | **GENERIC** | Keep as-is |
| `_ssh_run()` | **GENERIC** | Keep as-is |
| `_ssh_submit()` | **GENERIC** | Keep as-is |
| `_confirm_and_prepare_cluster_submission()` | **GENERIC** | Keep as-is (uses PROJECT_ROOT) |
| `_run_cluster_sync()` | **GENERIC** | Keep as-is (uses PROJECT_ROOT) |

### 3. Constants and Configuration

| Constant | Whales-Specific? | Generic Replacement |
|----------|------------------|---------------------|
| `REMOTE_BASE` | **CONFIGURABLE** | Auto-detect or config file |
| `SSH_HOST` | **CONFIGURABLE** | Auto-detect or config file |
| `CONDA_ENV_PATH` | **CONFIGURABLE** | Derive from REMOTE_BASE |
| `MAIL_USER` | **CONFIGURABLE** | Config file or env var |
| `PROJECT_ROOT` | **GENERIC** | Auto-detect (current logic works) |
| `PARTITION_DEFAULTS` | **GENERIC** | Keep as-is |

### 4. CLI Arguments to Modify

| Argument | Current | Generic Change |
|----------|---------|----------------|
| `--mode` | Description mentions "whale definitions" | Generic description: "multi-job mode" |
| `--whale-definitions` | Stripped from argv | Generic: no special handling needed |

### 5. Environment Variables in Worker Script

Current `worker.slurm` uses:
```bash
# Whale-specific
WHALE_DEF=""      # Single definition
WHALE_DEFS=""     # Array of definitions
```

Generic version should use:
```bash
# Generic - user-defined
LAUNCHER_ENV_VARS=""   # JSON or key=value pairs
LAUNCHER_ARRAY_MODE="" # "true" or "false"
```

---

## Generic API Design

### New Public API

```python
# cluster_kit/launcher.py

from cluster_kit.launcher import add_launcher_args, maybe_launch

def parse_args():
    parser = argparse.ArgumentParser(...)
    add_launcher_args(
        parser,
        partition="cpu_shared",      # Default partition
        cpus=16,                      # Default CPUs
        mem="64G",                    # Default memory
        time="04:00:00",              # Default time
        array_mode=False,             # Whether to support --mode array
    )
    return parser.parse_args()

def main():
    args = parse_args()
    
    # Generic gate function
    if maybe_launch(
        __file__,
        args,
        env_vars={                    # Custom env vars for SLURM job
            "MY_CONFIG": "value",
            "EXPERIMENT_ID": "exp_123",
        },
    ):
        return
    
    # Normal processing...
```

### Function Signatures

```python
def add_launcher_args(
    parser: argparse.ArgumentParser,
    *,
    partition: str = "cpu_express",
    cpus: int | None = None,
    mem: str | None = None,
    time: str | None = None,
    array_mode: bool = False,        # Enable --mode array support
) -> None:
    """Add launcher CLI arguments to parser."""

def maybe_launch(
    script_path: str,
    args: argparse.Namespace,
    *,
    env_vars: dict[str, str] | None = None,  # Custom env vars for SLURM
) -> bool:
    """Gate function: handle execution if needed.
    
    Returns True if execution was handled (caller should exit),
    False if script should proceed with normal processing.
    """
```

### Configuration File (cluster-kit.yaml)

```yaml
# cluster-kit.yaml (auto-detected in project root)
remote:
  host: "cluster"                    # SSH host alias
  base_path: "/mnt/slurm-beegfs/Users/username/project"
  
resources:
  default_partition: "cpu_shared"
  default_cpus: 16
  default_mem: "64G"
  default_time: "04:00:00"
  
notifications:
  email: "user@example.com"
  
partitions:
  cpu_express:
    cpus: 16
    mem: "96G"
    time: "02:00:00"
  cpu_shared:
    cpus: 32
    mem: "240G"
    time: "24:00:00"
```

---

## Worker Script Changes

### Current `worker.slurm` (whales-specific)

```bash
#!/bin/bash
#SBATCH ...

# Load conda environment
eval "$(conda shell.bash hook)"
conda activate "$CONDA_ENV_PATH/whales"

# Change to project directory
cd "$REMOTE_BASE"

# Handle whale definitions
if [ -n "$WHALE_DEFS" ]; then
    # Array mode: extract definition from WHALE_DEFS
    IFS=',' read -ra DEFS <<< "$WHALE_DEFS"
    WHALE_DEF="${DEFS[$SLURM_ARRAY_TASK_ID]}"
fi

# Build command
CMD=("$@")
if [ -n "$WHALE_DEF" ]; then
    CMD+=("--whale-definitions" "$WHALE_DEF")
fi

# Run
"${CMD[@]}"
```

### Generic `worker.slurm`

```bash
#!/bin/bash
#SBATCH ...

# Configuration (auto-detected or from config)
REMOTE_BASE="{{REMOTE_BASE}}"
CONDA_ENV="{{CONDA_ENV}}"

# Load conda environment
eval "$(conda shell.bash hook)"
conda activate "$CONDA_ENV"

# Change to project directory
cd "$REMOTE_BASE"

# Set user-defined environment variables
{% for key, value in env_vars.items() %}
export {{key}}="{{value}}"
{% endfor %}

# Optional: Array mode handling
if [ -n "$LAUNCHER_ARRAY_INDEX_VAR" ]; then
    # User provided array data via env var
    export "$LAUNCHER_ARRAY_INDEX_VAR"="${ARRAY_VALUES[$SLURM_ARRAY_TASK_ID]}"
fi

# Run the command
"$@"
```

---

## Migration Guide

### From Whales Launcher to Generic Launcher

#### Step 1: Update imports

```python
# Old (whales)
from src.common.config.launcher import add_launcher_args, maybe_launch
from src.common.config.setup import add_whale_definitions_arg, parse_whale_definitions_arg

# New (generic)
from cluster_kit.launcher import add_launcher_args, maybe_launch
```

#### Step 2: Remove whale-specific argument handling

```python
# Old (whales)
def parse_args():
    parser = argparse.ArgumentParser(...)
    add_whale_definitions_arg(parser)  # REMOVE
    add_launcher_args(parser, partition="cpu_shared")
    return parser.parse_args()

def main():
    args = parse_args()
    whale_defs = parse_whale_definitions_arg(args.whale_definitions)  # REMOVE
    if maybe_launch(__file__, args, whale_defs):  # CHANGE
        return

# New (generic)
def parse_args():
    parser = argparse.ArgumentParser(...)
    add_launcher_args(parser, partition="cpu_shared")
    return parser.parse_args()

def main():
    args = parse_args()
    if maybe_launch(__file__, args):  # No whale_defs
        return
```

#### Step 3: Handle custom environment variables (optional)

```python
# New: Pass custom env vars to SLURM job
if maybe_launch(
    __file__,
    args,
    env_vars={
        "EXPERIMENT_NAME": "my_experiment",
        "DATA_VERSION": "v2.1",
    }
):
    return
```

#### Step 4: Create cluster-kit.yaml (optional)

```yaml
# cluster-kit.yaml in project root
remote:
  host: "cluster"
  base_path: "/mnt/slurm-beegfs/Users/username/my_project"
  
notifications:
  email: "user@example.com"
```

---

## Testing Strategy

### Unit Tests

1. **Configuration Loading**
   - Test auto-detection of project root
   - Test cluster-kit.yaml parsing
   - Test fallback to defaults

2. **Argument Parsing**
   - Test `add_launcher_args()` adds correct arguments
   - Test partition defaults are applied correctly
   - Test CLI overrides work

3. **maybe_launch() Logic**
   - Test `run_from=local` returns False
   - Test `run_from=cluster` triggers submission
   - Test env_vars are passed correctly

4. **SSH Helpers**
   - Mock SSH calls for testing
   - Test error handling

### Integration Tests

1. **Local Mode**
   - Script runs normally when `run_from=local`
   - No SLURM submission attempted

2. **Cluster Submission**
   - Mock SSH to verify correct sbatch command
   - Verify env vars are exported correctly
   - Verify log directories are created

3. **Configuration File**
   - Test with and without cluster-kit.yaml
   - Test partial configuration (some keys missing)

### Example Test Structure

```python
# tests/test_launcher.py

def test_maybe_launch_local_returns_false():
    args = argparse.Namespace(run_from="local")
    result = maybe_launch("/path/to/script.py", args)
    assert result is False

def test_maybe_launch_cluster_returns_true():
    args = argparse.Namespace(
        run_from="cluster",
        partition="cpu_shared",
        slurm_cpus=16,
        slurm_mem="64G",
        slurm_time="04:00:00",
    )
    with mock.patch('cluster_kit.launcher._ssh_submit') as mock_submit:
        mock_submit.return_value = "12345"
        result = maybe_launch("/path/to/script.py", args)
        assert result is True

def test_env_vars_passed_to_slurm():
    args = argparse.Namespace(run_from="cluster", ...)
    env_vars = {"MY_VAR": "value"}
    with mock.patch('cluster_kit.launcher._ssh_submit') as mock_submit:
        maybe_launch("/path/to/script.py", args, env_vars=env_vars)
        # Verify env vars in sbatch command
        call_args = mock_submit.call_args[0][0]
        assert "MY_VAR=value" in call_args
```

---

## File Structure

```
cluster-kit/
├── cluster_kit/
│   ├── __init__.py
│   ├── launcher.py              # Main launcher module
│   ├── config.py                # Configuration loading
│   └── templates/
│       └── worker.slurm.j2      # Jinja2 template for worker script
├── tests/
│   ├── test_launcher.py
│   ├── test_config.py
│   └── fixtures/
│       └── cluster-kit.yaml
├── docs/
│   └── launcher-design.md       # This document
└── pyproject.toml
```

---

## Summary

### Key Design Decisions

1. **Replace whale_defs with env_vars**: Instead of hardcoded whale-definition handling, accept a generic dictionary of environment variables.

2. **Configuration file**: Use `cluster-kit.yaml` for project-specific settings (remote host, base path, email) while keeping CLI overrides for resource specs.

3. **Keep existing SLURM logic**: The core SLURM submission logic (`_build_sbatch_base`, `_ssh_submit`, etc.) is already generic and can be reused.

4. **Template-based worker script**: Use Jinja2 templating for the worker script to allow customization while keeping a sensible default.

5. **Backward compatibility**: The whales codebase can continue using its current launcher while new projects use the generic one.

### Migration Path

1. Create generic launcher as new package (`cluster-kit`)
2. Whales codebase continues using existing launcher (no changes needed)
3. New projects use `cluster-kit`
4. Optional: Eventually migrate whales to cluster-kit if desired

### Benefits

- **Reusable**: Any Python project can use the launcher
- **Configurable**: Project-specific settings in YAML file
- **Simple**: Same ease of use as whales launcher
- **Extensible**: Easy to add new features without breaking existing code
- **Testable**: Clear separation of concerns enables unit testing
