# Windows PC (WSL) SSH Setup

One-time setup to use a Windows PC as a cluster-kit ssh-executor target over a
VPN. All kit primitives (git, `setsid`, process-group kills, POSIX quoting,
`uv`) need a POSIX shell, so **sshd runs inside WSL**, not Windows OpenSSH
Server — the latter executes remote commands through `cmd.exe`, which breaks
every one of those primitives.

This guide was validated end-to-end on 2026-06-12 against `DC-1TL9R44`
(Windows 11, WSL2 Ubuntu, mirrored networking) reached from a Mac over the
CEMFI FortiClient VPN, with Tailscale as fallback. The
[Troubleshooting](#troubleshooting) section records the failure modes hit
during that setup.

## 0. Checks first (PowerShell)

```powershell
wsl --version        # need WSL >= 2.0 for mirrored networking; `wsl --update` if older
wsl -l -v            # note the distro's exact NAME (e.g. "Ubuntu", "Ubuntu-24.04")
```

## 1. WSL: sshd with systemd

Inside the WSL distro (Ubuntu assumed):

```bash
# Enable systemd so sshd starts with the VM
sudo tee /etc/wsl.conf > /dev/null << 'EOF'
[boot]
systemd=true
EOF

sudo apt update && sudo apt install -y openssh-server git

# Key-only auth on a non-default port, via a drop-in (don't edit sshd_config)
sudo tee /etc/ssh/sshd_config.d/cluster-kit.conf > /dev/null << 'EOF'
Port 2222
PasswordAuthentication no
PubkeyAuthentication yes
EOF
```

Then from PowerShell: `wsl --shutdown`, reopen WSL, and enable sshd:

```bash
systemctl is-system-running   # "running" or "degraded" both mean systemd is up
# Ubuntu 22.10+ uses systemd socket activation, which IGNORES the Port
# setting in sshd_config — disable the socket and run the service instead:
sudo systemctl disable --now ssh.socket 2>/dev/null
sudo systemctl enable --now ssh
ss -tln | grep 2222           # must show a listener
ssh -p 2222 localhost echo ok # "Permission denied (publickey)" = sshd answers (keys come later)
```

## 2. Windows networking

**Windows 11 (recommended): mirrored networking** — WSL shares the host's
interfaces (including LAN and Tailscale IPs), so sshd on 2222 is reachable on
any address Windows has. In `%UserProfile%\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

Then `wsl --shutdown` and reopen. Add **both** firewall rules (admin
PowerShell) — in mirrored mode, inbound traffic to WSL passes through the
Hyper-V firewall in addition to the Windows one:

```powershell
New-NetFirewallRule -DisplayName "WSL sshd" -Direction Inbound -Protocol TCP -LocalPort 2222 -Action Allow
New-NetFirewallHyperVRule -Name "WSL-sshd" -DisplayName "WSL sshd" -Direction Inbound -Protocol TCP -LocalPorts 2222 -Action Allow -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'
```

(The GUID is WSL's fixed VM-creator id. If `New-NetFirewallHyperVRule` does
not exist, the Windows/WSL version is too old for mirrored mode — use the
fallback below.)

Note: the `vEthernet (WSL (Hyper-V firewall))` adapter in `ipconfig` exists
in mirrored mode too — its presence does NOT mean NAT mode is active. To
check the actual mode, run `ip -4 addr` inside WSL: mirrored shows the same
IPs as Windows; NAT shows a private `172.x` address.

**Windows 10 fallback (NAT mode):** forward the port to the WSL IP (which
changes per boot, so schedule the refresh at logon via Task Scheduler):

```powershell
$wslIp = (wsl hostname -I).Trim().Split(" ")[0]
netsh interface portproxy delete v4tov4 listenport=2222 2>$null
netsh interface portproxy add v4tov4 listenport=2222 connectaddress=$wslIp connectport=2222
```

## 3. Keep the machine and VM available

- **Power settings** (admin PowerShell): `powercfg /change standby-timeout-ac 0`
  and `powercfg /change hibernate-timeout-ac 0`. On Modern Standby machines
  also check the network stays connected in standby.
- **WSL VM lifetime**: the VM stops when nothing runs in it. Register a logon
  task that holds it open. Two details matter: a one-shot command
  (`/bin/true`) lets the VM idle out again, so keep a process alive; and Task
  Scheduler kills tasks after 3 days unless `-ExecutionTimeLimit 0`:

  ```powershell
  $action   = New-ScheduledTaskAction -Execute "C:\Windows\System32\wsl.exe" -Argument "-d Ubuntu --exec sleep infinity"
  $trigger  = New-ScheduledTaskTrigger -AtLogOn
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
  Register-ScheduledTask -TaskName "WSL keepalive" -Action $action -Trigger $trigger -Settings $settings
  Start-ScheduledTask -TaskName "WSL keepalive"
  ```

  This fires at **logon**: after a reboot someone must log into Windows once
  (locking the screen afterwards is fine).
- Detached jobs and workflow orchestrators die if the VM stops or Windows
  sleeps — never silently: they show up as `DIED` (jobs) or `LOST` (workflow).

## 4. Choosing the address: corporate VPN vs Tailscale

Validated outcome: **if the client Mac already runs the corporate VPN
(FortiClient) and the PC sits on the same office subnet as the cluster,
prefer the PC's office LAN IP.** The VPN then carries the traffic natively —
no extra dependency (it is already up for the cluster) and no route hacks.
Keep Tailscale as a fallback for working from networks without the VPN.

Caveats learned the hard way:

- **FortiClient black-holes Tailscale IPs.** FortiClient pushes routes for
  `100.64/11` and `100.96/11`, which are *more specific* than Tailscale's
  `100.64/10` — so while the corporate VPN is up, traffic to any Tailscale
  address dies inside the Forti tunnel (`tailscale ping` still works because
  it bypasses OS routing; plain `ssh` times out). Workaround, per boot/VPN
  reconnect (not persistent):

  ```bash
  sudo route add -host <pc-tailscale-ip> -interface <tailscale-utun>
  # find the interface: route -n get <pc-tailscale-ip>  (before FortiClient hijacks it)
  ```

- **The corporate VPN blocks ICMP**: `ping <lan-ip>` fails even when SSH
  works. Test reachability with `nc -z <ip> 2222` or `ssh`, never ping.
- **The LAN IP is DHCP**: if `ssh pc` stops connecting after weeks, the lease
  probably changed. Recover the new address via the Tailscale fallback
  (`ssh pc-tailscale 'ip -4 addr'`) or `ipconfig` on the PC, or request a
  DHCP reservation from IT.

## 5. Keys and Mac-side SSH config

On the Mac (or reuse an existing key):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_pc
```

Append the `.pub` to `~/.ssh/authorized_keys` **inside WSL**
(`chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys`). Then in the Mac's
`~/.ssh/config`, one primary host plus the Tailscale fallback:

```
Host pc
  HostName <pc-office-lan-ip>          # e.g. 192.168.1.104, via corporate VPN
  Port 2222
  User <wsl-user>
  IdentityFile ~/.ssh/id_ed25519_pc
  AddKeysToAgent yes
  ForwardAgent yes
  ConnectTimeout 5
  ServerAliveInterval 30
  StrictHostKeyChecking accept-new

Host pc-tailscale
  HostName <pc-tailscale-ip>           # 100.x.y.z; needs the route hack while the VPN is up
  Port 2222
  User <wsl-user>
  IdentityFile ~/.ssh/id_ed25519_pc
  AddKeysToAgent yes
  ForwardAgent yes
  ConnectTimeout 5
  ServerAliveInterval 30
  StrictHostKeyChecking accept-new
```

`ForwardAgent yes` + `AddKeysToAgent yes` matter beyond convenience: every
cluster-kit SSH session then carries the Mac's agent, so **`git fetch/pull`
on the PC (the git sync mode) authenticates to GitHub through the Mac's
key** — no PC-side GitHub key is required for Mac-driven syncs.
`AddKeysToAgent` loads the key into the agent on first use, so forwarding
keeps working after reboots.

Verify: `ssh pc 'uname -a'` (with the VPN connected).

## 6. GitHub clone + uv on the PC

Inside WSL:

```bash
mkdir -p ~/GitHub && cd ~/GitHub
# With agent forwarding active (ssh -A or the config above), the clone can
# use the Mac's GitHub key directly. A PC-side key is only needed to
# commit/push FROM the PC when working there directly:
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""   # optional; add .pub at github.com/settings/keys
ssh-keyscan -t ed25519,rsa github.com >> ~/.ssh/known_hosts
git clone git@github.com:<user>/<repo>.git

# uv (installs to ~/.local/bin, which the kit adds to PATH for jobs;
# note: ~/.local/bin is NOT on the PATH of non-interactive SSH sessions)
curl -LsSf https://astral.sh/uv/install.sh | sh
cd ~/GitHub/<repo> && uv sync
```

## 7. Profile in each repo's `.env`

```bash
CLUSTER_PC_EXECUTOR=ssh
CLUSTER_PC_SYNC_MODE=git
CLUSTER_PC_HOST=pc
CLUSTER_PC_USER=<wsl-user>
CLUSTER_PC_REMOTE_BASE=/home/<wsl-user>/GitHub/<repo>
CLUSTER_PC_SSH_KEY=~/.ssh/id_ed25519_pc
```

Check with `cluster-kit -p pc --config`, then see
[ssh-executor.md](ssh-executor.md) for daily usage and the integration
checklist.

## Troubleshooting

Symptoms observed during the validated setup, with their actual causes:

| Symptom | Likely cause | Fix |
|---|---|---|
| `ssh pc` times out; `tailscale ping` works | FortiClient route conflict (it claims `100.64/11`, beating Tailscale's `/10`) | `sudo route add -host <ts-ip> -interface <tailscale-utun>`, or switch to the LAN IP (§4) |
| `ping` fails but `ssh` works (LAN IP) | Corporate VPN blocks ICMP | Normal — test with TCP, not ping |
| ICMP to Tailscale IP works, TCP 2222 times out | PC-side: WSL VM stopped, mirrored-mode binding hiccup, or Hyper-V firewall rule missing | `wsl -l -v` (Running?), reopen Ubuntu, re-check §2 rules; transient mirrored hiccups can self-heal — retry first |
| Worked for days, then `ssh pc` refuses/times out | DHCP gave the PC a new LAN IP | Recover via `ssh pc-tailscale` or `ipconfig`; update `~/.ssh/config`; ask IT for a reservation |
| sshd ignores `Port 2222`, still on 22 | Ubuntu `ssh.socket` socket activation overrides sshd_config | `sudo systemctl disable --now ssh.socket && sudo systemctl enable --now ssh` |
| `uv: command not found` in jobs | `~/.local/bin` not on non-interactive PATH | Already handled — kit wrappers export it; for manual `ssh pc '...'` add `export PATH="$HOME/.local/bin:$PATH"` |
| Jobs end as `DIED` / workflow jobs as `LOST` | WSL VM stopped or Windows slept mid-run | §3: keepalive task + power settings; check `powercfg /requests` |
| `git fetch` on the PC: permission denied | No agent forwarding on that session and no PC-side GitHub key | Use the §5 config (`ForwardAgent yes`) or add the PC key to GitHub |
| Keepalive task vanishes after 3 days | Task Scheduler default execution limit | Recreate with `-ExecutionTimeLimit 0` (§3) |
