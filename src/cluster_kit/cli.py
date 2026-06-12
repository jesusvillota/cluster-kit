"""CLI entry point for cluster-kit."""

import argparse
import os
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
    print(f"  executor:      {config.executor}")
    print(f"  sync_mode:     {config.sync_mode}")

    if errors:
        print("\n[WARN] Validation issues:")
        for err in errors:
            print(f"  - {err}")
    else:
        print("\n[OK] Configuration is valid.")


def _cmd_sync_code(args: argparse.Namespace) -> None:
    """Sync source code to the remote (rsync or git, per profile)."""
    from cluster_kit.config import get_sync_mode

    if get_sync_mode() == "git":
        from cluster_kit.sync.git_sync import GitSyncer

        syncer = GitSyncer(
            dry_run=args.dry_run,
            force=args.force,
            verbose=args.verbose,
        )
        if not syncer.sync():
            sys.exit(1)
        return

    if args.force:
        print(
            "[cluster-kit] --force only applies to git sync mode",
            file=sys.stderr,
        )
        sys.exit(2)

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
    from cluster_kit.config import get_executor

    if get_executor() != "slurm":
        print(
            "[cluster-kit] The TUI requires the SLURM executor; "
            "use `cluster-kit job list` for ssh-executor profiles.",
            file=sys.stderr,
        )
        sys.exit(1)

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
    """Submit a script as a SLURM job or a detached remote job."""
    script_path = args.script

    if args.run_from == "local":
        import subprocess

        result = subprocess.run([sys.executable, script_path])
        sys.exit(result.returncode)

    from cluster_kit.config import get_executor

    if get_executor() == "ssh":
        from cluster_kit.jobs import JobError, submit

        print(
            "[cluster-kit] ssh executor: --partition/--slurm-* flags are ignored"
        )
        try:
            handle = submit(f"uv run python {script_path}")
        except JobError as exc:
            print(f"[cluster-kit] {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"[cluster-kit] Submitted detached job {handle.job_id}")
        print(f"[cluster-kit] Log: {handle.log_path}")
        return

    from cluster_kit.launch.launcher import submit_job

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


def _cmd_env_create(args: argparse.Namespace) -> None:
    """Generate environment files from pyproject.toml."""
    from pathlib import Path

    from cluster_kit.env.builder import create_environment_files

    pyproject_path = Path(args.pyproject).resolve()
    output_dir = Path(args.output_dir).resolve()
    python_version = args.python_version if hasattr(args, "python_version") else None

    create_environment_files(
        pyproject_path=pyproject_path,
        output_dir=output_dir,
        python_version=python_version,
        include_dev=args.include_dev,
        dry_run=args.dry_run,
        partition=args.partition,
    )


def _cmd_env_launch(args: argparse.Namespace) -> None:
    """Upload environment files and submit env build job to cluster."""
    from pathlib import Path

    from cluster_kit.env.builder import launch_environment

    env_file = Path(args.env_file).resolve()
    slurm_file = Path(args.slurm_file).resolve()
    pyproject_path = Path(args.pyproject).resolve()
    python_version = args.python_version if hasattr(args, "python_version") else None

    launch_environment(
        env_file=env_file,
        slurm_file=slurm_file,
        pyproject_path=pyproject_path,
        wait=args.wait,
        check_interval=args.check_interval,
        python_version=python_version,
        partition=args.partition,
    )


def _cmd_workflow_run(args: argparse.Namespace) -> None:
    """Launch a YAML/TOML-defined workflow via the login-node orchestrator."""
    from pathlib import Path

    from cluster_kit.workflow import WorkflowError, submit_workflow

    try:
        submit_workflow(
            Path(args.workflow_file),
            dry_run=args.dry_run,
            project_root=args.project_root,
            sync=False if args.no_sync else None,
            dependency=args.dependency,
            worker_script=args.worker_script,
            max_concurrent=args.max_concurrent,
            poll_interval=args.poll_interval,
        )
    except WorkflowError as exc:
        print(f"[cluster-kit] Workflow error: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_workflow_status(args: argparse.Namespace) -> None:
    """Show the state of an orchestrated workflow run."""
    from cluster_kit.workflow import show_status
    from cluster_kit.workflow.remote import RemoteError

    try:
        show_status(args.run_id, show_log=args.log)
    except RemoteError as exc:
        print(f"[cluster-kit] Workflow error: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_workflow_cancel(args: argparse.Namespace) -> None:
    """Cancel an orchestrated workflow run."""
    from cluster_kit.workflow import cancel_run
    from cluster_kit.workflow.remote import RemoteError

    try:
        cancel_run(args.run_id)
    except RemoteError as exc:
        print(f"[cluster-kit] Workflow error: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_exec(args: argparse.Namespace) -> None:
    """Run a command synchronously on the remote, streaming output."""
    import shlex

    from cluster_kit.config import get_remote_base
    from cluster_kit.utils.ssh import (
        RemoteUnreachableError,
        ensure_reachable,
        stream_remote,
    )

    try:
        ensure_reachable()
    except RemoteUnreachableError as exc:
        print(f"[cluster-kit] {exc}", file=sys.stderr)
        sys.exit(1)

    base = shlex.quote(str(get_remote_base()))
    command = (
        f"cd {base} && "
        f'export PATH="$HOME/.local/bin:$PATH" && {args.remote_command}'
    )
    sys.exit(stream_remote(f"bash -c {shlex.quote(command)}"))


def _cmd_job(args: argparse.Namespace) -> None:
    """Detached-job operations (submit/list/status/logs/cancel)."""
    from rich import box
    from rich.console import Console
    from rich.table import Table

    from cluster_kit.jobs import (
        JobError,
        cancel,
        job_status,
        list_jobs,
        read_log,
        submit,
    )
    from cluster_kit.utils.ssh import RemoteUnreachableError

    console = Console()
    state_styles = {
        "RUNNING": "cyan",
        "COMPLETED": "green",
        "FAILED": "red",
        "CANCELLED": "yellow",
        "DIED": "red",
    }

    try:
        if args.job_command == "submit":
            handle = submit(args.command, name=args.name)
            console.print(
                f"[green][OK][/green] Submitted [bold]{handle.job_id}[/bold]\n"
                f"[cyan]Log:[/cyan] {handle.log_path}\n"
                f"Check with: [bold]cluster-kit job status {handle.job_id}[/bold]"
            )
        elif args.job_command == "list":
            jobs = list_jobs()
            table = Table(title="Detached jobs", box=box.ROUNDED)
            table.add_column("Job ID")
            table.add_column("State")
            table.add_column("Exit")
            table.add_column("Created")
            table.add_column("Command")
            for job in jobs:
                style = state_styles.get(job["state"], "")
                state = (
                    f"[{style}]{job['state']}[/{style}]" if style else job["state"]
                )
                table.add_row(
                    job["job_id"],
                    state,
                    job.get("rc") or "-",
                    job.get("created_at") or "-",
                    (job.get("command") or "-")[:60],
                )
            console.print(table)
        elif args.job_command == "status":
            info = job_status(args.job_id)
            style = state_styles.get(info["state"], "")
            state = (
                f"[{style}]{info['state']}[/{style}]" if style else info["state"]
            )
            console.print(
                f"[cyan]Job:[/cyan] {info['job_id']}\n"
                f"[cyan]State:[/cyan] {state}"
                + (f" (exit {info['rc']})" if info.get("rc") else "")
                + f"\n[cyan]PID:[/cyan] {info.get('pid') or '-'}\n"
                f"[cyan]Created:[/cyan] {info.get('created_at') or '-'}\n"
                f"[cyan]Git SHA:[/cyan] {info.get('git_sha') or '-'}\n"
                f"[cyan]Command:[/cyan] {info.get('command') or '-'}"
            )
            if info["state"] == "DIED":
                console.print(
                    "[yellow]No exit code and no live process — the machine "
                    "(or WSL VM) likely shut down or slept mid-run.[/yellow]"
                )
        elif args.job_command == "logs":
            output = read_log(args.job_id, lines=args.lines, follow=args.follow)
            if not args.follow:
                print(output, end="")
        elif args.job_command == "cancel":
            if cancel(args.job_id, force=args.force):
                console.print(
                    f"[green][OK][/green] Cancelled [bold]{args.job_id}[/bold]"
                )
            else:
                console.print(
                    f"[yellow]No signal delivered for {args.job_id} "
                    "(already finished?)[/yellow]"
                )
    except (JobError, RemoteUnreachableError) as exc:
        print(f"[cluster-kit] {exc}", file=sys.stderr)
        sys.exit(1)


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
    code_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Git sync mode only: reset the remote checkout to origin/<branch> "
            "(discards remote-side commits/changes; untracked files survive)"
        ),
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
        default=60,
        metavar="N",
        help="Refresh interval in seconds (default: 60)",
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


def _build_env_parser(subparsers: argparse._SubParsersAction) -> None:
    """Build the 'env' subcommand with create and launch sub-subcommands."""
    env_parser = subparsers.add_parser(
        "env",
        help="Manage cluster conda environment from pyproject.toml",
    )
    env_sub = env_parser.add_subparsers(
        dest="env_command", help="Environment commands", required=True
    )

    create_parser = env_sub.add_parser(
        "create",
        help="Generate environment.yml and conda_env.slurm from pyproject.toml",
    )
    create_parser.add_argument(
        "--pyproject",
        default="pyproject.toml",
        help="Path to pyproject.toml (default: ./pyproject.toml)",
    )
    create_parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to write generated files (default: current directory)",
    )
    create_parser.add_argument(
        "--python-version",
        default=None,
        metavar="VERSION",
        help="Override Python version (e.g. 3.10, 3.12)",
    )
    create_parser.add_argument(
        "--include-dev",
        action="store_true",
        default=False,
        help="Include dev optional-dependencies",
    )
    create_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print generated files instead of writing them",
    )
    create_parser.add_argument(
        "--partition",
        default=None,
        help="SLURM partition for env build job (default: from env or cpu_express)",
    )
    create_parser.set_defaults(func=_cmd_env_create)

    launch_parser = env_sub.add_parser(
        "launch",
        help="Upload environment files and submit env build job to cluster",
    )
    launch_parser.add_argument(
        "--pyproject",
        default="pyproject.toml",
        help="Path to pyproject.toml (used to auto-generate env files if missing)",
    )
    launch_parser.add_argument(
        "--env-file",
        default="environment.yml",
        help="Local path to environment.yml (default: ./environment.yml)",
    )
    launch_parser.add_argument(
        "--slurm-file",
        default="conda_env.slurm",
        help="Local path to conda_env.slurm (default: ./conda_env.slurm)",
    )
    launch_parser.add_argument(
        "--python-version",
        default=None,
        metavar="VERSION",
        help="Override Python version for auto-generation",
    )
    launch_parser.add_argument(
        "--wait",
        action="store_true",
        default=False,
        help="Wait for the job to complete and show status",
    )
    launch_parser.add_argument(
        "--check-interval",
        type=int,
        default=30,
        metavar="SECONDS",
        help="Seconds between status checks when --wait is set (default: 30)",
    )
    launch_parser.add_argument(
        "--partition",
        default=None,
        help="SLURM partition for env build job (default: from env or cpu_express)",
    )
    launch_parser.set_defaults(func=_cmd_env_launch)


def _build_workflow_parser(subparsers: argparse._SubParsersAction) -> None:
    """Build the 'workflow' subcommand with workflow operations."""
    workflow_parser = subparsers.add_parser(
        "workflow",
        help="Submit YAML/TOML-defined SLURM workflows",
    )
    workflow_sub = workflow_parser.add_subparsers(
        dest="workflow_command",
        help="Workflow commands",
        required=True,
    )

    run_parser = workflow_sub.add_parser(
        "run",
        help="Submit a workflow file",
    )
    run_parser.add_argument(
        "workflow_file",
        help="Path to the workflow file",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate and print the submission plan without submitting jobs",
    )
    run_parser.add_argument(
        "--project-root",
        default=None,
        help="Override the workflow project_root",
    )
    run_parser.add_argument(
        "--no-sync",
        action="store_true",
        default=False,
        help="Do not sync code before workflow submission",
    )
    run_parser.add_argument(
        "--dependency",
        choices=["afterok", "afterany"],
        default=None,
        help="Override workflow dependency mode",
    )
    run_parser.add_argument(
        "--worker-script",
        default=None,
        help="Override workflow worker script path",
    )
    run_parser.add_argument(
        "--max-concurrent",
        type=int,
        default=None,
        help=(
            "Max queued jobs (pending+running) at any time; defaults to the "
            "workflow's max_concurrent, then $CLUSTER_MAX_JOBS, then 4"
        ),
    )
    run_parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        help="Orchestrator poll interval in seconds (default: 30)",
    )
    run_parser.set_defaults(func=_cmd_workflow_run)

    status_parser = workflow_sub.add_parser(
        "status",
        help="Show the state of an orchestrated workflow run",
    )
    status_parser.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Run id (default: latest run)",
    )
    status_parser.add_argument(
        "--latest",
        action="store_true",
        default=False,
        help="Show the latest run (default when no run id is given)",
    )
    status_parser.add_argument(
        "--log",
        action="store_true",
        default=False,
        help="Also tail the orchestrator log",
    )
    status_parser.set_defaults(func=_cmd_workflow_status)

    cancel_parser = workflow_sub.add_parser(
        "cancel",
        help="Kill an orchestrated run and scancel its active jobs",
    )
    cancel_parser.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Run id (default: latest run)",
    )
    cancel_parser.add_argument(
        "--latest",
        action="store_true",
        default=False,
        help="Cancel the latest run (default when no run id is given)",
    )
    cancel_parser.set_defaults(func=_cmd_workflow_cancel)


