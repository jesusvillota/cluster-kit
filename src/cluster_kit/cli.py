"""CLI entry point for cluster-kit."""

import argparse
import sys

from cluster_kit import __version__
from cluster_kit.config import load_config, validate_config


def _cmd_config(args: argparse.Namespace) -> None:
    """Display current cluster configuration."""
    try:
        config = load_config()
    except Exception as exc:
        print(f"[cluster-kit] Failed to load config: {exc}", file=sys.stderr)
        sys.exit(1)

    errors = validate_config(config)

    print("=== Cluster Configuration ===")
    print(f"  host:          {config.host}")
    print(f"  user:          {config.user}")
    print(f"  remote_base:   {config.remote_base}")
    print(f"  ssh_key:       {config.ssh_key}")
    print(f"  ssh_timeout:   {config.ssh_timeout}")
    print(f"  sync_exclude:  {config.sync_exclude}")

    if errors:
        print("\n[WARN] Validation issues:")
        for err in errors:
            print(f"  - {err}")
    else:
        print("\n[OK] Configuration is valid.")


def _cmd_sync_code(args: argparse.Namespace) -> None:
    """Sync source code to the cluster."""
    from cluster_kit.sync.code import CodeDeployer

    deployer = CodeDeployer(
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    success = deployer.deploy()
    if not success:
        sys.exit(1)


def _cmd_sync_outputs(args: argparse.Namespace) -> None:
    """Sync output files from the cluster."""
    from cluster_kit.sync.outputs import OutputSyncer, parse_formats

    mode = args.mode if args.mode else "visualization"

    formats = None
    if args.formats:
        try:
            formats = parse_formats(args.formats)
        except ValueError as e:
            print(f"[cluster-kit] Error: {e}", file=sys.stderr)
            sys.exit(1)

    syncer = OutputSyncer(
        mode=mode,
        formats=formats,
        dry_run=args.dry_run,
        delete=args.delete,
        verbose=args.verbose,
        show_tree=args.show_tree,
    )
    success = syncer.sync()
    if not success:
        sys.exit(1)


def _cmd_sync_cp(args: argparse.Namespace) -> None:
    """Copy files between local and cluster."""
    from cluster_kit.sync.transfer import FileTransfer

    transfer = FileTransfer(
        dry_run=args.dry_run,
        verbose=args.verbose,
        recursive=args.recursive,
    )
    success = transfer.copy(args.src, args.dst)
    if not success:
        sys.exit(1)


def _cmd_tui(args: argparse.Namespace) -> None:
    """Launch the cluster management TUI."""
    if args.phone:
        from cluster_kit.tui.app_phone import PhoneClusterTUI

        app = PhoneClusterTUI(
            refresh_interval=args.refresh,
            all_users=args.all_users,
        )
    else:
        from cluster_kit.tui.app import ClusterTUI

        app = ClusterTUI(
            refresh_interval=args.refresh,
            all_users=args.all_users,
        )
    app.run()


def _cmd_launch(args: argparse.Namespace) -> None:
    """Submit a script as a SLURM job."""
    from cluster_kit.launch.launcher import submit_job

    script_path = args.script

    if args.run_from == "local":
        import subprocess
        import sys

        result = subprocess.run([sys.executable, script_path])
        sys.exit(result.returncode)

    job_id = submit_job(
        script_path,
        partition=args.partition,
        cpus=args.slurm_cpus,
        mem=args.slurm_mem,
        time=args.slurm_time,
        sync=args.sync,
    )

    if job_id is None:
        sys.exit(1)


def _cmd_serve(args: argparse.Namespace) -> None:
    """Start a server for phone access."""
    from rich.console import Console

    from cluster_kit.tui.phone_access import (
        PhoneAccessPreflightError,
        PhoneAccessStatus,
        _render_preflight_error,
        build_start_handoff_lines,
        build_status_lines,
        build_stop_lines,
        config_from_args,
        ensure_phone_access_preflight,
        get_phone_access_status,
        start_phone_access,
        stop_phone_access,
    )

    console = Console()
    config = config_from_args(args)

    try:
        ensure_phone_access_preflight(config)
    except PhoneAccessPreflightError as exc:
        console.print(_render_preflight_error(exc))
        if args.serve_command == "status":
            status = PhoneAccessStatus(
                config=config,
                session_exists=False,
                ttyd_processes=tuple(),
                preflight_failed=True,
            )
            console.print("\n".join(build_status_lines(status)))
        sys.exit(1)

    if args.serve_command == "start":
        status = start_phone_access(config)
        console.print("\n".join(build_start_handoff_lines(status)))
        sys.exit(0 if status.is_running else 1)
    elif args.serve_command == "status":
        status = get_phone_access_status(config)
        console.print("\n".join(build_status_lines(status)))
        sys.exit(0 if status.is_running else 1)
    elif args.serve_command == "stop":
        status = stop_phone_access(config)
        console.print("\n".join(build_stop_lines(status)))
        sys.exit(0)


def _build_sync_parser(subparsers: argparse._SubParsersAction) -> None:
    """Build the 'sync' subcommand with nested sub-subcommands."""
    sync_parser = subparsers.add_parser(
        "sync",
        help="Synchronize files between local machine and cluster",
    )
    sync_sub = sync_parser.add_subparsers(dest="sync_command", help="Sync operations")

    code_parser = sync_sub.add_parser("code", help="Push source code to the cluster")
    code_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview changes without syncing",
    )
    code_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Show detailed output",
    )
    code_parser.set_defaults(func=_cmd_sync_code)

    outputs_parser = sync_sub.add_parser(
        "outputs",
        help="Pull output files from the cluster",
    )

    mode_group = outputs_parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--all",
        action="store_const",
        const="all",
        dest="mode",
        help="Sync all outputs",
    )
    mode_group.add_argument(
        "--visualization",
        action="store_const",
        const="visualization",
        dest="mode",
        help="Sync only visualization outputs (default)",
    )
    mode_group.add_argument(
        "--processed",
        action="store_const",
        const="processed",
        dest="mode",
        help="Sync only processed outputs",
    )

    outputs_parser.add_argument(
        "--formats",
        type=str,
        default=None,
        help=(
            "Comma-separated list of file formats to sync "
            "(pdf,png,tex,csv,json,parquet,yaml,all)"
        ),
    )
    outputs_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview changes without syncing",
    )
    outputs_parser.add_argument(
        "--delete",
        action="store_true",
        default=False,
        help="Delete local files not present on cluster",
    )
    outputs_parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Show detailed output",
    )
    outputs_parser.add_argument(
        "--show-tree",
        action="store_true",
        default=False,
        help="Display directory tree after sync",
    )
    outputs_parser.set_defaults(func=_cmd_sync_outputs)

    cp_parser = sync_sub.add_parser(
        "cp",
        help="Copy files between local and cluster",
    )
    cp_parser.add_argument("src", help="Source path (local or user@cluster:remote)")
    cp_parser.add_argument(
        "dst",
        help="Destination path (local or user@cluster:remote)",
    )
    cp_parser.add_argument(
        "--recursive",
        "-r",
        action="store_true",
        default=False,
        help="Copy directories recursively",
    )
    cp_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Preview the copy operation",
    )
    cp_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show detailed output",
    )
    cp_parser.set_defaults(func=_cmd_sync_cp)


