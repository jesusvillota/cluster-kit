# pyright: reportMissingImports=false
"""Phone access lifecycle management for cluster TUI.

Manages tmux + ttyd orchestration for remote phone access to the cluster TUI.
"""

from __future__ import annotations

import argparse
import errno
import os
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from rich.console import Console
from rich.panel import Panel

from cluster_kit.tui.backend.job_actions import (
    QA_SAFE_MODE_ENV_VAR,
    is_qa_safe_mode_enabled,
)

DEFAULT_PHONE_ACCESS_PORT = 7681
DEFAULT_PHONE_ACCESS_HOST = "127.0.0.1"
DEFAULT_PHONE_ACCESS_SESSION_NAME = "cluster-kit-phone"
DEFAULT_PHONE_ACCESS_COMMAND = "cluster-kit tui"
DEFAULT_PHONE_UI_COMMAND = "cluster-kit tui --phone"
PHONE_ACCESS_PHONE_SESSION_SUFFIX = "-phone-ui"

PHONE_ACCESS_PORT_ENV_VAR = "CLUSTER_KIT_PHONE_PORT"
PHONE_ACCESS_SESSION_ENV_VAR = "CLUSTER_KIT_PHONE_SESSION_NAME"
PHONE_ACCESS_COMMAND_ENV_VAR = "CLUSTER_KIT_PHONE_COMMAND"
PHONE_ACCESS_SESSION_MARKER_ENV_VAR = "CLUSTER_KIT_PHONE_SESSION_MARKER"

PROJECT_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class PhoneAccessConfig:
    port: int
    session_name: str
    cluster_tui_command: str
    ui_mode: str = "desktop"
    qa_safe_mode_enabled: bool = False


@dataclass(frozen=True)
class DiscoveredProcess:
    pid: int
    command: str
    pid_alive: bool = True


@dataclass(frozen=True)
class PhoneAccessStatus:
    config: PhoneAccessConfig
    session_exists: bool
    ttyd_processes: tuple[DiscoveredProcess, ...]
    preflight_failed: bool = False

    @property
    def is_running(self) -> bool:
        return self.lifecycle_state == "running"

    @property
    def local_url(self) -> str:
        return build_phone_access_local_url(self.config)

    @property
    def alive_ttyd_processes(self) -> tuple[DiscoveredProcess, ...]:
        return tuple(process for process in self.ttyd_processes if process.pid_alive)

    @property
    def lifecycle_state(self) -> str:
        if self.preflight_failed:
            return "degraded"
        has_any_ttyd = bool(self.ttyd_processes)
        has_alive_ttyd = bool(self.alive_ttyd_processes)
        if (
            self.session_exists
            and has_alive_ttyd
            and len(self.alive_ttyd_processes) == len(self.ttyd_processes)
        ):
            return "running"
        if self.session_exists or has_any_ttyd:
            return "degraded"
        return "stopped"


class PhoneAccessPreflightError(RuntimeError):
    pass


def parse_port(value: str) -> int:
    port = int(value)
    if port <= 0:
        raise argparse.ArgumentTypeError("port must be a positive integer")
    return port


def resolve_phone_access_config(
    environ: Mapping[str, str] | None = None,
) -> PhoneAccessConfig:
    env = environ if environ is not None else os.environ
    raw_port = env.get(PHONE_ACCESS_PORT_ENV_VAR, str(DEFAULT_PHONE_ACCESS_PORT))
    try:
        port = parse_port(raw_port)
    except ValueError as exc:
        raise ValueError(f"Invalid {PHONE_ACCESS_PORT_ENV_VAR}: {raw_port!r}") from exc
    except argparse.ArgumentTypeError as exc:
        raise ValueError(f"Invalid {PHONE_ACCESS_PORT_ENV_VAR}: {raw_port!r}") from exc

    session_name = env.get(
        PHONE_ACCESS_SESSION_ENV_VAR,
        DEFAULT_PHONE_ACCESS_SESSION_NAME,
    )
    cluster_tui_command = env.get(
        PHONE_ACCESS_COMMAND_ENV_VAR,
        DEFAULT_PHONE_ACCESS_COMMAND,
    )
    qa_safe_mode_enabled = is_qa_safe_mode_enabled(env.get(QA_SAFE_MODE_ENV_VAR))
    return PhoneAccessConfig(
        port=port,
        session_name=session_name,
        cluster_tui_command=cluster_tui_command,
        ui_mode="desktop",
        qa_safe_mode_enabled=qa_safe_mode_enabled,
    )