def _build_job_parser(subparsers: argparse._SubParsersAction) -> None:
    """Build the 'job' subcommand (detached jobs on ssh-executor profiles)."""
    job_parser = subparsers.add_parser(
        "job",
        help="Manage detached jobs on an ssh-executor remote",
    )
    job_sub = job_parser.add_subparsers(
        dest="job_command", help="Job operations", required=True
    )

    submit_parser = job_sub.add_parser(
        "submit", help="Submit a command as a detached job"
    )
    submit_parser.add_argument(
        "command",
        help="Shell command to run from the remote project root",
    )
    submit_parser.add_argument(
        "--name",
        default=None,
        help="Job name (default: derived from the command)",
    )
    submit_parser.set_defaults(func=_cmd_job)

    list_parser = job_sub.add_parser("list", help="List jobs and their states")
    list_parser.set_defaults(func=_cmd_job)

    status_parser = job_sub.add_parser("status", help="Show one job's state")
    status_parser.add_argument("job_id", help="Job id")
    status_parser.set_defaults(func=_cmd_job)

    logs_parser = job_sub.add_parser("logs", help="Tail a job's log")
    logs_parser.add_argument("job_id", help="Job id")
    logs_parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=50,
        help="Number of log lines to show (default: 50)",
    )
    logs_parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        default=False,
        help="Follow the log (tail -f)",
    )
    logs_parser.set_defaults(func=_cmd_job)

    cancel_parser = job_sub.add_parser(
        "cancel", help="TERM a job's whole process group"
    )
    cancel_parser.add_argument("job_id", help="Job id")
    cancel_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Follow up with SIGKILL",
    )
    cancel_parser.set_defaults(func=_cmd_job)


