"""Tests for the centralized worker.slurm.

These execute the real packaged script under a faked SLURM environment with
stub ``uv``/``conda`` binaries on PATH, so a behavioural break is caught without
touching a cluster.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cluster_kit.launch import get_worker_template

# Enough of the SLURM environment for the resource/banner block to evaluate.
FAKE_SLURM = {
    "SLURM_JOB_ID": "4242",
    "SLURM_CPUS_PER_TASK": "8",
    "SLURM_MEM_PER_NODE": "65536",
    "SLURM_JOB_PARTITION": "cpu_express",
}


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    """A PATH entry with `uv` and `conda` stubs that echo what they were given."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/bash\n"
        'echo "UV_CALLED: $*"\n'
        'echo "ENV_CLUSTER_REMOTE_BASE=${CLUSTER_REMOTE_BASE:-<unset>}"\n'
        'echo "ENV_CLUSTER_KIT_JOB=${CLUSTER_KIT_JOB:-<unset>}"\n'
        'echo "ENV_TEXMFHOME=${TEXMFHOME:-<unset>}"\n'
        'echo "ENV_PATH=$PATH"\n'
    )
    uv.chmod(0o755)

    # `conda shell.bash hook` output is eval'd, so it must be valid bash.
    conda = bin_dir / "conda"
    conda.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "shell.bash" ]]; then\n'
        '  echo "conda() { echo CONDA_CALLED: \\$*; }"\n'
        "  exit 0\n"
        "fi\n"
        'echo "CONDA_CALLED: $*"\n'
    )
    conda.chmod(0o755)
    return bin_dir


def run_worker(
    project_dir: Path,
    stub_bin: Path,
    *,
    env: dict | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    full_env = {
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "HOME": str(project_dir),
        "PROJECT_DIR": str(project_dir),
        **FAKE_SLURM,
        **(env or {}),
    }
    full_env = {k: v for k, v in full_env.items() if v is not None}
    return subprocess.run(
        ["bash", str(get_worker_template()), *(args or ["python", "-c", "pass"])],
        capture_output=True,
        text=True,
        env=full_env,
    )


class TestRuntimeDetection:
    def test_uv_when_venv_present(self, tmp_path, stub_bin):
        (tmp_path / ".venv").mkdir()
        result = run_worker(tmp_path, stub_bin)
        assert result.returncode == 0, result.stderr
        assert "UV_CALLED: run --frozen --no-sync python -c pass" in result.stdout
        assert "Runtime        : uv" in result.stdout

    def test_conda_when_conda_envs_present(self, tmp_path, stub_bin):
        (tmp_path / "conda_envs").mkdir()
        result = run_worker(tmp_path, stub_bin)
        assert result.returncode == 0, result.stderr
        assert f"CONDA_CALLED: activate {tmp_path}/conda_envs" in result.stdout
        assert "Runtime        : conda" in result.stdout

    def test_venv_wins_over_conda(self, tmp_path, stub_bin):
        (tmp_path / ".venv").mkdir()
        (tmp_path / "conda_envs").mkdir()
        result = run_worker(tmp_path, stub_bin)
        assert "UV_CALLED" in result.stdout
        assert "CONDA_CALLED" not in result.stdout

    def test_no_runtime_fails_loudly(self, tmp_path, stub_bin):
        result = run_worker(tmp_path, stub_bin)
        assert result.returncode == 1
        assert "no python runtime found" in result.stderr


class TestRemoteBase:
    def test_defaults_to_project_dir(self, tmp_path, stub_bin):
        """A job knows its own deployment even if nothing exported it."""
        (tmp_path / ".venv").mkdir()
        result = run_worker(tmp_path, stub_bin, env={"CLUSTER_REMOTE_BASE": None})
        assert result.returncode == 0, result.stderr
        assert f"ENV_CLUSTER_REMOTE_BASE={tmp_path}" in result.stdout

    def test_explicit_value_is_preserved(self, tmp_path, stub_bin):
        """The launcher's export must not be second-guessed by the worker."""
        (tmp_path / ".venv").mkdir()
        result = run_worker(
            tmp_path, stub_bin, env={"CLUSTER_REMOTE_BASE": "/explicit/base"}
        )
        assert result.returncode == 0, result.stderr
        assert "ENV_CLUSTER_REMOTE_BASE=/explicit/base" in result.stdout

    def test_job_marker_is_set(self, tmp_path, stub_bin):
        """config.py keys .env precedence off this marker."""
        (tmp_path / ".venv").mkdir()
        result = run_worker(tmp_path, stub_bin)
        assert "ENV_CLUSTER_KIT_JOB=1" in result.stdout

    def test_missing_project_dir_fails(self, tmp_path, stub_bin):
        (tmp_path / ".venv").mkdir()
        result = run_worker(tmp_path, stub_bin, env={"PROJECT_DIR": None})
        assert result.returncode != 0
        assert "PROJECT_DIR" in result.stderr


class TestTexLive:
    def test_not_applied_by_default(self, tmp_path, stub_bin):
        (tmp_path / ".venv").mkdir()
        result = run_worker(tmp_path, stub_bin)
        assert "ENV_TEXMFHOME=<unset>" in result.stdout

    def test_warns_when_root_unset(self, tmp_path, stub_bin):
        (tmp_path / ".venv").mkdir()
        result = run_worker(tmp_path, stub_bin, env={"TEXLIVE": "1"})
        assert result.returncode == 0, result.stderr
        assert "CLUSTER_TEXLIVE_ROOT is unset" in result.stderr

    def test_applied_when_configured(self, tmp_path, stub_bin):
        (tmp_path / ".venv").mkdir()
        texlive = tmp_path / "texlive"
        result = run_worker(
            tmp_path,
            stub_bin,
            env={"TEXLIVE": "1", "CLUSTER_TEXLIVE_ROOT": str(texlive)},
        )
        assert result.returncode == 0, result.stderr
        assert "CLUSTER_TEXLIVE_ROOT is unset" not in result.stderr
        assert f"ENV_TEXMFHOME={texlive}/texmf-local" in result.stdout
        # TeX Live must precede the runtime's own LaTeX on PATH.
        assert f"ENV_PATH={texlive}/bin/x86_64-linux:" in result.stdout


def test_worker_is_packaged():
    """The template must be shipped, not just present in a source checkout."""
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as fh:
        data = tomllib.load(fh)
    package_data = data["tool"]["setuptools"]["package-data"]["cluster_kit"]
    assert "launch/worker.slurm" in package_data
