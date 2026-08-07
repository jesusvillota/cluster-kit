#!/bin/bash
# Centralized SLURM worker script, owned by cluster-kit.
#
# Consuming repos do NOT keep a copy of this file. `cluster-kit sync code`
# deploys it to {remote_base}/.cluster_kit/worker.slurm on every sync, so the
# worker always matches the cluster-kit that deployed it. Each git worktree
# gets its own copy (.cluster_kit is never shared between worktrees).
#
# It is NOT submitted directly — the launcher builds the sbatch command and
# passes the python command as positional arguments ($@). Every #SBATCH flag
# that mattered is set on the sbatch command line by _build_sbatch_base, which
# is why this file carries no #SBATCH defaults.
#
# Environment variables (all exported by the launcher):
#   PROJECT_DIR          — Remote deployment root. REQUIRED; worktree-specific.
#   CLUSTER_REMOTE_BASE  — Same value; exported so cluster_kit's config inside
#                          the job resolves to the deployment it is running
#                          from rather than whatever the submitting shell had.
#   CLUSTER_TEXLIVE_ROOT — TeX Live installation root (optional).
#   TEXLIVE              — "1" to put TeX Live on PATH (visualization scripts).
#
# The python runtime is autodetected from the deployment: a .venv directory
# means uv, a conda_envs directory means conda.

set -euo pipefail

# ==============================================================================
# Deployment root
# ==============================================================================
# No fallback on purpose: a job that does not know its own deployment must fail
# loudly rather than silently run against a guessed path.
PROJECT_DIR="${PROJECT_DIR:?PROJECT_DIR not exported by the launcher}"

# Make the job self-describing: anything importing cluster_kit inside this job
# resolves the base it is actually running from.
export CLUSTER_KIT_JOB=1
export CLUSTER_REMOTE_BASE="${CLUSTER_REMOTE_BASE:-$PROJECT_DIR}"

# uv installs to ~/.local/bin, which non-interactive sbatch shells do not have
# on PATH (no .bashrc is sourced). Harmless when the runtime is conda.
export PATH="$HOME/.local/bin:$PATH"

# ==============================================================================
# Resource exports
# ==============================================================================
MEMORY_GB=$((SLURM_MEM_PER_NODE / 1024))
export CPU_LIMIT=$SLURM_CPUS_PER_TASK
export RAM_LIMIT=$MEMORY_GB
export PYTHONUNBUFFERED=1
export TMPDIR=/tmp
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK

# ==============================================================================
# Temp directories (array-aware naming)
# ==============================================================================
if [[ -n "${SLURM_ARRAY_TASK_ID:-}" ]]; then
    export DUCKDB_TEMP_DIR="/tmp/duckdb_${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}"
    export MPLCONFIGDIR="/tmp/matplotlib_${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID}"
else
    export DUCKDB_TEMP_DIR="/tmp/duckdb_${SLURM_JOB_ID}"
    export MPLCONFIGDIR="/tmp/matplotlib_${SLURM_JOB_ID}"
fi

mkdir -p "$DUCKDB_TEMP_DIR" "$MPLCONFIGDIR"
trap 'rm -rf "$DUCKDB_TEMP_DIR" "$MPLCONFIGDIR"' EXIT

cd "$PROJECT_DIR"

# ==============================================================================
# TeX Live (matplotlib text.usetex)
# ==============================================================================
# Must come before the runtime activation below so TeX Live wins over any LaTeX
# bundled in the environment.
if [[ "${TEXLIVE:-0}" == "1" ]]; then
    if [[ -n "${CLUSTER_TEXLIVE_ROOT:-}" ]]; then
        export PATH="${CLUSTER_TEXLIVE_ROOT}/bin/x86_64-linux:$PATH"
        export TEXMFHOME="${CLUSTER_TEXLIVE_ROOT}/texmf-local"
    else
        echo "WARNING: TEXLIVE=1 but CLUSTER_TEXLIVE_ROOT is unset" >&2
    fi
fi

# ==============================================================================
# Info banner
# ==============================================================================
echo "============================================="
echo "SLURM Worker - $(date)"
echo "============================================="
if [[ -n "${SLURM_ARRAY_TASK_ID:-}" ]]; then
    echo "Job ID         : ${SLURM_ARRAY_JOB_ID}_${SLURM_ARRAY_TASK_ID} (array)"
else
    echo "Job ID         : ${SLURM_JOB_ID}"
fi
echo "Partition      : ${SLURM_JOB_PARTITION}"
echo "CPUs           : ${SLURM_CPUS_PER_TASK}"
echo "Memory         : ${MEMORY_GB} GB"
echo "Project dir    : ${PROJECT_DIR}"
echo "Command        : $*"
echo "============================================="
echo ""

# ==============================================================================
# Runtime activation and execution
# ==============================================================================
if [[ -d .venv ]]; then
    echo "Runtime        : uv (.venv)"
    # --no-sync: the cluster has no git, so uv cannot resolve the cluster-kit
    # git dependency here. The venv is built on the login node and cluster_kit
    # is mirrored in by the deploy.
    exec uv run --frozen --no-sync "$@"
elif [[ -d conda_envs ]]; then
    echo "Runtime        : conda (conda_envs)"
    # Bootstrap the module system, then conda itself. Compute nodes give us a
    # bare non-interactive shell, so neither is on PATH by default.
    if [[ -f /etc/profile.d/modules.sh ]]; then
        source /etc/profile.d/modules.sh
    elif [[ -f /usr/share/modules/init/bash ]]; then
        source /usr/share/modules/init/bash
    elif [[ -f /opt/modules/init/bash ]]; then
        source /opt/modules/init/bash
    fi

    module load conda/3 2>/dev/null || {
        for _conda in /opt/conda/bin/conda /usr/local/conda/bin/conda /opt/anaconda3/bin/conda; do
            if [[ -f "$_conda" ]]; then
                export PATH="$(dirname "$_conda"):$PATH"
                break
            fi
        done
    }

    eval "$(conda shell.bash hook)" || {
        echo "ERROR: could not initialize conda" >&2
        exit 1
    }
    conda activate "${PROJECT_DIR}/conda_envs"
    exec "$@"
else
    echo "ERROR: no python runtime found in ${PROJECT_DIR}" >&2
    echo "       expected a .venv/ (uv) or conda_envs/ (conda) directory" >&2
    exit 1
fi