def _build_tui_parser(subparsers: argparse._SubParsersAction) -> None:
    """Build the 'tui' subcommand."""
    tui_parser = subparsers.add_parser(
        "tui",
        help="Launch the interactive cluster management TUI",
    )
    tui_parser.add_argument(
        "--phone",
        action="store_true",
        default=False,
        help="Optimize display for phone screens",
    )
    tui_parser.add_argument(
        "--refresh",
        type=int,
        default=5,
        metavar="N",
        help="Refresh interval in seconds (default: 5)",
    )
    tui_parser.add_argument(
        "--all-users",
        action="store_true",
        default=False,
        help="Show jobs for all cluster users",
    )
    tui_parser.set_defaults(func=_cmd_tui)


def _build_launch_parser(subparsers: argparse._SubParsersAction) -> None:
    """Build the 'launch' subcommand."""
    launch_parser = subparsers.add_parser(
        "launch",
        help="Submit a script as a SLURM job on the cluster",
    )
    launch_parser.add_argument(
        "script",
        help="Path to the Python script to submit",
    )
    launch_parser.add_argument(
        "--run-from",
        choices=["local", "cluster"],
        default="cluster",
        help="Execution target (default: cluster)",
    )
    launch_parser.add_argument(
        "--partition",
        default="cpu_shared",
        help="SLURM partition (default: cpu_shared)",
    )
    launch_parser.add_argument(
        "--slurm-cpus",
        type=int,
        default=16,
        metavar="N",
        help="Number of CPUs per task (default: 16)",
    )
    launch_parser.add_argument(
        "--slurm-mem",
        default="64G",
        help="Memory allocation per job (default: 64G)",
    )
    launch_parser.add_argument(
        "--slurm-time",
        default="04:00:00",
        help="Wall-clock time limit (default: 04:00:00)",
    )
    launch_parser.add_argument(
        "--sync",
        action="store_true",
        default=False,
        help="Auto-sync code before submitting",
    )
    launch_parser.set_defaults(func=_cmd_launch)


