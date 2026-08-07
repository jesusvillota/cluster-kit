"""Tests for cluster_kit.config module."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import pytest

from cluster_kit.config import (
    ClusterConfig,
    ConfigError,
    _get_env_var,
    _worktree_name,
    load_config,
    reset_config_cache,
    validate_config,
    validate_config_strict,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_env():
    """Clear all CLUSTER_* env vars before and after each test."""
    keys_to_clear = [
        k for k in os.environ if k.startswith("CLUSTER_")
    ]
    saved = {k: os.environ.pop(k) for k in keys_to_clear}
    yield
    for k in keys_to_clear:
        os.environ.pop(k, None)
    for k, v in saved.items():
        os.environ[k] = v
    reset_config_cache()


@pytest.fixture
def minimal_env():
    """Set only the required CLUSTER_REMOTE_BASE."""
    os.environ["CLUSTER_REMOTE_BASE"] = "/mnt/slurm-beegfs/Users/test/project"
    yield


# ---------------------------------------------------------------------------
# _get_env_var
# ---------------------------------------------------------------------------


class TestGetEnvVar:
    def test_unprefixed_fallback(self):
        os.environ["CLUSTER_HOST"] = "my-cluster"
        assert _get_env_var("HOST") == "my-cluster"

    def test_profile_takes_precedence(self):
        os.environ["CLUSTER_HOST"] = "default-cluster"
        os.environ["CLUSTER_DEV_HOST"] = "dev-cluster"
        assert _get_env_var("HOST", env_profile="dev") == "dev-cluster"

    def test_profile_falls_back_when_not_set(self):
        os.environ["CLUSTER_HOST"] = "default-cluster"
        assert _get_env_var("HOST", env_profile="dev") == "default-cluster"

    def test_returns_none_when_unset(self):
        assert _get_env_var("HOST") is None


# ---------------------------------------------------------------------------
# load_config — defaults
# ---------------------------------------------------------------------------


class TestLoadConfigDefaults:
    def test_defaults_applied(self, minimal_env):
        cfg = load_config()
        assert cfg.host == "cluster"
        assert cfg.ssh_key == Path("~/.ssh/id_ed25519_cluster").expanduser()
        assert cfg.ssh_timeout == 30
        assert cfg.sync_exclude == "__pycache__,*.pyc,*.pyo"

    def test_user_from_os(self, minimal_env):
        with patch.dict(os.environ, {"USER": "testuser"}):
            cfg = load_config()
            assert cfg.user == "testuser"


# ---------------------------------------------------------------------------
# load_config — env var overrides
# ---------------------------------------------------------------------------


class TestLoadConfigEnvVars:
    def test_all_vars_override(self, clean_env):
        os.environ["CLUSTER_HOST"] = "prod-cluster"
        os.environ["CLUSTER_USER"] = "produser"
        os.environ["CLUSTER_REMOTE_BASE"] = "/opt/project"
        os.environ["CLUSTER_SSH_KEY"] = "/tmp/test_key"
        os.environ["CLUSTER_SSH_TIMEOUT"] = "60"
        os.environ["CLUSTER_SYNC_EXCLUDE"] = ".git,node_modules"

        cfg = load_config(env_file=Path("/nonexistent/.env"))
        assert cfg.host == "prod-cluster"
        assert cfg.user == "produser"
        assert cfg.remote_base == PurePosixPath("/opt/project")
        assert cfg.ssh_key == Path("/tmp/test_key")
        assert cfg.ssh_timeout == 60
        assert cfg.sync_exclude == ".git,node_modules"

    def test_remote_base_required(self, clean_env):
        with pytest.raises(ConfigError, match="CLUSTER_REMOTE_BASE is required"):
            load_config(env_file=Path("/nonexistent/.env"))

    def test_invalid_timeout_raises(self, clean_env):
        os.environ["CLUSTER_REMOTE_BASE"] = "/tmp"
        os.environ["CLUSTER_SSH_TIMEOUT"] = "not-a-number"
        with pytest.raises(ConfigError, match="must be an integer"):
            load_config()


# ---------------------------------------------------------------------------
# load_config — multi-cluster profiles
# ---------------------------------------------------------------------------


class TestMultiClusterProfiles:
    def test_profile_via_env_profile_arg(self, clean_env):
        os.environ["CLUSTER_REMOTE_BASE"] = "/default/path"
        os.environ["CLUSTER_DEV_REMOTE_BASE"] = "/dev/path"
        os.environ["CLUSTER_DEV_HOST"] = "dev-host"

        cfg = load_config(env_profile="dev")
        assert cfg.remote_base == PurePosixPath("/dev/path")
        assert cfg.host == "dev-host"

    def test_profile_via_cluster_env(self, clean_env):
        os.environ["CLUSTER_REMOTE_BASE"] = "/default/path"
        os.environ["CLUSTER_ENV"] = "staging"
        os.environ["CLUSTER_STAGING_REMOTE_BASE"] = "/staging/path"
        os.environ["CLUSTER_STAGING_HOST"] = "staging-host"

        cfg = load_config()
        assert cfg.remote_base == PurePosixPath("/staging/path")
        assert cfg.host == "staging-host"

    def test_profile_partial_override(self, clean_env):
        os.environ["CLUSTER_REMOTE_BASE"] = "/default/path"
        os.environ["CLUSTER_HOST"] = "default-host"
        os.environ["CLUSTER_DEV_REMOTE_BASE"] = "/dev/path"
        # CLUSTER_DEV_HOST not set — should fall back to default

        cfg = load_config(env_profile="dev")
        assert cfg.remote_base == PurePosixPath("/dev/path")
        assert cfg.host == "default-host"


# ---------------------------------------------------------------------------
# load_config — env_file
# ---------------------------------------------------------------------------


class TestLoadConfigEnvFile:
    def test_env_file_loading(self, tmp_path, clean_env):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "CLUSTER_REMOTE_BASE=/from/env/file\n"
            "CLUSTER_HOST=env-file-host\n"
        )
        cfg = load_config(env_file=env_file)
        assert cfg.remote_base == PurePosixPath("/from/env/file")
        assert cfg.host == "env-file-host"

    def test_env_file_not_found_uses_env(self, minimal_env):
        cfg = load_config(env_file=Path("/nonexistent/.env"))
        assert cfg.remote_base == PurePosixPath("/mnt/slurm-beegfs/Users/test/project")


# ---------------------------------------------------------------------------
# Worktree isolation
# ---------------------------------------------------------------------------


class TestWorktreeName:
    def test_linked_worktree_returns_dir_name(self, tmp_path):
        wt = tmp_path / "my-feature"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: /repo/.git/worktrees/my-feature\n")
        assert _worktree_name(wt) == "my-feature"

    def test_main_checkout_returns_none(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        assert _worktree_name(repo) is None

    def test_nested_dir_finds_enclosing_worktree(self, tmp_path):
        wt = tmp_path / "my-feature"
        nested = wt / "src" / "pkg"
        nested.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: /repo/.git/worktrees/my-feature\n")
        assert _worktree_name(nested) == "my-feature"

    def test_outside_repo_returns_none(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert _worktree_name(plain) is None


class TestWorktreeIsolation:
    @staticmethod
    def _worktree(tmp_path, name="price-pressure-extension"):
        wt = tmp_path / name
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: /repo/.git/worktrees/{name}\n")
        return wt

    def test_remote_base_suffixed_in_worktree(
        self, tmp_path, monkeypatch, minimal_env
    ):
        monkeypatch.chdir(self._worktree(tmp_path))
        cfg = load_config()
        assert cfg.remote_base == PurePosixPath(
            "/mnt/slurm-beegfs/Users/test/project__price-pressure-extension"
        )
        assert cfg.canonical_remote_base == PurePosixPath(
            "/mnt/slurm-beegfs/Users/test/project"
        )
        assert cfg.is_worktree_isolated

    def test_main_checkout_unchanged(self, tmp_path, monkeypatch, minimal_env):
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.chdir(repo)
        cfg = load_config()
        assert cfg.remote_base == PurePosixPath("/mnt/slurm-beegfs/Users/test/project")
        assert cfg.canonical_remote_base == cfg.remote_base
        assert not cfg.is_worktree_isolated

    def test_git_sync_mode_not_suffixed(self, tmp_path, monkeypatch, minimal_env):
        # A git-mode remote is a single checkout at a fixed path — suffixing it
        # would point at a directory that does not exist.
        monkeypatch.chdir(self._worktree(tmp_path))
        os.environ["CLUSTER_SYNC_MODE"] = "git"
        cfg = load_config()
        assert cfg.remote_base == PurePosixPath("/mnt/slurm-beegfs/Users/test/project")
        assert not cfg.is_worktree_isolated

    def test_trailing_slash_stripped(self, tmp_path, monkeypatch, clean_env):
        os.environ["CLUSTER_REMOTE_BASE"] = "/mnt/project/"
        monkeypatch.chdir(self._worktree(tmp_path, "wt"))
        assert load_config().remote_base == PurePosixPath("/mnt/project__wt")


# ---------------------------------------------------------------------------
# .env precedence (laptop vs inside a job)
# ---------------------------------------------------------------------------


class TestDotenvPrecedence:
    """`.env` is truth on a laptop; the job environment is truth in a job.

    Inside a worktree job the `.env` at the deployment root is a symlink to the
    canonical one, so letting it win would silently redirect the job to another
    worktree's deployment.
    """

    @staticmethod
    def _env_file(tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("CLUSTER_REMOTE_BASE=/from/dotenv\n")
        return env_file

    def test_dotenv_wins_on_laptop(self, tmp_path, clean_env):
        os.environ["CLUSTER_REMOTE_BASE"] = "/from/shell"
        cfg = load_config(env_file=self._env_file(tmp_path))
        assert cfg.remote_base == PurePosixPath("/from/dotenv")

    def test_environment_wins_inside_job(self, tmp_path, clean_env):
        os.environ["CLUSTER_KIT_JOB"] = "1"
        os.environ["CLUSTER_REMOTE_BASE"] = "/deployment/for/this/job"
        cfg = load_config(env_file=self._env_file(tmp_path))
        assert cfg.remote_base == PurePosixPath("/deployment/for/this/job")

    def test_dotenv_still_fills_unset_vars_inside_job(self, tmp_path, clean_env):
        """Only deliberate exports are protected; .env still supplies the rest."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "CLUSTER_REMOTE_BASE=/from/dotenv\nCLUSTER_HOST=dotenv-host\n"
        )
        os.environ["CLUSTER_KIT_JOB"] = "1"
        os.environ["CLUSTER_REMOTE_BASE"] = "/deployment/for/this/job"
        cfg = load_config(env_file=env_file)
        assert cfg.remote_base == PurePosixPath("/deployment/for/this/job")
        assert cfg.host == "dotenv-host"


