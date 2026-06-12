# SSH Executor (plain remote machines)

cluster-kit can target machines without SLURM — e.g. a Windows PC running WSL
behind a VPN — through the **ssh executor**. The same profile mechanism that
selects clusters selects executors: a profile with `CLUSTER_<P>_EXECUTOR=ssh`
runs jobs as detached processes on the machine instead of submitting sbatch.

Machine setup: [pc-ssh-setup.md](pc-ssh-setup.md).

## Profiles

```bash
# .env (per repo)
CLUSTER_HOST=cluster                          # default profile: SLURM cluster
CLUSTER_REMOTE_BASE=/mnt/.../scripts_whales

CLUSTER_PC_EXECUTOR=ssh                       # "pc" profile: WSL machine
CLUSTER_PC_SYNC_MODE=git
CLUSTER_PC_HOST=pc
CLUSTER_PC_USER=<wsl-user>
CLUSTER_PC_REMOTE_BASE=/home/<wsl-user>/GitHub/<repo>
CLUSTER_PC_SSH_KEY=~/.ssh/id_ed25519_pc
```

Select the profile per invocation with `-p/--profile` (sets `CLUSTER_ENV`):

```bash
cluster-kit -p pc --config
```

## Code sync (git mode)

With `SYNC_MODE=git` the remote holds a normal GitHub clone; `sync code`
means: verify the local tree is committed and pushed (interactive prompt
offers to commit+push), then on the remote `git fetch` + `git checkout
<local branch>` + `git pull --ff-only`. The remote checkout is never reset
implicitly; remote-side edits cause a loud failure.

```bash
cluster-kit -p pc sync code            # preflight + ff-only pull
cluster-kit -p pc sync code --dry-run
cluster-kit -p pc sync code --force    # explicit reset --hard origin/<branch>
```

## Synchronous exec

Runs from the remote project root with output streamed back; works on any
profile (handy on the cluster login node too). Shell syntax is allowed.

```bash
cluster-kit -p pc exec "uv run python -c 'import sys; print(sys.version)'"
cluster-kit -p pc exec "duckdb -c \"select count(*) from 'output/**/*.parquet'\""
```

## Detached jobs

sbatch-like UX without SLURM: the job survives the SSH session, lives in
`<remote_base>/.cluster_kit/jobs/<job_id>/` (wrapper script, pid, log, exit
code), and is killed as a whole process group on cancel.

```bash
cluster-kit -p pc job submit "uv run src/process.py --years 2015" --name panel
cluster-kit -p pc job list
cluster-kit -p pc job status panel_20260612-101500_3f2a
cluster-kit -p pc job logs panel_20260612-101500_3f2a -f
cluster-kit -p pc job cancel panel_20260612-101500_3f2a [--force]
```

States: `RUNNING`, `COMPLETED`, `FAILED` (non-zero exit), `CANCELLED`, and
`DIED` (no exit code and no live process — usually the WSL VM stopped or
Windows slept mid-run).

## Workflows

`workflow run` works unchanged: on an ssh profile the orchestrator is
uploaded to the machine itself and spawns each YAML job as a local process
group, honoring stage dependencies and `max_concurrent` (counted against this
run's live processes). SLURM resource keys (`partition`, `cpus`, `mem`,
`time`, `qos`, `texlive`) are ignored.

```bash
cluster-kit -p pc workflow run pipeline.yaml
cluster-kit -p pc workflow status --latest
cluster-kit -p pc workflow cancel --latest   # TERMs orchestrator + children
```

The TUI is SLURM-only; use `job list` / `workflow status` on ssh profiles.

## Manual integration checklist

Run once after setting up a machine:

1. VPN down: `cluster-kit -p pc exec "true"` → fails fast with the VPN hint.
2. VPN up: `cluster-kit -p pc --config` valid; `exec "uname -a"` streams.
3. `sync code` with a dirty tree → prompts commit+push → remote pulls ff-only.
4. `job submit "uv run python -c 'import time; time.sleep(60)'"`, close the
   terminal, `job status` still RUNNING, `job logs -f` streams, `job cancel`
   kills the group (verify with `exec "pgrep -f time.sleep || echo dead"`).
5. Two-stage `workflow run` completes; `workflow status --latest` renders.
6. From a consuming repo: a launcher main with `--run-from pc`.