def _build_serve_parser(subparsers: argparse._SubParsersAction) -> None:
    """Build the 'serve' subcommand."""
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start a server for remote phone access",
    )
    serve_sub = serve_parser.add_subparsers(
        dest="serve_command", help="Server lifecycle commands", required=True
    )

    start_parser = serve_sub.add_parser(
        "start", help="Start tmux + ttyd phone access for the cluster TUI"
    )
    start_parser.add_argument(
        "--port",
        type=int,
        default=7681,
        metavar="PORT",
        help=(
            "ttyd port for phone access (default: 7681; env: CLUSTER_KIT_PHONE_PORT)"
        ),
    )
    start_parser.add_argument(
        "--session-name",
        default="cluster-kit-phone",
        help=(
            "tmux session name for the cluster TUI "
            "(default: cluster-kit-phone; env: CLUSTER_KIT_PHONE_SESSION_NAME)"
        ),
    )
    start_parser.add_argument(
        "--cluster-tui-command",
        default=argparse.SUPPRESS,
        help=(
            "command started inside tmux "
            "(env: CLUSTER_KIT_PHONE_COMMAND; "
            "wins over --phone-ui when explicitly provided)"
        ),
    )
    start_parser.add_argument(
        "--phone-ui",
        action="store_true",
        help=(
            "start the phone-oriented Cluster TUI command "
            "when no explicit --cluster-tui-command is provided"
        ),
    )
    start_parser.add_argument(
        "--qa-safe-mode",
        action="store_true",
        default=False,
        help=(
            "route cancel/sync through harmless QA-safe stubs "
            "(env: CLUSTER_KIT_QA_SAFE_MODE=1)"
        ),
    )
    start_parser.set_defaults(func=_cmd_serve)

    status_parser = serve_sub.add_parser(
        "status", help="Show tmux + ttyd phone-access status"
    )
    status_parser.add_argument(
        "--port",
        type=int,
        default=7681,
        metavar="PORT",
        help=(
            "ttyd port for phone access (default: 7681; env: CLUSTER_KIT_PHONE_PORT)"
        ),
    )
    status_parser.add_argument(
        "--session-name",
        default="cluster-kit-phone",
        help=(
            "tmux session name for the cluster TUI "
            "(default: cluster-kit-phone; env: CLUSTER_KIT_PHONE_SESSION_NAME)"
        ),
    )
    status_parser.add_argument(
        "--cluster-tui-command",
        default=argparse.SUPPRESS,
        help=(
            "command started inside tmux "
            "(env: CLUSTER_KIT_PHONE_COMMAND; "
            "wins over --phone-ui when explicitly provided)"
        ),
    )
    status_parser.add_argument(
        "--phone-ui",
        action="store_true",
        help=(
            "start the phone-oriented Cluster TUI command "
            "when no explicit --cluster-tui-command is provided"
        ),
    )
    status_parser.add_argument(
        "--qa-safe-mode",
        action="store_true",
        default=False,
        help=(
            "route cancel/sync through harmless QA-safe stubs "
            "(env: CLUSTER_KIT_QA_SAFE_MODE=1)"
        ),
    )
    status_parser.set_defaults(func=_cmd_serve)

    stop_parser = serve_sub.add_parser("stop", help="Stop tmux + ttyd phone access")
    stop_parser.add_argument(
        "--port",
        type=int,
        default=7681,
        metavar="PORT",
        help=(
            "ttyd port for phone access (default: 7681; env: CLUSTER_KIT_PHONE_PORT)"
        ),
    )
    stop_parser.add_argument(
        "--session-name",
        default="cluster-kit-phone",
        help=(
            "tmux session name for the cluster TUI "
            "(default: cluster-kit-phone; env: CLUSTER_KIT_PHONE_SESSION_NAME)"
        ),
    )
    stop_parser.add_argument(
        "--cluster-tui-command",
        default=argparse.SUPPRESS,
        help=(
            "command started inside tmux "
            "(env: CLUSTER_KIT_PHONE_COMMAND; "
            "wins over --phone-ui when explicitly provided)"
        ),
    )
    stop_parser.add_argument(
        "--phone-ui",
        action="store_true",
        help=(
            "start the phone-oriented Cluster TUI command "
            "when no explicit --cluster-tui-command is provided"
        ),
    )
    stop_parser.add_argument(
        "--qa-safe-mode",
        action="store_true",
        default=False,
        help=(
            "route cancel/sync through harmless QA-safe stubs "
            "(env: CLUSTER_KIT_QA_SAFE_MODE=1)"
        ),
    )
    stop_parser.set_defaults(func=_cmd_serve)


def build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="cluster-kit",
        description=(
            "CLI toolkit for cluster management, code sync, and SLURM job submission"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        action="store_true",
        default=False,
        help="Display current cluster configuration and exit",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    _build_sync_parser(subparsers)
    _build_tui_parser(subparsers)
    _build_launch_parser(subparsers)
    _build_serve_parser(subparsers)

    return parser


def main() -> None:
    """Main entry point for the cluster-kit CLI."""
    parser = build_parser()
    args = parser.parse_args()

    if args.config:
        _cmd_config(args)
        sys.exit(0)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.parse_args([args.command, "--help"])