class TestDotenvOptional:
    def test_config_loads_without_python_dotenv(self, monkeypatch, clean_env):
        """A cluster env missing python-dotenv must not break every import."""
        import cluster_kit.config as config_mod

        monkeypatch.setattr(config_mod, "load_dotenv", None)
        os.environ["CLUSTER_REMOTE_BASE"] = "/from/environment"
        cfg = load_config(env_file=Path("/nonexistent/.env"))
        assert cfg.remote_base == PurePosixPath("/from/environment")

    def test_existing_env_file_ignored_without_dotenv(
        self, tmp_path, monkeypatch, clean_env
    ):
        import cluster_kit.config as config_mod

        monkeypatch.setattr(config_mod, "load_dotenv", None)
        (tmp_path / ".env").write_text("CLUSTER_REMOTE_BASE=/from/dotenv\n")
        os.environ["CLUSTER_REMOTE_BASE"] = "/from/environment"
        cfg = load_config(env_file=tmp_path / ".env")
        assert cfg.remote_base == PurePosixPath("/from/environment")


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    def _make_config(self, **overrides):
        defaults = dict(
            host="cluster",
            user="testuser",
            remote_base=PurePosixPath("/absolute/path"),
            ssh_key=Path("/nonexistent/key"),
            ssh_timeout=30,
            sync_exclude="__pycache__,*.pyc",
        )
        defaults.update(overrides)
        return ClusterConfig(**defaults)

    def test_valid_config(self):
        cfg = self._make_config()
        assert validate_config(cfg) == []

    def test_empty_host(self):
        cfg = self._make_config(host="")
        errors = validate_config(cfg)
        assert any("CLUSTER_HOST" in e for e in errors)

    def test_whitespace_host(self):
        cfg = self._make_config(host="   ")
        errors = validate_config(cfg)
        assert any("CLUSTER_HOST" in e for e in errors)

    def test_empty_user(self):
        cfg = self._make_config(user="")
        errors = validate_config(cfg)
        assert any("CLUSTER_USER" in e for e in errors)

    def test_relative_remote_base(self):
        cfg = self._make_config(remote_base=PurePosixPath("relative/path"))
        errors = validate_config(cfg)
        assert any("CLUSTER_REMOTE_BASE" in e for e in errors)

    def test_timeout_too_low(self):
        cfg = self._make_config(ssh_timeout=0)
        errors = validate_config(cfg)
        assert any("CLUSTER_SSH_TIMEOUT" in e for e in errors)

    def test_timeout_too_high(self):
        cfg = self._make_config(ssh_timeout=301)
        errors = validate_config(cfg)
        assert any("CLUSTER_SSH_TIMEOUT" in e for e in errors)

    def test_sync_exclude_with_spaces(self):
        cfg = self._make_config(sync_exclude="__pycache__, *.pyc")
        errors = validate_config(cfg)
        assert any("CLUSTER_SYNC_EXCLUDE" in e for e in errors)

    def test_sync_exclude_trailing_space(self):
        cfg = self._make_config(sync_exclude="__pycache__,*.pyc ")
        errors = validate_config(cfg)
        assert any("CLUSTER_SYNC_EXCLUDE" in e for e in errors)

    def test_multiple_errors(self):
        cfg = self._make_config(host="", user="", ssh_timeout=999)
        errors = validate_config(cfg)
        assert len(errors) >= 3


