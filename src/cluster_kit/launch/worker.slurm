#!/bin/bash
# Generic SLURM worker script for Python scripts.
#
# This script sets up the cluster environment (conda, modules, temp dirs)
# and executes the Python command passed as positional arguments ($@).
#
# It is NOT meant to be submitted directly — the launcher module
# generates the correct sbatch command automatically and overrides
# the default #SBATCH directives below.
#
# Usage (via launcher — you don't call this directly):
#
#   Single job:
#     sbatch --partition=cpu_shared --cpus-per-task=16 --mem=64G --time=04:00:00 \
#       worker.slurm python path/to/script.py [args]
#
# Customization via environment variables:
#   PROJECT_DIR   — Project root directory (default: auto-detected from script path)
#   CONDA_ENV     — Conda environment name or path (default: "conda_envs")
#   TEXLIVE       — Set to "1" to enable TeX Live (for visualization scripts)
#   MAIL_USER     — Email address for SLURM notifications

# Default SBATCH directives (overridden by CLI args from the launcher)
#SBATCH --partition=cpu_express
#SBATCH --qos=cpu_express
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --time=02:00:00
#SBATCH --mem=64G
#SBATCH --mail-type=BEGIN,END,FAIL
#SBATCH --mail-user=computingjvm@gmail.com

set -euo pipefail

# ==============================================================================
# Project directory (override via PROJECT_DIR env var)
# ==============================================================================
PROJECT_DIR="${PROJECT_DIR:-.}"

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

# ==============================================================================
# Environment setup
# ==============================================================================
# Activate conda environment (override via CONDA_ENV env var)
CONDA_ENV="${CONDA_ENV:-conda_envs}"
if [[ -d "$CONDA_ENV" ]]; then
    # Path-based environment
    conda activate "$CONDA_ENV"
else
    # Named environment
    conda activate "$CONDA_ENV"
fi

# TeX Live (activated when TEXLIVE=1 env var is set by the launcher)
if [[ "${TEXLIVE:-0}" == "1" ]]; then
    if [[ -f "${PROJECT_DIR}/runnables/sh/helpers/texlive_path.sh" ]]; then
        source "${PROJECT_DIR}/runnables/sh/helpers/texlive_path.sh"
    fi
fi

cd "$PROJECT_DIR"

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
# Execute the Python command
# ==============================================================================
"$@"