def _build_exec_parser(subparsers: argparse._SubParsersAction) -> None:
    """Build the 'exec' subcommand (synchronous remote command)."""
    exec_parser = subparsers.add_parser(
        "exec",
        help="Run a command synchronously on the remote, streaming output",
    )
    exec_parser.add_argument(
        "remote_command",
        help="Shell command to run from the remote project root",
    )
    exec_parser.set_defaults(func=_cmd_exec)


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
    parser.add_argument(
        "-p",
        "--profile",
        default=None,
        metavar="NAME",
        help=(
            "Config profile: resolve CLUSTER_<NAME>_* env vars before "
            "CLUSTER_* (equivalent to setting CLUSTER_ENV)"
        ),
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    _build_sync_parser(subparsers)
    _build_env_parser(subparsers)
    _build_workflow_parser(subparsers)
    _build_tui_parser(subparsers)
    _build_launch_parser(subparsers)
    _build_job_parser(subparsers)
    _build_exec_parser(subparsers)
    _build_serve_parser(subparsers)

    return parser


def main() -> None:
    """Main entry point for the cluster-kit CLI."""
    parser = build_parser()
    args = parser.parse_args()

    if args.profile:
        from cluster_kit.config import reset_config_cache

        os.environ["CLUSTER_ENV"] = args.profile
        reset_config_cache()

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
