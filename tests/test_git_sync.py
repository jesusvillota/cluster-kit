"""Tests for git-based code sync (preflight matrix + remote command)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cluster_kit.config import ClusterConfig
from cluster_kit.sync.git_sync import GitSyncer

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _config() -> ClusterConfig:
    return ClusterConfig(
        host="pc",
        user="wsluser",
        remote_base=PurePosixPath("/home/wsluser/GitHub/project"),
        ssh_key=Path("~/.ssh/id_ed25519_pc"),
        ssh_timeout=10,
        sync_exclude="__pycache__",
        executor="ssh",
        sync_mode="git",
    )


class FakeGit:
    """Simulated local git repo state."""

    def __init__(
        self,
        *,
        branch: str = "main",
        upstream: bool = True,
        dirty: list[str] | None = None,
        unpushed: int = 0,
        head: str = "a" * 40,
    ) -> None:
        self.branch = branch
        self.upstream = upstream
        self.dirty = dirty or []
        self.unpushed = unpushed
        self.head = head
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        ok = lambda out: SimpleNamespace(returncode=0, stdout=out, stderr="")  # noqa: E731
        fail = SimpleNamespace(returncode=1, stdout="", stderr="error")
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            if args[2] == "HEAD":
                return ok(self.branch + "\n")
            return ok(f"origin/{self.branch}\n") if self.upstream else fail
        if args[0] == "status":
            return ok("\n".join(self.dirty))
        if args[:2] == ("rev-list", "--count"):
            return ok(f"{self.unpushed}\n")
        if args == ("rev-parse", "HEAD"):
            return ok(self.head + "\n")
        if args[0] in ("add", "commit", "push"):
            return ok("")
        raise AssertionError(f"unexpected git call: {args}")


class FakeRemote:
    def __init__(self, *, fail: bool = False, head: str = "a" * 40) -> None:
        self.fail = fail
        self.head = head
        self.commands: list[str] = []

    def __call__(self, command: str):
        self.commands.append(command)
        if "rev-parse HEAD" in command:
            return SimpleNamespace(returncode=0, stdout=self.head + "\n", stderr="")
        if "git status" in command:
            return SimpleNamespace(returncode=0, stdout=" M file.py", stderr="")
        if self.fail:
            return SimpleNamespace(
                returncode=1, stdout="", stderr="fatal: Not possible to fast-forward"
            )
        return SimpleNamespace(returncode=0, stdout="Updating...", stderr="")


class FakeDeployLock:
    calls: list[tuple[str, str]] = []

    def __init__(self, *, host: str, remote_base: str, purpose: str):
        self.host = host
        self.remote_base = str(remote_base)
        self.purpose = purpose

    def __enter__(self):
        self.calls.append(("enter", self.remote_base))
        return self

    def __exit__(self, exc_type, exc, tb):
        self.calls.append(("exit", self.remote_base))


def _syncer(
    git: FakeGit,
    remote: FakeRemote | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> GitSyncer:
    syncer = GitSyncer(
        config=_config(),
        project_root=Path("/local/project"),
        dry_run=dry_run,
        force=force,
    )
    syncer._git = git  # type: ignore[method-assign]
    if remote is not None:
        syncer._remote = remote  # type: ignore[method-assign]
    return syncer


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_clean_repo_passes(self):
        assert _syncer(FakeGit()).preflight() == "main"

    def test_detached_head_fails(self):
        from cluster_kit.sync.git_sync import GitSyncError

        with pytest.raises(GitSyncError, match="detached HEAD"):
            _syncer(FakeGit(branch="HEAD")).preflight()

    def test_missing_upstream_fails(self):
        from cluster_kit.sync.git_sync import GitSyncError

        with pytest.raises(GitSyncError, match="no upstream"):
            _syncer(FakeGit(upstream=False)).preflight()

    def test_dirty_tree_fails_non_interactive(self):
        from cluster_kit.sync.git_sync import GitSyncError

        with (
            patch("cluster_kit.sync.git_sync._is_interactive", return_value=False),
            pytest.raises(GitSyncError, match="uncommitted changes"),
        ):
            _syncer(FakeGit(dirty=[" M src/a.py"])).preflight()

    def test_unpushed_commits_fail_non_interactive(self):
        from cluster_kit.sync.git_sync import GitSyncError

        with (
            patch("cluster_kit.sync.git_sync._is_interactive", return_value=False),
            pytest.raises(GitSyncError, match="unpushed commit"),
        ):
            _syncer(FakeGit(unpushed=2)).preflight()

    def test_dirty_tree_interactive_commit_and_push(self):
        git = FakeGit(dirty=[" M src/a.py"])
        with (
            patch("cluster_kit.sync.git_sync._is_interactive", return_value=True),
            patch(
                "cluster_kit.sync.git_sync.Prompt.ask",
                side_effect=["yes", "fix things"],
            ),
        ):
            assert _syncer(git).preflight() == "main"
        assert ("add", "-A") in git.calls
        assert ("commit", "-m", "fix things") in git.calls
        assert ("push",) in git.calls

    def test_dirty_tree_interactive_decline_aborts(self):
        from cluster_kit.sync.git_sync import GitSyncError

        with (
            patch("cluster_kit.sync.git_sync._is_interactive", return_value=True),
            patch("cluster_kit.sync.git_sync.Prompt.ask", return_value="no"),
            pytest.raises(GitSyncError, match="aborted"),
        ):
            _syncer(FakeGit(dirty=[" M src/a.py"])).preflight()

    def test_dry_run_reports_without_prompting(self):
        # No Prompt patching: a prompt would raise in the test environment.
        git = FakeGit(dirty=[" M src/a.py"], unpushed=1)
        assert _syncer(git, dry_run=True).preflight() == "main"


# ---------------------------------------------------------------------------
# Remote update
# ---------------------------------------------------------------------------


class TestRemoteUpdate:
    def test_remote_command_ff_only(self):
        syncer = _syncer(FakeGit())
        command = syncer._remote_update_command("main")
        assert command == (
            "cd /home/wsluser/GitHub/project && git fetch origin && "
            "git checkout main && git pull --ff-only origin main"
        )

    def test_remote_command_force_resets(self):
        syncer = _syncer(FakeGit(), force=True)
        command = syncer._remote_update_command("main")
        assert "git reset --hard origin/main" in command
        assert "--ff-only" not in command

    def test_sync_success_verifies_head_match(self):
        remote = FakeRemote()
        syncer = _syncer(FakeGit(), remote)
        with (
            patch("cluster_kit.sync.git_sync.ensure_reachable"),
            patch("cluster_kit.sync.git_sync.RemoteDeployLock", FakeDeployLock),
        ):
            FakeDeployLock.calls = []
            assert syncer.sync() is True
        assert any("git pull --ff-only" in cmd for cmd in remote.commands)
        assert FakeDeployLock.calls == [
            ("enter", "/home/wsluser/GitHub/project"),
            ("exit", "/home/wsluser/GitHub/project"),
        ]

    def test_sync_fails_on_diverged_remote(self):
        remote = FakeRemote(fail=True)
        syncer = _syncer(FakeGit(), remote)
        with (
            patch("cluster_kit.sync.git_sync.ensure_reachable"),
            patch("cluster_kit.sync.git_sync.RemoteDeployLock", FakeDeployLock),
        ):
            assert syncer.sync() is False
        # Failure path inspects the remote checkout state.
        assert any("git status" in cmd for cmd in remote.commands)

    def test_sync_fails_on_head_mismatch(self):
        remote = FakeRemote(head="b" * 40)
        syncer = _syncer(FakeGit(head="a" * 40), remote)
        with (
            patch("cluster_kit.sync.git_sync.ensure_reachable"),
            patch("cluster_kit.sync.git_sync.RemoteDeployLock", FakeDeployLock),
        ):
            assert syncer.sync() is False

    def test_dry_run_skips_remote_calls(self):
        remote = FakeRemote()
        syncer = _syncer(FakeGit(), remote, dry_run=True)
        assert syncer.sync() is True
        assert remote.commands == []

    def test_unreachable_host_fails_with_hint(self):
        from cluster_kit.utils.ssh import RemoteUnreachableError

        remote = FakeRemote()
        syncer = _syncer(FakeGit(), remote)
        with patch(
            "cluster_kit.sync.git_sync.ensure_reachable",
            side_effect=RemoteUnreachableError("Host 'pc' unreachable"),
        ), patch("cluster_kit.sync.git_sync.RemoteDeployLock") as lock:
            assert syncer.sync() is False
        lock.assert_not_called()
        assert remote.commands == []

    def test_lock_failure_aborts_before_remote_update(self):
        from cluster_kit.sync.lock import RemoteDeployLockError

        remote = FakeRemote()
        syncer = _syncer(FakeGit(), remote)
        with (
            patch("cluster_kit.sync.git_sync.ensure_reachable"),
            patch(
                "cluster_kit.sync.git_sync.RemoteDeployLock",
                side_effect=RemoteDeployLockError("busy"),
            ),
        ):
            assert syncer.sync() is False
        assert remote.commands == []
