"""Git-based code synchronization for ssh-executor profiles.

Instead of pushing the working tree with rsync, the remote machine holds a
normal GitHub clone of the project. Syncing means:

1. Local preflight: the working tree must be clean and the current branch
   pushed. Interactively, the user is offered a commit+push; otherwise the
   sync fails with a clear message.
2. Remote update: ``git fetch`` + ``git checkout <local branch>`` +
   ``git pull --ff-only``. The remote checkout is never reset implicitly;
   ``force=True`` performs an explicit ``git reset --hard origin/<branch>``.
3. Verification: local and remote HEAD shas must match.

Example:
    >>> from cluster_kit.sync.git_sync import GitSyncer
    >>> GitSyncer(dry_run=True).sync()

CLI:
    $ cluster-kit -p pc sync code
    $ cluster-kit -p pc sync code --force
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.prompt import Prompt

from cluster_kit.config import ClusterConfig, load_config
from cluster_kit.utils.ssh import (
    RemoteUnreachableError,
    ensure_reachable,
    run_remote,
)

_console = Console()


class GitSyncError(RuntimeError):
    """Raised when git-based code sync fails."""


def _find_project_root(start_path: Optional[Path] = None) -> Path:
    """Find the project root by searching upward for a .git directory."""
    current = (start_path or Path.cwd()).resolve()
    for path in [current] + list(current.parents):
        if (path / ".git").is_dir():
            return path
    raise GitSyncError(f"no git repository found above {current}")


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


class GitSyncer:
    """Bring the remote git clone up to date with the pushed local branch."""

    def __init__(
        self,
        *,
        config: Optional[ClusterConfig] = None,
        project_root: Optional[Path] = None,
        dry_run: bool = False,
        force: bool = False,
        verbose: bool = False,
    ):
        self.config = config or load_config()
        self.project_root = project_root or _find_project_root()
        self.dry_run = dry_run
        self.force = force
        self.verbose = verbose

    # -- subprocess boundaries (monkeypatched in tests) ---------------------

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

    def _remote(self, command: str) -> subprocess.CompletedProcess:
        return run_remote(command, config=self.config, timeout=120)

    # -- local preflight -----------------------------------------------------

    def _current_branch(self) -> str:
        result = self._git("rev-parse", "--abbrev-ref", "HEAD")
        branch = result.stdout.strip()
        if result.returncode != 0 or not branch or branch == "HEAD":
            raise GitSyncError(
                "detached HEAD (or not a git repo); check out a branch with an "
                "upstream before syncing"
            )
        return branch

    def _require_upstream(self, branch: str) -> None:
        result = self._git("rev-parse", "--abbrev-ref", f"{branch}@{{u}}")
        if result.returncode != 0:
            raise GitSyncError(
                f"branch '{branch}' has no upstream; push it first: "
                f"git push -u origin {branch}"
            )

    def _dirty_files(self) -> list[str]:
        result = self._git("status", "--porcelain")
        return [line for line in result.stdout.splitlines() if line.strip()]

    def _unpushed_count(self, branch: str) -> int:
        result = self._git("rev-list", "--count", f"{branch}@{{u}}..HEAD")
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip() or 0)

    def _offer_commit_and_push(self, dirty: list[str]) -> None:
        listing = "\n".join(f"  {line}" for line in dirty[:20])
        if len(dirty) > 20:
            listing += f"\n  ... and {len(dirty) - 20} more"
        if not _is_interactive():
            raise GitSyncError(
                "uncommitted changes won't reach the remote clone "
                f"(git sync mode):\n{listing}\ncommit and push, then retry"
            )
        _console.print(
            f"[yellow]Uncommitted changes won't reach the remote clone:"
            f"[/yellow]\n{listing}"
        )
        answer = Prompt.ask("[cyan]Commit & push now?[/cyan]", choices=["yes", "no"])
        if answer != "yes":
            raise GitSyncError("sync aborted: uncommitted changes")
        message = Prompt.ask("[cyan]Commit message[/cyan]").strip()
        if not message:
            raise GitSyncError("sync aborted: empty commit message")
        for args in (("add", "-A"), ("commit", "-m", message), ("push",)):
            result = self._git(*args)
            if result.returncode != 0:
                raise GitSyncError(
                    f"git {' '.join(args)} failed: {result.stderr.strip()}"
                )
        _console.print("[green][OK][/green] Committed and pushed")

    def _offer_push(self, branch: str, count: int) -> None:
        if not _is_interactive():
            raise GitSyncError(
                f"{count} unpushed commit(s) on '{branch}' won't reach the "
                "remote clone; push first"
            )
        answer = Prompt.ask(
            f"[cyan]{count} unpushed commit(s) on '{branch}'. Push now?[/cyan]",
            choices=["yes", "no"],
        )
        if answer != "yes":
            raise GitSyncError("sync aborted: unpushed commits")
        result = self._git("push")
        if result.returncode != 0:
            raise GitSyncError(f"git push failed: {result.stderr.strip()}")
        _console.print("[green][OK][/green] Pushed")

    def preflight(self) -> str:
        """Validate the local repo state; returns the branch to sync."""
        branch = self._current_branch()
        self._require_upstream(branch)
        dirty = self._dirty_files()
        if dirty:
            if self.dry_run:
                _console.print(
                    f"[yellow]DRY[/yellow] {len(dirty)} uncommitted change(s) "
                    "would block the sync (or trigger a commit+push prompt)"
                )
            else:
                self._offer_commit_and_push(dirty)
        unpushed = self._unpushed_count(branch)
        if unpushed:
            if self.dry_run:
                _console.print(
                    f"[yellow]DRY[/yellow] {unpushed} unpushed commit(s) "
                    "would block the sync (or trigger a push prompt)"
                )
            else:
                self._offer_push(branch, unpushed)
        return branch

    # -- remote update ---------------------------------------------------------

    def _remote_update_command(self, branch: str) -> str:
        base = shlex.quote(str(self.config.remote_base))
        quoted_branch = shlex.quote(branch)
        if self.force:
            update = (
                f"git checkout {quoted_branch} && "
                f"git reset --hard origin/{quoted_branch}"
            )
        else:
            update = (
                f"git checkout {quoted_branch} && "
                f"git pull --ff-only origin {quoted_branch}"
            )
        return f"cd {base} && git fetch origin && {update}"

    def sync(self) -> bool:
        """Run the full git sync; returns True on success."""
        _console.print(
            f"[cyan]Git sync:[/cyan] {self.project_root} -> "
            f"{self.config.host}:{self.config.remote_base}"
        )
        try:
            branch = self.preflight()
        except GitSyncError as exc:
            _console.print(f"[red][FAIL][/red] {exc}")
            return False

        command = self._remote_update_command(branch)
        if self.dry_run:
            _console.print(
                f"[yellow]DRY[/yellow] would run: ssh {self.config.host} {command!r}"
            )
            return True

        try:
            ensure_reachable(config=self.config)
        except RemoteUnreachableError as exc:
            _console.print(f"[red][FAIL][/red] {exc}")
            return False

        result = self._remote(command)
        if result.returncode != 0:
            base = shlex.quote(str(self.config.remote_base))
            status = self._remote(f"cd {base} && git status --short --branch")
            _console.print(
                f"[red][FAIL][/red] remote update failed:\n"
                f"{result.stderr.strip()}\n"
                f"[yellow]Remote checkout state:[/yellow]\n"
                f"{status.stdout.strip()}\n"
                "Resolve on the remote machine, or rerun with --force to "
                f"reset it to origin/{branch} (uncommitted remote work is "
                "lost; untracked files survive)."
            )
            return False
        if self.verbose and result.stdout.strip():
            _console.print(result.stdout.strip())

        local_sha = self._git("rev-parse", "HEAD").stdout.strip()
        base = shlex.quote(str(self.config.remote_base))
        remote_sha = self._remote(f"cd {base} && git rev-parse HEAD").stdout.strip()
        if local_sha and local_sha == remote_sha:
            _console.print(
                f"[green][OK][/green] Remote clone at [bold]{branch}[/bold] "
                f"@ {local_sha[:10]}"
            )
            return True
        _console.print(
            f"[red][FAIL][/red] HEAD mismatch after sync: "
            f"local {local_sha[:10]} vs remote {remote_sha[:10] or '?'} — "
            "did the local branch move past its pushed upstream?"
        )
        return False