# ---------------------------------------------------------------------------
# validate_config_strict
# ---------------------------------------------------------------------------


class TestValidateConfigStrict:
    def _make_config(self, **overrides):
        defaults = dict(
            host="cluster",
            user="testuser",
            remote_base=PurePosixPath("/absolute/path"),
            ssh_key=Path("/nonexistent/key"),
            ssh_timeout=30,
            sync_exclude="__pycache__,*.pyc",
        )
        defaults.update(overrides)
        return ClusterConfig(**defaults)

    def test_valid_passes(self):
        cfg = self._make_config()
        validate_config_strict(cfg)  # no exception

    def test_invalid_raises(self):
        cfg = self._make_config(host="")
        with pytest.raises(ConfigError, match="CLUSTER_HOST"):
            validate_config_strict(cfg)


# ---------------------------------------------------------------------------
# executor / sync_mode
# ---------------------------------------------------------------------------


class TestExecutorAndSyncMode:
    def test_defaults(self, minimal_env):
        config = load_config()
        assert config.executor == "slurm"
        assert config.sync_mode == "rsync"

    def test_env_overrides(self, minimal_env):
        os.environ["CLUSTER_EXECUTOR"] = "ssh"
        os.environ["CLUSTER_SYNC_MODE"] = "git"
        config = load_config()
        assert config.executor == "ssh"
        assert config.sync_mode == "git"

    def test_profile_prefixed_executor(self, minimal_env):
        os.environ["CLUSTER_PC_HOST"] = "pc"
        os.environ["CLUSTER_PC_EXECUTOR"] = "ssh"
        os.environ["CLUSTER_PC_SYNC_MODE"] = "git"
        os.environ["CLUSTER_PC_REMOTE_BASE"] = "/home/wsluser/GitHub/project"

        default_config = load_config()
        assert default_config.executor == "slurm"

        pc_config = load_config(env_profile="pc")
        assert pc_config.host == "pc"
        assert pc_config.executor == "ssh"
        assert pc_config.sync_mode == "git"
        assert str(pc_config.remote_base) == "/home/wsluser/GitHub/project"

    def test_invalid_executor_rejected(self, minimal_env):
        os.environ["CLUSTER_EXECUTOR"] = "kubernetes"
        errors = validate_config(load_config())
        assert any("CLUSTER_EXECUTOR" in e for e in errors)

    def test_invalid_sync_mode_rejected(self, minimal_env):
        os.environ["CLUSTER_SYNC_MODE"] = "ftp"
        errors = validate_config(load_config())
        assert any("CLUSTER_SYNC_MODE" in e for e in errors)