def build_phone_ui_command() -> str:
    return DEFAULT_PHONE_UI_COMMAND


def is_phone_ui_command(command: str) -> bool:
    normalized_command = " ".join(command.split())
    normalized_phone_command = " ".join(DEFAULT_PHONE_UI_COMMAND.split())
    return normalized_command == normalized_phone_command


def resolve_tmux_session_name(config: PhoneAccessConfig) -> str:
    if config.ui_mode == "phone" and not config.session_name.endswith(
        PHONE_ACCESS_PHONE_SESSION_SUFFIX
    ):
        return f"{config.session_name}{PHONE_ACCESS_PHONE_SESSION_SUFFIX}"
    return config.session_name


def build_cluster_tui_shell_command(config: PhoneAccessConfig) -> str:
    if not config.qa_safe_mode_enabled:
        return f"exec {config.cluster_tui_command}"
    return f"{QA_SAFE_MODE_ENV_VAR}=1 exec {config.cluster_tui_command}"


def build_tmux_has_session_command(session_name: str) -> list[str]:
    return ["tmux", "has-session", "-t", session_name]


def build_tmux_start_command(config: PhoneAccessConfig) -> list[str]:
    shell_command = build_cluster_tui_shell_command(config)
    return [
        "tmux",
        "new-session",
        "-d",
        "-s",
        resolve_tmux_session_name(config),
        "-c",
        str(PROJECT_ROOT),
        shell_command,
    ]


def build_tmux_stop_command(session_name: str) -> list[str]:
    return ["tmux", "kill-session", "-t", session_name]


def build_session_verification_marker(config: PhoneAccessConfig) -> str:
    return "|".join(
        (
            "v1",
            f"ui={config.ui_mode}",
            f"qa_safe={'1' if config.qa_safe_mode_enabled else '0'}",
            f"command={config.cluster_tui_command}",
        )
    )


def build_tmux_set_marker_command(config: PhoneAccessConfig) -> list[str]:
    return [
        "tmux",
        "set-environment",
        "-t",
        resolve_tmux_session_name(config),
        PHONE_ACCESS_SESSION_MARKER_ENV_VAR,
        build_session_verification_marker(config),
    ]


def build_tmux_show_marker_command(config: PhoneAccessConfig) -> list[str]:
    return [
        "tmux",
        "show-environment",
        "-t",
        resolve_tmux_session_name(config),
        PHONE_ACCESS_SESSION_MARKER_ENV_VAR,
    ]


def build_phone_access_local_url(config: PhoneAccessConfig) -> str:
    return f"http://{DEFAULT_PHONE_ACCESS_HOST}:{config.port}"


def build_tailscale_serve_command(config: PhoneAccessConfig) -> str:
    return f"tailscale serve {build_phone_access_local_url(config)}"


def build_ttyd_start_command(config: PhoneAccessConfig) -> list[str]:
    return [
        "ttyd",
        "-i",
        DEFAULT_PHONE_ACCESS_HOST,
        "-p",
        str(config.port),
        "tmux",
        "attach-session",
        "-t",
        resolve_tmux_session_name(config),
    ]


def build_process_discovery_command() -> list[str]:
    return ["ps", "ax", "-o", "pid=,command="]


def build_required_command_templates(
    config: PhoneAccessConfig,
) -> dict[str, list[str]]:
    tmux_session_name = resolve_tmux_session_name(config)
    return {
        "tmux session check": build_tmux_has_session_command(tmux_session_name),
        "tmux session start": build_tmux_start_command(config),
        "tmux session stop": build_tmux_stop_command(tmux_session_name),
        "ttyd session start": build_ttyd_start_command(config),
        "process discovery": build_process_discovery_command(),
    }


