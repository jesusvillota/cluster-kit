"""Tests for environment creation and launch functionality."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cluster_kit.env.builder import (
    _extract_python_version,
    create_environment_files,
    generate_environment_yml,
    generate_slurm_script,
    launch_environment,
    parse_pyproject,
)


class TestExtractPythonVersion:
    def test_greater_than_equal(self):
        assert _extract_python_version(">=3.10") == "3.10"

    def test_greater_than_equal_with_upper(self):
        assert _extract_python_version(">=3.10,<3.13") == "3.10"

    def test_tilde_equal(self):
        assert _extract_python_version("~=3.10") == "3.10"

    def test_double_equal(self):
        assert _extract_python_version("==3.10.*") == "3.10"

    def test_fallback_first_version(self):
        assert _extract_python_version("!=3.9,>=3.10,<4.0") == "3.10"

    def test_no_version_defaults(self):
        assert _extract_python_version("") == "3.12"
        assert _extract_python_version(">=1.0") == "1.0"

    def test_patch_version(self):
        assert _extract_python_version(">=3.10.2") == "3.10"


class TestParsePyproject:
    def test_parse_cluster_kit_pyproject(self):
        pyproject = Path(__file__).parent.parent / "pyproject.toml"
        data = parse_pyproject(pyproject)

        assert data["name"] == "cluster-kit"
        assert data["python_version"] == "3.10"
        assert "python-dotenv>=1.0.0" in data["dependencies"]
        assert "textual>=0.41.0" in data["dependencies"]
        assert "rich>=13.0.0" in data["dependencies"]
        assert "pytest>=7.0" in data["dev_dependencies"]
        assert "ruff>=0.1.0" in data["dev_dependencies"]

    def test_parse_minimal_pyproject(self, tmp_path: Path):
        content = textwrap.dedent("""\
            [project]
            name = "my-app"
            requires-python = ">=3.11"
            dependencies = [
                "requests>=2.28.0",
            ]
        """)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(content)

        data = parse_pyproject(pyproject)

        assert data["name"] == "my-app"
        assert data["python_version"] == "3.11"
        assert data["dependencies"] == ["requests>=2.28.0"]
        assert data["dev_dependencies"] == []

    def test_parse_no_dependencies(self, tmp_path: Path):
        content = textwrap.dedent("""\
            [project]
            name = "bare"
        """)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(content)

        data = parse_pyproject(pyproject)

        assert data["name"] == "bare"
        assert data["python_version"] == "3.12"
        assert data["dependencies"] == []
        assert data["dev_dependencies"] == []

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            parse_pyproject(tmp_path / "nonexistent.toml")


class TestGenerateEnvironmentYml:
    def test_basic_generation(self):
        data = {
            "name": "my-project",
            "python_version": "3.10",
            "dependencies": ["pandas>=2.0.0", "numpy>=1.24.0"],
            "dev_dependencies": [],
        }

        yml = generate_environment_yml(data)

        assert "name: my-project" in yml
        assert "channels:" in yml
        assert "  - conda-forge" in yml
        assert "  - defaults" in yml
        assert "  - python=3.10" in yml
        assert "  - pip" in yml
        assert "    - pandas>=2.0.0" in yml
        assert "    - numpy>=1.24.0" in yml

    def test_with_dev_dependencies(self):
        data = {
            "name": "my-project",
            "python_version": "3.11",
            "dependencies": ["requests>=2.28.0"],
            "dev_dependencies": ["pytest>=7.0", "ruff>=0.1.0"],
        }

        yml = generate_environment_yml(data, include_dev=True)

        assert "    - requests>=2.28.0" in yml
        assert "    - pytest>=7.0" in yml
        assert "    - ruff>=0.1.0" in yml

    def test_without_dev_dependencies(self):
        data = {
            "name": "my-project",
            "python_version": "3.11",
            "dependencies": ["requests>=2.28.0"],
            "dev_dependencies": ["pytest>=7.0"],
        }

        yml = generate_environment_yml(data, include_dev=False)

        assert "    - requests>=2.28.0" in yml
        assert "pytest" not in yml

    def test_no_dependencies(self):
        data = {
            "name": "bare",
            "python_version": "3.12",
            "dependencies": [],
            "dev_dependencies": [],
        }

        yml = generate_environment_yml(data)

        assert "python=3.12" in yml
        assert "  - pip: []" in yml or "  - pip:" in yml

    def test_yaml_is_valid_structure(self):
        data = {
            "name": "test",
            "python_version": "3.12",
            "dependencies": ["pkg>=1.0"],
            "dev_dependencies": [],
        }

        yml = generate_environment_yml(data)

        lines = yml.strip().split("\n")
        assert lines[0] == "name: test"
        assert lines[1] == "channels:"
        assert lines[2] == "  - conda-forge"
        assert lines[3] == "  - defaults"
        assert lines[4] == "dependencies:"
        assert lines[5] == "  - python=3.12"
        assert lines[6] == "  - pip"


class TestGenerateSlurmScript:
    def test_basic_generation(self):
        script = generate_slurm_script("my-project", "/mnt/data/my-project/")

        assert "env_my-project" in script
        assert "/mnt/data/my-project/_logs_/build_env/" in script
        assert '/mnt/data/my-project/conda_envs/my-project/"' in script
        assert "/mnt/data/my-project/environment.yml" in script
        assert "conda env create" in script
        assert "module load conda/3" in script
        assert "CONDA_PKGS_DIRS" in script

    def test_trailing_slash_handled(self):
        script = generate_slurm_script("app", "/mnt/data/app")

        assert "/mnt/data/app/environment.yml" in script
        assert "/mnt/data/app/conda_envs/app/" in script

    def test_conda_base_is_parent(self):
        script = generate_slurm_script("app", "/mnt/data/user/project")

        assert "/mnt/data/user/.conda/pkgs" in script
        assert "/mnt/data/user/.conda/envs" in script
        assert "/mnt/data/user/.cache" in script

    def test_strips_trailing_slash_for_parent(self):
        script = generate_slurm_script("app", "/mnt/data/user/project/")

        assert "/mnt/data/user/.conda/pkgs" in script


class TestCreateEnvironmentFiles:
    def test_creates_both_files(self, tmp_path: Path):
        pyproject_path = tmp_path / "pyproject.toml"
        pyproject_path.write_text(textwrap.dedent("""\
            [project]
            name = "test-project"
            requires-python = ">=3.11"
            dependencies = [
                "httpx>=0.24.0",
            ]
        """))

        output_dir = tmp_path / "env"
        with patch(
            "cluster_kit.env.builder.get_remote_base",
            return_value="/mnt/data/test-project/",
        ):
            create_environment_files(
                pyproject_path=pyproject_path,
                output_dir=output_dir,
                dry_run=False,
            )

        yml_path = output_dir / "environment.yml"
        slurm_path = output_dir / "conda_env.slurm"

        assert yml_path.exists()
        assert slurm_path.exists()

        yml_content = yml_path.read_text()
        assert "name: test-project" in yml_content
        assert "python=3.11" in yml_content
        assert "    - httpx>=0.24.0" in yml_content

        slurm_content = slurm_path.read_text()
        assert "env_test-project" in slurm_content
        assert "/mnt/data/test-project/" in slurm_content

    def test_dry_run_does_not_write(self, tmp_path: Path):
        pyproject_path = tmp_path / "pyproject.toml"
        pyproject_path.write_text(textwrap.dedent("""\
            [project]
            name = "test-project"
            dependencies = []
        """))

        output_dir = tmp_path / "env"
        with patch(
            "cluster_kit.env.builder.get_remote_base",
            return_value="/mnt/data/test-project/",
        ):
            create_environment_files(
                pyproject_path=pyproject_path,
                output_dir=output_dir,
                dry_run=True,
            )

        assert not output_dir.exists()

    def test_missing_pyproject_prints_error(self, tmp_path: Path):
        with pytest.raises(SystemExit) as exc_info:
            create_environment_files(
                pyproject_path=tmp_path / "nonexistent.toml",
                output_dir=tmp_path,
                dry_run=False,
            )
        assert exc_info.value.code == 1


class TestLaunchEnvironment:
    def test_missing_env_files_triggers_auto_generation(self, tmp_path: Path):
        pyproject_path = tmp_path / "pyproject.toml"
        pyproject_path.write_text(textwrap.dedent("""\
            [project]
            name = "launch-test"
            requires-python = ">=3.11"
            dependencies = ["click>=8.0"]
        """))

        env_file = tmp_path / "environment.yml"
        slurm_file = tmp_path / "conda_env.slurm"

        assert not env_file.exists()
        assert not slurm_file.exists()

        with patch(
            "cluster_kit.env.builder.get_remote_base",
            return_value="/mnt/data/launch-test/",
        ):
            with patch(
                "cluster_kit.env.builder._test_connection", return_value=True
            ):
                with patch(
                    "cluster_kit.env.builder._ssh_run",
                    return_value=MagicMock(
                        stdout="Submitted batch job 12345", returncode=0
                    ),
                ):
                    with patch(
                        "cluster_kit.env.builder._upload_file", return_value=True
                    ):
                        try:
                            launch_environment(
                                env_file=env_file,
                                slurm_file=slurm_file,
                                pyproject_path=pyproject_path,
                                wait=False,
                            )
                        except SystemExit:
                            pass

        # Files should have been auto-generated
        assert env_file.exists()
        assert slurm_file.exists()

    def test_launch_without_config_errors(self, tmp_path: Path):
        pyproject_path = tmp_path / "pyproject.toml"
        pyproject_path.write_text(textwrap.dedent("""\
            [project]
            name = "no-config-test"
            dependencies = []
        """))

        env_file = tmp_path / "environment.yml"
        slurm_file = tmp_path / "conda_env.slurm"

        # Manually create env files so auto-generation is skipped
        env_file.write_text("name: no-config-test\n")
        slurm_file.write_text("#!/bin/bash\necho test\n")

        with patch(
            "cluster_kit.env.builder.get_remote_base",
            side_effect=Exception("Not configured"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                launch_environment(
                    env_file=env_file,
                    slurm_file=slurm_file,
                    pyproject_path=pyproject_path,
                    wait=False,
                )
            assert exc_info.value.code == 1

    def test_connection_failure_errors(self, tmp_path: Path):
        pyproject_path = tmp_path / "pyproject.toml"
        pyproject_path.write_text(textwrap.dedent("""\
            [project]
            name = "conn-fail-test"
            dependencies = []
        """))

        env_file = tmp_path / "environment.yml"
        slurm_file = tmp_path / "conda_env.slurm"
        env_file.write_text("name: test\n")
        slurm_file.write_text("#!/bin/bash\necho test\n")

        with patch(
            "cluster_kit.env.builder.get_remote_base",
            return_value="/mnt/data/test/",
        ):
            with patch(
                "cluster_kit.env.builder._test_connection",
                return_value=False,
            ):
                with pytest.raises(SystemExit) as exc_info:
                    launch_environment(
                        env_file=env_file,
                        slurm_file=slurm_file,
                        pyproject_path=pyproject_path,
                        wait=False,
                    )
                assert exc_info.value.code == 1


class TestCLIEnvParser:
    def test_env_create_defaults(self):
        from cluster_kit.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["env", "create"])

        assert args.env_command == "create"
        assert args.pyproject == "pyproject.toml"
        assert args.output_dir == "."
        assert args.python_version is None
        assert args.include_dev is False
        assert args.dry_run is False

    def test_env_create_custom_flags(self):
        from cluster_kit.cli import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "env", "create",
            "--pyproject", "custom.toml",
            "--output-dir", "dist",
            "--python-version", "3.11",
            "--include-dev",
            "--dry-run",
        ])

        assert args.env_command == "create"
        assert args.pyproject == "custom.toml"
        assert args.output_dir == "dist"
        assert args.python_version == "3.11"
        assert args.include_dev is True
        assert args.dry_run is True

    def test_env_launch_defaults(self):
        from cluster_kit.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["env", "launch"])

        assert args.env_command == "launch"
        assert args.pyproject == "pyproject.toml"
        assert args.env_file == "environment.yml"
        assert args.slurm_file == "conda_env.slurm"
        assert args.python_version is None
        assert args.wait is False
        assert args.check_interval == 30

    def test_env_launch_custom_flags(self):
        from cluster_kit.cli import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "env", "launch",
            "--pyproject", "custom.toml",
            "--env-file", "my_env.yml",
            "--slurm-file", "my_env.slurm",
            "--python-version", "3.12",
            "--wait",
            "--check-interval", "10",
        ])

        assert args.env_command == "launch"
        assert args.pyproject == "custom.toml"
        assert args.env_file == "my_env.yml"
        assert args.slurm_file == "my_env.slurm"
        assert args.python_version == "3.12"
        assert args.wait is True
        assert args.check_interval == 10

    def test_env_without_command_shows_help(self):
        from cluster_kit.cli import build_parser

        parser = build_parser()

        with pytest.raises(SystemExit):
            parser.parse_args(["env"])
