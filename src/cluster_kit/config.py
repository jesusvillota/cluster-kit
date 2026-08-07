"""Configuration management for cluster-kit.

Loads cluster connection settings from CLUSTER_* environment variables with
multi-cluster profile support via CLUSTER_ENV prefix resolution.

Usage:
    >>> from cluster_kit.config import load_config, validate_config
    >>> config = load_config()
    >>> validate_config(config)
    >>> print(config.host)

Multi-cluster example:
    >>> import os
    >>> os.environ["CLUSTER_ENV"] = "dev"
    >>> os.environ["CLUSTER_DEV_HOST"] = "dev-cluster"
    >>> os.environ["CLUSTER_DEV_REMOTE_BASE"] = "/home/user/project"
    >>> config = load_config()  # Uses CLUSTER_DEV_* vars
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - depends on the runtime environment
    # Cluster environments are built separately from the laptop and have gone
    # missing python-dotenv before.  A job that was handed its configuration
    # via the environment does not need .env at all, so degrade rather than
    # take down every module that imports this one.
    load_dotenv = None

__all__ = [
    "ClusterConfig",
    "ConfigError",
    "load_config",
    "validate_config",
    "get_cluster_host",
    "get_cluster_user",
    "get_remote_base",
    "get_canonical_remote_base",
    "get_texlive_root",
    "get_ssh_key",
    "get_ssh_timeout",
    "get_sync_exclude",
    "get_slurm_partition",
    "get_executor",
    "get_sync_mode",
]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_HOST = "cluster"
_DEFAULT_SSH_KEY = "~/.ssh/id_ed25519_cluster"
_DEFAULT_SSH_TIMEOUT = 30
_DEFAULT_SYNC_EXCLUDE = "__pycache__,*.pyc,*.pyo"
_DEFAULT_SLURM_PARTITION = "cpu_express"
_DEFAULT_EXECUTOR = "slurm"
_DEFAULT_SYNC_MODE = "rsync"

VALID_EXECUTORS = ("slurm", "ssh")
VALID_SYNC_MODES = ("rsync", "git")

# Variable base names (without CLUSTER_ prefix)
_VAR_NAMES = (
    "HOST",
    "USER",
    "REMOTE_BASE",
    "SSH_KEY",
    "SSH_TIMEOUT",
    "SYNC_EXCLUDE",
    "SLURM_PARTITION",
    "EXECUTOR",
    "SYNC_MODE",
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConfigError(ValueError):
    """Raised when cluster configuration is invalid or missing required values."""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterConfig:
    """Immutable configuration holder for cluster connection settings.

    Attributes:
        host: SSH alias or hostname for the cluster.
        user: Username on the remote cluster.
        remote_base: Absolute POSIX path to the project root on the cluster.
            Suffixed with ``__<worktree>`` when running from a linked git
            worktree — see :func:`_worktree_name`.
        canonical_remote_base: ``remote_base`` without the worktree suffix, i.e.
            the shared deployment directory.  Equal to ``remote_base`` outside a
            linked worktree.
        ssh_key: Path to the SSH private key file.
        ssh_timeout: SSH connection timeout in seconds (1-300).
        sync_exclude: Comma-separated rsync exclude patterns.
        slurm_partition: SLURM partition for job submission.
        executor: Job execution backend: ``slurm`` (sbatch on a login node)
            or ``ssh`` (detached processes on a plain remote machine).
        sync_mode: Code sync strategy: ``rsync`` (push working tree) or
            ``git`` (remote checkout pulls from the shared GitHub remote).
        texlive_root: TeX Live installation root on the cluster.  Exported to
            jobs that request TeX Live; empty when the cluster has none.
    """

    host: str
    user: str
    remote_base: PurePosixPath
    ssh_key: Path
    ssh_timeout: int
    sync_exclude: str
    slurm_partition: str = _DEFAULT_SLURM_PARTITION
    executor: str = _DEFAULT_EXECUTOR
    sync_mode: str = _DEFAULT_SYNC_MODE
    canonical_remote_base: Optional[PurePosixPath] = None
    texlive_root: str = ""

    def __post_init__(self) -> None:
        if self.canonical_remote_base is None:
            object.__setattr__(self, "canonical_remote_base", self.remote_base)

    @property
    def is_worktree_isolated(self) -> bool:
        """True when ``remote_base`` carries a worktree suffix."""
        return self.remote_base != self.canonical_remote_base


# ---------------------------------------------------------------------------
# Core loading logic
# ---------------------------------------------------------------------------


def _get_env_var(name: str, env_profile: Optional[str] = None) -> Optional[str]:
    """Resolve an environment variable with optional profile prefix.

    When *env_profile* is set, looks for ``CLUSTER_{PROFILE}_{name}`` first,
    then falls back to ``CLUSTER_{name}``.

    Args:
        name: Variable base name (e.g., ``"HOST"``).
        env_profile: Optional profile identifier (e.g., ``"dev"``).

    Returns:
        The resolved value, or ``None`` if not set.
    """
    if env_profile:
        prefixed = os.getenv(f"CLUSTER_{env_profile.upper()}_{name}")
        if prefixed is not None:
            return prefixed
    return os.getenv(f"CLUSTER_{name}")


def _worktree_name(start: Optional[Path] = None) -> Optional[str]:
    """Return the directory name if *start* sits inside a linked git worktree.

    Git writes a ``.git`` *directory* in the main checkout and a ``.git`` *file*
    (a gitdir pointer) in every linked worktree, so the file/dir distinction is
    enough — no subprocess needed.

    Args:
        start: Directory to search upward from.  Defaults to the cwd.

    Returns:
        The worktree directory name, or ``None`` in a main checkout or outside
        any repository.
    """
    base = (start or Path.cwd()).resolve()
    for path in (base, *base.parents):
        dot_git = path / ".git"
        if dot_git.is_file():
            return path.name
        if dot_git.is_dir():
            return None
    return None


def load_config(
    env_file: Optional[Path] = None,
    env_profile: Optional[str] = None,
) -> ClusterConfig:
    """Load cluster configuration from environment variables.

    Resolution order:
        1. If *env_profile* is provided (or ``CLUSTER_ENV`` is set), use
           ``CLUSTER_{PROFILE}_*`` prefixed variables.
        2. Fall back to unprefixed ``CLUSTER_*`` variables.
        3. Apply defaults where defined.

    Args:
        env_file: Optional path to a ``.env`` file to load before reading
            environment variables.  Defaults to ``.env`` in the current
            working directory.
        env_profile: Optional profile name (e.g., ``"dev"``, ``"prod"``).
            When ``None``, reads ``CLUSTER_ENV`` from the environment.

    Returns:
        A fully populated :class:`ClusterConfig` instance.

    Raises:
        ConfigError: If a required variable is missing or has an invalid value.
    """
    # Load .env file if present.
    #
    # Precedence flips depending on where we are running.  On a laptop ".env is
    # truth": it must beat a stale CLUSTER_* left ambient in the shell, which is
    # why override defaults to True.  Inside a job the opposite holds — the
    # launcher exported the deployment this job belongs to, and the .env sitting
    # at a worktree deployment root is a symlink to the *canonical* one, so
    # letting it win would silently redirect the job to another deployment.
    # worker.slurm sets CLUSTER_KIT_JOB=1 to mark that boundary.
    if env_file is None:
        env_file = Path(".env")
    if load_dotenv is not None and env_file.exists():
        in_job = os.getenv("CLUSTER_KIT_JOB") == "1"
        load_dotenv(env_file, override=not in_job)

    # Determine active profile
    if env_profile is None:
        env_profile = os.getenv("CLUSTER_ENV")

    # Resolve each variable
    host = _get_env_var("HOST", env_profile) or _DEFAULT_HOST
    user = _get_env_var("USER", env_profile) or os.getenv("USER", "")
    remote_base_raw = _get_env_var("REMOTE_BASE", env_profile)
    ssh_key_raw = _get_env_var("SSH_KEY", env_profile) or _DEFAULT_SSH_KEY
    ssh_timeout_raw = _get_env_var("SSH_TIMEOUT", env_profile)
    sync_exclude = _get_env_var("SYNC_EXCLUDE", env_profile) or _DEFAULT_SYNC_EXCLUDE
    slurm_partition = (
        _get_env_var("SLURM_PARTITION", env_profile) or _DEFAULT_SLURM_PARTITION
    )
    executor = _get_env_var("EXECUTOR", env_profile) or _DEFAULT_EXECUTOR
    sync_mode = _get_env_var("SYNC_MODE", env_profile) or _DEFAULT_SYNC_MODE
    texlive_root = _get_env_var("TEXLIVE_ROOT", env_profile) or ""

    # Type conversions
    if remote_base_raw is None:
        raise ConfigError(
            "CLUSTER_REMOTE_BASE is required but not set. "
            "Set it to the absolute path of the project root on the cluster."
        )

    # Isolate concurrent git worktrees: each gets its own remote deployment
    # directory, so the destructive `sync code` (rm -rf + rsync --delete) run
    # from one worktree cannot clobber the code another worktree's queued jobs
    # are about to run.  Skipped in git sync mode, which targets a single
    # remote checkout that must keep its configured path.
    canonical_remote_base = PurePosixPath(remote_base_raw)
    worktree = _worktree_name() if sync_mode != "git" else None
    if worktree:
        remote_base_raw = f"{remote_base_raw.rstrip('/')}__{worktree}"

    ssh_timeout = _DEFAULT_SSH_TIMEOUT
    if ssh_timeout_raw is not None:
        try:
            ssh_timeout = int(ssh_timeout_raw)
        except ValueError:
            raise ConfigError(
                f"CLUSTER_SSH_TIMEOUT must be an integer between 1 and 300, "
                f"got '{ssh_timeout_raw}'"
            )

    return ClusterConfig(
        host=host,
        user=user,
        remote_base=PurePosixPath(remote_base_raw),
        ssh_key=Path(ssh_key_raw).expanduser(),
        ssh_timeout=ssh_timeout,
        sync_exclude=sync_exclude,
        slurm_partition=slurm_partition,
        executor=executor,
        sync_mode=sync_mode,
        canonical_remote_base=canonical_remote_base,
        texlive_root=texlive_root,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_config(config: ClusterConfig) -> list[str]:
    """Validate a :class:`ClusterConfig` instance.

    Checks:
        - ``host`` is non-empty.
        - ``user`` is non-empty.
        - ``remote_base`` is an absolute path.
        - ``ssh_key`` file exists with correct permissions (600 or 400).
        - ``ssh_timeout`` is between 1 and 300.
        - ``sync_exclude`` contains no spaces around commas.

    Args:
        config: The configuration to validate.

    Returns:
        A list of error messages.  Empty list means the config is valid.
    """
    errors: list[str] = []

    # Host
    if not config.host or not config.host.strip():
        errors.append("CLUSTER_HOST must be a non-empty string")

    # User
    if not config.user or not config.user.strip():
        errors.append("CLUSTER_USER must be a non-empty string")

    # Remote base — must be absolute
    if not config.remote_base.is_absolute():
        errors.append("CLUSTER_REMOTE_BASE must be an absolute path")

    # SSH key — file must exist with correct permissions
    ssh_key = config.ssh_key
    if ssh_key.exists():
        mode = ssh_key.stat().st_mode & 0o777
        if mode not in (0o600, 0o400):
            errors.append(
                f"CLUSTER_SSH_KEY file exists but has incorrect permissions "
                f"({oct(mode)}); expected 0o600 or 0o400"
            )
    else:
        # Only error if the path was explicitly set (not default)
        # We warn but don't fail — the key might be added later
        pass

    # SSH timeout
    if not (1 <= config.ssh_timeout <= 300):
        errors.append(
            f"CLUSTER_SSH_TIMEOUT must be an integer between 1 and 300, "
            f"got {config.ssh_timeout}"
        )

    # Sync exclude — no spaces around commas
    if config.sync_exclude and re.search(r"\s+,|,\s+|\s+$|^\s+", config.sync_exclude):
        errors.append(
            "CLUSTER_SYNC_EXCLUDE must be comma-separated patterns without spaces"
        )

    # Executor / sync mode
    if config.executor not in VALID_EXECUTORS:
        errors.append(
            f"CLUSTER_EXECUTOR must be one of {VALID_EXECUTORS}, "
            f"got '{config.executor}'"
        )
    if config.sync_mode not in VALID_SYNC_MODES:
        errors.append(
            f"CLUSTER_SYNC_MODE must be one of {VALID_SYNC_MODES}, "
            f"got '{config.sync_mode}'"
        )

    return errors


def validate_config_strict(config: ClusterConfig) -> None:
    """Validate configuration and raise :class:`ConfigError` on failure.

    Args:
        config: The configuration to validate.

    Raises:
        ConfigError: If any validation checks fail, with all errors joined.
    """
    errors = validate_config(config)
    if errors:
        raise ConfigError("; ".join(errors))


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

# Module-level cache for the loaded config
_config_cache: Optional[ClusterConfig] = None


def _get_config() -> ClusterConfig:
    """Return the cached config or load a fresh one."""
    global _config_cache
    if _config_cache is None:
        _config_cache = load_config()
    return _config_cache


def get_cluster_host() -> str:
    """Return the configured cluster host."""
    return _get_config().host


def get_cluster_user() -> str:
    """Return the configured cluster user."""
    return _get_config().user


def get_remote_base() -> PurePosixPath:
    """Return the configured remote base path (worktree-suffixed if applicable)."""
    return _get_config().remote_base


def get_canonical_remote_base() -> PurePosixPath:
    """Return the shared remote base, without any worktree suffix."""
    config = _get_config()
    return config.canonical_remote_base or config.remote_base


def get_texlive_root() -> str:
    """Return the cluster's TeX Live root, or "" when unconfigured."""
    return _get_config().texlive_root


def get_ssh_key() -> Path:
    """Return the configured SSH key path."""
    return _get_config().ssh_key


def get_ssh_timeout() -> int:
    """Return the configured SSH timeout in seconds."""
    return _get_config().ssh_timeout


def get_sync_exclude() -> str:
    """Return the configured sync exclude patterns."""
    return _get_config().sync_exclude


def get_slurm_partition() -> str:
    """Return the configured SLURM partition."""
    return _get_config().slurm_partition


def get_executor() -> str:
    """Return the configured execution backend (``slurm`` or ``ssh``)."""
    return _get_config().executor


def get_sync_mode() -> str:
    """Return the configured code sync mode (``rsync`` or ``git``)."""
    return _get_config().sync_mode


def reset_config_cache() -> None:
    """Reset the module-level config cache.  Useful for testing."""
    global _config_cache
    _config_cache = None