def _validate_command_template(name: str, command: Sequence[str]) -> str | None:
    if not command:
        return f"{name} template is empty"
    if any(not part.strip() for part in command):
        return f"{name} template contains an empty command segment"
    return None


def _format_missing_binaries_message(missing_binaries: Sequence[str]) -> str:
    binaries = ", ".join(missing_binaries)
    lines = [
        "Phone access preflight failed: missing required binaries",
        f"Missing: {binaries}",
    ]
    if sys.platform == "darwin":
        lines.append("Remediation: brew install tmux ttyd")
    else:
        lines.append(
            "Remediation: install tmux and ttyd using your system package manager"
        )
    return "\n".join(lines)


def _format_repo_root_message(current_dir: Path) -> str:
    return "\n".join(
        [
            "Phone access preflight failed: run from the repository root",
            f"Current directory: {current_dir}",
            f"Expected directory: {PROJECT_ROOT}",
        ]
    )


def _format_command_template_message(errors: Sequence[str]) -> str:
    return "\n".join(
        [
            (
                "Phone access preflight failed: required command "
                "template validation failed"
            ),
            *errors,
        ]
    )


def ensure_phone_access_preflight(
    config: PhoneAccessConfig,
    *,
    current_dir: Path | None = None,
    binary_resolver=None,
) -> None:
    active_binary_resolver = binary_resolver or shutil.which
    active_dir = (current_dir or Path.cwd()).resolve()
    if active_dir != PROJECT_ROOT:
        raise PhoneAccessPreflightError(_format_repo_root_message(active_dir))

    missing_binaries = [
        binary_name
        for binary_name in ("tmux", "ttyd")
        if active_binary_resolver(binary_name) is None
    ]
    if missing_binaries:
        raise PhoneAccessPreflightError(
            _format_missing_binaries_message(missing_binaries)
        )

    template_errors = [
        error
        for template_name, command in build_required_command_templates(config).items()
        if (error := _validate_command_template(template_name, command)) is not None
    ]
    if template_errors:
        raise PhoneAccessPreflightError(
            _format_command_template_message(template_errors)
        )


def _render_preflight_error(error: PhoneAccessPreflightError) -> Panel:
    return Panel.fit(error.args[0], title="Preflight check failed", border_style="red")


def parse_process_discovery_output(stdout: str) -> tuple[DiscoveredProcess, ...]:
    processes: list[DiscoveredProcess] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        if not pid_text or not command:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        processes.append(DiscoveredProcess(pid=pid, command=command.strip()))
    return tuple(processes)


def is_phone_access_ttyd_process(command: str, config: PhoneAccessConfig) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    tmux_target_session = resolve_tmux_session_name(config)

    expected_suffix = [
        "-i",
        DEFAULT_PHONE_ACCESS_HOST,
        "-p",
        str(config.port),
        "tmux",
        "attach-session",
        "-t",
        tmux_target_session,
    ]
    return (
        len(tokens) == len(expected_suffix) + 1
        and Path(tokens[0]).name == "ttyd"
        and [
            Path(token).name if index == 4 else token
            for index, token in enumerate(tokens[1:])
        ]
        == expected_suffix
    )


