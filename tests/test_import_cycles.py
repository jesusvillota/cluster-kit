"""Regression tests for circular imports across cluster-kit modules.

These tests spawn fresh Python subprocesses to ensure clean sys.modules
state and verify that importing individual submodules in any order does not
trigger circular import / partially initialized module errors.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

IMPORT_STATEMENTS = [
    "from cluster_kit.launch.launcher import PARTITION_DEFAULTS",
    "from cluster_kit.launch.launcher import add_launcher_args, maybe_launch",
    "from cluster_kit.launch import get_worker_template",
    "from cluster_kit.sync.code import CodeDeployer, sync_code",
    "from cluster_kit.sync.lock import deploy_lock_path, RemoteDeployLock",
    "from cluster_kit.cli import _cmd_resources",
    "import cluster_kit.launch",
    "import cluster_kit.launch.launcher",
    "import cluster_kit.sync",
    "import cluster_kit.sync.code",
    "import cluster_kit.sync.lock",
    "import cluster_kit.cli",
]


@pytest.mark.parametrize("stmt", IMPORT_STATEMENTS)
def test_clean_import_in_isolated_process(stmt: str) -> None:
    """Ensure import statement executes cleanly in a fresh Python process."""
    repo_root = Path(__file__).resolve().parents[1] / "src"
    cmd = [sys.executable, "-c", stmt]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    assert result.returncode == 0, (
        f"Import failed for '{stmt}':\n"
        f"STDOUT: {result.stdout}\n"
        f"STDERR: {result.stderr}"
    )
