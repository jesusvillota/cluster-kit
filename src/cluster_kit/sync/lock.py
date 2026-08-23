"""Remote deployment lock shared by code sync and workflow launch."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import PurePosixPath
from types import TracebackType

from rich.console import Console

_console = Console()


class RemoteDeployLockError(RuntimeError):
    """Raised when the remote deploy lock cannot be acquired."""


def deploy_lock_path(remote_base: str | PurePosixPath) -> str:
    """Return the per-remote-base deploy lock path."""
    return f"{remote_base}/.cluster_kit/deploy.lock"


class RemoteDeployLock:
    """Hold a remote flock for the lifetime of this context manager.

    The remote process owns the lock fd. If the local process dies, SSH closes,
    the remote process exits, and the kernel releases the lock.
    """

    def __init__(
        self,
        *,
        host: str,
        remote_base: str | PurePosixPath,
        purpose: str,
        timeout: int = 30,
    ) -> None:
        self.host = host
        self.remote_base = str(remote_base)
        self.lock_path = deploy_lock_path(self.remote_base)
        self.purpose = purpose
        self.timeout = timeout
        self._proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "RemoteDeployLock":
        _console.print(
            f"[cyan]Waiting for deploy lock[/cyan] {self.lock_path} "
            f"[dim]({self.purpose})[/dim]"
        )
        command = _remote_lock_command(self.lock_path)
        try:
            proc = subprocess.Popen(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    f"ConnectTimeout={self.timeout}",
                    self.host,
                    command,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.stdout is not None
            ready = proc.stdout.readline().strip()
        except Exception as exc:
            raise RemoteDeployLockError(
                f"could not acquire deploy lock for {self.remote_base}: {exc}"
            ) from exc

        if ready != "cluster-kit-deploy-lock-acquired":
            stderr = ""
            try:
                stderr = proc.stderr.read() if proc.stderr is not None else ""
            finally:
                proc.kill()
                proc.wait(timeout=5)
            raise RemoteDeployLockError(
                "could not acquire deploy lock for "
                f"{self.remote_base}: {stderr.strip() or 'remote lock failed'}"
            )

        self._proc = proc
        _console.print(f"[green][OK][/green] Deploy lock acquired: {self.lock_path}")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        _console.print(f"[green][OK][/green] Deploy lock released: {self.lock_path}")


def _remote_lock_command(lock_path: str) -> str:
    quoted_lock = shlex.quote(lock_path)
    script = f"""
import fcntl
import os
import sys

lock_path = {lock_path!r}
os.makedirs(os.path.dirname(lock_path), exist_ok=True)
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    print("cluster-kit-deploy-lock-acquired", flush=True)
    sys.stdin.read()
finally:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
"""
    return f"python3 -c {shlex.quote(script)} # {quoted_lock}"