def discover_matching_ttyd_processes(
    stdout: str, config: PhoneAccessConfig
) -> tuple[DiscoveredProcess, ...]:
    return tuple(
        process
        for process in parse_process_discovery_output(stdout)
        if is_phone_access_ttyd_process(process.command, config)
    )


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def spawn_command(command: Sequence[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )


def tmux_session_exists(
    config: PhoneAccessConfig,
    runner=run_command,
) -> bool:
    result = runner(build_tmux_has_session_command(resolve_tmux_session_name(config)))
    return result.returncode == 0


def is_process_pid_alive(pid: int, signal_sender=os.kill) -> bool:
    try:
        signal_sender(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        raise
    return True


def annotate_process_liveness(
    processes: Sequence[DiscoveredProcess],
    pid_checker=is_process_pid_alive,
) -> tuple[DiscoveredProcess, ...]:
    return tuple(
        DiscoveredProcess(
            pid=process.pid,
            command=process.command,
            pid_alive=pid_checker(process.pid),
        )
        for process in processes
    )


def get_phone_access_status(
    config: PhoneAccessConfig,
    runner=run_command,
    pid_checker=is_process_pid_alive,
) -> PhoneAccessStatus:
    session_exists = tmux_session_exists(config, runner=runner)
    process_result = runner(build_process_discovery_command())
    ttyd_processes = (
        annotate_process_liveness(
            discover_matching_ttyd_processes(process_result.stdout, config),
            pid_checker=pid_checker,
        )
        if process_result.returncode == 0
        else tuple()
    )
    return PhoneAccessStatus(
        config=config,
        session_exists=session_exists,
        ttyd_processes=ttyd_processes,
    )


def _ensure_command_succeeded(
    result: subprocess.CompletedProcess[str],
    command_name: str,
) -> None:
    if result.returncode == 0:
        return
    detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
    raise RuntimeError(f"{command_name} failed: {detail}")


def _ensure_safe_session_reuse(
    config: PhoneAccessConfig,
    status: PhoneAccessStatus,
    *,
    runner=run_command,
) -> None:
    if not config.qa_safe_mode_enabled or not status.session_exists:
        return

    marker_result = runner(build_tmux_show_marker_command(config))
    expected_prefix = f"{PHONE_ACCESS_SESSION_MARKER_ENV_VAR}="
    actual_marker = marker_result.stdout.strip()
    expected_marker = build_session_verification_marker(config)
    if marker_result.returncode == 0 and actual_marker.startswith(expected_prefix):
        if actual_marker[len(expected_prefix) :] == expected_marker:
            return

    raise RuntimeError(
        "qa-safe-mode start refused: existing tmux session is unverified or "
        "incompatible; stop the session first or use a different session name"
    )


def build_session_mode_label(config: PhoneAccessConfig) -> str:
    return "phone-safe" if config.ui_mode == "phone" else "shared"


def start_phone_access(
    config: PhoneAccessConfig,
    runner=run_command,
    spawner=spawn_command,
) -> PhoneAccessStatus:
    status = get_phone_access_status(config, runner=runner)
    _ensure_safe_session_reuse(config, status, runner=runner)
    if not status.session_exists:
        _ensure_command_succeeded(
            runner(build_tmux_start_command(config)),
            "tmux session start",
        )
        if config.qa_safe_mode_enabled:
            _ensure_command_succeeded(
                runner(build_tmux_set_marker_command(config)),
                "tmux session marker set",
            )
    if not status.alive_ttyd_processes:
        spawner(build_ttyd_start_command(config))
    return get_phone_access_status(config, runner=runner)


def stop_phone_access(
    config: PhoneAccessConfig,
    runner=run_command,
) -> PhoneAccessStatus:
    status = get_phone_access_status(config, runner=runner)
    for process in sorted(status.ttyd_processes, key=lambda process: process.pid):
        if not process.pid_alive:
            continue
        try:
            os.kill(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    if status.session_exists:
        _ensure_command_succeeded(
            runner(build_tmux_stop_command(resolve_tmux_session_name(config))),
            "tmux session stop",
        )
    return get_phone_access_status(config, runner=runner)


def build_status_lines(status: PhoneAccessStatus) -> list[str]:
    lines = [
        f"phone-access state: {status.lifecycle_state}",
        f"ui mode: {status.config.ui_mode}",
        f"session mode: {build_session_mode_label(status.config)}",
        f"prerequisites: {build_prerequisite_state_line()}",
        f"local url: {status.local_url}",
        f"tmux session: {status.config.session_name}",
        f"tmux target session: {resolve_tmux_session_name(status.config)}",
        f"tmux session active: {'yes' if status.session_exists else 'no'}",
        f"ttyd state: {build_ttyd_state_label(status)}",
        f"ttyd processes: {len(status.ttyd_processes)}",
        "tailscale has not been configured automatically",
        f"manual next step: {build_tailscale_serve_command(status.config)}",
        build_single_operator_warning_line(),
    ]
    for process in status.ttyd_processes:
        lines.append(
            f"ttyd pid {process.pid} alive: {'yes' if process.pid_alive else 'no'} - "
            f"{process.command}"
        )
    return lines


def build_start_handoff_lines(status: PhoneAccessStatus) -> list[str]:
    lines = [
        "phone-access ready: yes",
        f"phone-access state: {status.lifecycle_state}",
        f"phone-access running: {'yes' if status.is_running else 'no'}",
        f"ui mode: {status.config.ui_mode}",
        f"session mode: {build_session_mode_label(status.config)}",
        f"session: {status.config.session_name}",
        f"tmux target session: {resolve_tmux_session_name(status.config)}",
        f"local url: {status.local_url}",
        f"prerequisites: {build_prerequisite_state_line()}",
        f"tmux session active: {'yes' if status.session_exists else 'no'}",
        f"ttyd state: {build_ttyd_state_label(status)}",
        f"ttyd processes: {len(status.ttyd_processes)}",
    ]
    lines.append(
        "qa safe mode: "
        f"{'enabled' if status.config.qa_safe_mode_enabled else 'disabled'}"
    )
    lines.extend(
        [
            "tailscale has not been configured automatically",
            f"manual next step: {build_tailscale_serve_command(status.config)}",
            build_single_operator_warning_line(),
        ]
    )
    return lines


def build_stop_lines(status: PhoneAccessStatus) -> list[str]:
    return [
        "phone-access stopped: yes",
        f"phone-access state: {status.lifecycle_state}",
        f"ui mode: {status.config.ui_mode}",
        f"session mode: {build_session_mode_label(status.config)}",
        f"prerequisites: {build_prerequisite_state_line()}",
        "qa safe mode: "
        f"{'enabled' if status.config.qa_safe_mode_enabled else 'disabled'}",
        f"session: {status.config.session_name}",
        f"tmux target session: {resolve_tmux_session_name(status.config)}",
        f"local url: {status.local_url}",
        f"tmux session active: {'yes' if status.session_exists else 'no'}",
        f"ttyd state: {build_ttyd_state_label(status)}",
        f"ttyd processes: {len(status.ttyd_processes)}",
        build_single_operator_warning_line(),
    ]


def build_prerequisite_state_line(binary_resolver=None) -> str:
    active_binary_resolver = binary_resolver or shutil.which
    tmux_ready = active_binary_resolver("tmux") is not None
    ttyd_ready = active_binary_resolver("ttyd") is not None
    return f"tmux={'yes' if tmux_ready else 'no'}, ttyd={'yes' if ttyd_ready else 'no'}"


def build_single_operator_warning_line() -> str:
    return "warning: single operator only; do not use from multiple devices at once"


def build_ttyd_state_label(status: PhoneAccessStatus) -> str:
    if status.alive_ttyd_processes:
        return "running"
    if status.ttyd_processes:
        return "degraded"
    return "stopped"


def add_shared_cli_arguments(parser: argparse.ArgumentParser) -> None:
    defaults = resolve_phone_access_config()
    parser.add_argument(
        "--port",
        type=parse_port,
        default=defaults.port,
        metavar="PORT",
        help=(
            "ttyd port for phone access "
            f"(default: {defaults.port}; env: {PHONE_ACCESS_PORT_ENV_VAR})"
        ),
    )
    parser.add_argument(
        "--session-name",
        default=defaults.session_name,
        help=(
            "tmux session name for the cluster TUI "
            f"(default: {defaults.session_name}; env: {PHONE_ACCESS_SESSION_ENV_VAR})"
        ),
    )
    parser.add_argument(
        "--cluster-tui-command",
        default=argparse.SUPPRESS,
        help=(
            "command started inside tmux "
            f"(default: {defaults.cluster_tui_command}; "
            f"env: {PHONE_ACCESS_COMMAND_ENV_VAR}; "
            "wins over --phone-ui when explicitly provided)"
        ),
    )
    parser.add_argument(
        "--phone-ui",
        action="store_true",
        help=(
            "start the phone-oriented Cluster TUI command when no explicit "
            "--cluster-tui-command is provided"
        ),
    )
    parser.add_argument(
        "--qa-safe-mode",
        action="store_true",
        default=defaults.qa_safe_mode_enabled,
        help=(
            "route cancel/sync through harmless QA-safe stubs "
            f"(default: {defaults.qa_safe_mode_enabled}; env: {QA_SAFE_MODE_ENV_VAR}=1)"
        ),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cluster TUI phone-access lifecycle helper",
        epilog=(
            "Shared options for start/status/stop: "
            "--port PORT --session-name NAME --cluster-tui-command COMMAND --phone-ui. "
            "Precedence: --cluster-tui-command > --phone-ui > desktop default command."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser(
        "start",
        help="Start tmux + ttyd phone access for the cluster TUI",
        description=(
            "Start tmux + ttyd phone access for the cluster TUI. Precedence: "
            "--cluster-tui-command > --phone-ui > desktop default command."
        ),
    )
    add_shared_cli_arguments(start_parser)

    status_parser = subparsers.add_parser(
        "status",
        help="Show tmux + ttyd phone-access status",
    )
    add_shared_cli_arguments(status_parser)
    status_parser.description = (
        "Show tmux + ttyd phone-access status and the selected UI mode."
    )

    stop_parser = subparsers.add_parser(
        "stop",
        help="Stop tmux + ttyd phone access",
    )
    add_shared_cli_arguments(stop_parser)
    stop_parser.description = (
        "Stop tmux + ttyd phone access and report the active UI mode."
    )

    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> PhoneAccessConfig:
    defaults = resolve_phone_access_config()
    cluster_tui_command = getattr(args, "cluster_tui_command", None)
    if cluster_tui_command is not None:
        ui_mode = "phone" if is_phone_ui_command(cluster_tui_command) else "custom"
    elif args.phone_ui:
        cluster_tui_command = build_phone_ui_command()
        ui_mode = "phone"
    elif os.environ.get(PHONE_ACCESS_COMMAND_ENV_VAR) is not None:
        cluster_tui_command = defaults.cluster_tui_command
        ui_mode = "phone" if is_phone_ui_command(cluster_tui_command) else "custom"
    else:
        cluster_tui_command = defaults.cluster_tui_command
        ui_mode = "desktop"
    return PhoneAccessConfig(
        port=args.port,
        session_name=args.session_name,
        cluster_tui_command=cluster_tui_command,
        ui_mode=ui_mode,
        qa_safe_mode_enabled=args.qa_safe_mode,
    )


def dispatch_cli(
    args: argparse.Namespace,
    console: Console | None = None,
) -> int:
    active_console = console or Console()
    config = config_from_args(args)

    try:
        ensure_phone_access_preflight(config)
    except PhoneAccessPreflightError as exc:
        active_console.print(_render_preflight_error(exc))
        if args.command == "status":
            status = PhoneAccessStatus(
                config=config,
                session_exists=False,
                ttyd_processes=tuple(),
                preflight_failed=True,
            )
            active_console.print("\n".join(build_status_lines(status)))
        return 1

    if args.command == "start":
        status = start_phone_access(config)
    elif args.command == "status":
        status = get_phone_access_status(config)
    elif args.command == "stop":
        status = stop_phone_access(config)
    else:
        raise ValueError(f"Unsupported command: {args.command}")

    if args.command == "start":
        active_console.print("\n".join(build_start_handoff_lines(status)))
    elif args.command == "status":
        active_console.print("\n".join(build_status_lines(status)))
    else:
        active_console.print("\n".join(build_stop_lines(status)))
    return 0 if status.is_running or args.command == "stop" else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return dispatch_cli(args)
