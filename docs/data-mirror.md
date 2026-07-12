# Data mirror: cluster ⇄ PC

`cluster-kit sync mirror` keeps dataset directories identical on the cluster
and the PC so workflows can run interchangeably on either (`--run-from
cluster` or `pc`).

## How it works

- Two-pass **union rsync** per dataset: cluster→pc then pc→cluster, with
  `-az --update` and never `--delete`. Newest mtime wins, nothing is deleted,
  both sides converge to the union. Safe for append-mostly data.
- rsync runs **on the PC** against the cluster (direct CEMFI LAN,
  `cluster_from_pc` address). Data never routes through the Mac.
- For a real (non-dry-run) transfer, the Mac submits the combined two-pass
  rsync as a **detached job on the PC** (the same setsid+nohup mechanism as
  `cluster-kit job submit`) and polls it to completion, instead of blocking
  on one held-open SSH connection for the whole transfer. GB-scale first
  syncs can run for hours, and a single foreground SSH call was observed to
  drop with OpenSSH's own "Timeout, server not responding" partway through —
  the transfer itself was unaffected (it's PC↔cluster, unrelated to the
  Mac↔PC supervisory connection) but the Mac lost track of it. Detached +
  polled means a dropped connection only interrupts polling; the job keeps
  running on the PC and the next poll picks it back up. The job also shows
  up in the TUI's PC-jobs panel like any other detached job. Dry runs stay
  synchronous (they only stat/compare files, proportional to file count, not
  data volume).
- After each dataset completes, the result is stamped into
  `~/.cache/cluster-kit/mirror_state.json`. Both TUIs render this as the
  `⇄` mirror line (green = fresh, yellow = stale/never, red = last run failed).

## Manifest

Lives in the consuming repo (e.g. `whales/mirror.yaml`); run the command from
that repo root:

```yaml
cluster_from_pc: j-vill36@192.168.1.61   # how the PC addresses the cluster
datasets:
  whale_outputs:
    cluster: /mnt/slurm-beegfs/Users/j-vill36/scripts_whales/output
    pc: /home/j-vill36/GitHub/whales/output
    exclude: []                          # optional rsync --exclude patterns
```

Adding a dataset = one more block, zero code.

## Usage

```bash
cluster-kit sync mirror --dry-run --verbose   # preview, writes no state
cluster-kit sync mirror                       # mirror all datasets
cluster-kit sync mirror --dataset whale_outputs
```

## Prerequisites (one-time, on the PC)

```bash
ssh pc 'command -v rsync'                                        # rsync in WSL
ssh pc 'ssh -o BatchMode=yes j-vill36@192.168.1.61 true && echo OK'  # key auth to cluster
```

If the second check fails, the PC's WSL needs a passphrase-less key (or agent)
authorized on the cluster.

## Schedule (hourly, Mac launchd)

`~/Library/LaunchAgents/com.jesusvillota.cluster-kit-mirror.plist` runs
`cd ~/GitHub/whales && uv run cluster-kit sync mirror` every 3600 s, logging to
`~/.cache/cluster-kit/mirror.log`.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jesusvillota.cluster-kit-mirror.plist
launchctl kickstart gui/$(id -u)/com.jesusvillota.cluster-kit-mirror   # run now
```

Runs while the Mac is asleep are skipped; the TUI mirror line turns stale, and
a manual `cluster-kit sync mirror` catches up. The first-ever mirror of a large
tree can take hours; because it now runs as a detached PC job, sleeping the
Mac mid-poll doesn't kill the transfer — just re-run `cluster-kit sync mirror`
(or check `cluster-kit -p pc job list`) once the Mac wakes.
