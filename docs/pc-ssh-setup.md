# Windows PC (WSL) SSH Setup

One-time setup to use a Windows PC as a cluster-kit ssh-executor target over a
VPN (e.g. FortiClient). All kit primitives (git, `setsid`, process-group
kills, POSIX quoting, `uv`) need a POSIX shell, so **sshd runs inside WSL**,
not Windows OpenSSH Server — the latter executes remote commands through
`cmd.exe`, which breaks every one of those primitives.

## 1. WSL: sshd with systemd

Inside the WSL distro (Ubuntu assumed):

```bash
# Enable systemd so sshd starts with the VM
sudo tee /etc/wsl.conf > /dev/null << 'EOF'
[boot]
systemd=true
EOF

sudo apt update && sudo apt install -y openssh-server git

# Key-only auth on a non-default port (avoids clashing with any Windows sshd)
sudo sed -i 's/^#\?Port .*/Port 2222/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl enable ssh
```

Then from PowerShell: `wsl --shutdown` and reopen WSL so systemd + sshd come up.

## 2. Windows networking

**Windows 11 (recommended):** mirrored networking makes WSL share the host's
IP — no port forwarding needed. In `%UserProfile%\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

Then `wsl --shutdown` and reopen. Add a firewall rule (admin PowerShell):

```powershell
New-NetFirewallRule -DisplayName "WSL sshd" -Direction Inbound -Protocol TCP -LocalPort 2222 -Action Allow
```

**Windows 10 fallback (NAT mode):** forward the port to the WSL IP (which
changes per boot, so schedule the refresh at logon via Task Scheduler):

```powershell
$wslIp = (wsl hostname -I).Trim().Split(" ")[0]
netsh interface portproxy delete v4tov4 listenport=2222 2>$null
netsh interface portproxy add v4tov4 listenport=2222 connectaddress=$wslIp connectport=2222
```

## 3. Keep the machine and VM available

- **Power settings**: never sleep on AC power (`powercfg /change standby-timeout-ac 0`).
  On Modern Standby laptops also check that the network stays connected in
  standby; otherwise detached jobs surface as `DIED`.
- **WSL VM lifetime**: the VM stops when nothing runs in it. Add a Task
  Scheduler job at logon running `wsl.exe -d Ubuntu -e /bin/true` so systemd
  (and sshd) boot with Windows. While a detached job runs, the VM stays up.
- Detached jobs and workflow orchestrators die if the VM stops or Windows
  sleeps — never silently: they show up as `DIED` (jobs) or `LOST` (workflow).

## 4. Keys and Mac-side SSH config

On the Mac:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_pc
```

Append `~/.ssh/id_ed25519_pc.pub` to `~/.ssh/authorized_keys` **inside WSL**
(`chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys`). Then in the Mac's
`~/.ssh/config`:

```
Host pc
  HostName <pc-vpn-ip-or-hostname>
  Port 2222
  User <wsl-user>
  IdentityFile ~/.ssh/id_ed25519_pc
  ConnectTimeout 5
  ServerAliveInterval 30
```

Note: the PC's VPN-reachable IP may differ from its LAN IP — check the
FortiClient/intranet address. The kit does not automate the VPN; when the
host is unreachable it fails fast with a "connect the VPN" hint.

Verify: `ssh pc 'uname -a'` (with the VPN connected).

## 5. GitHub clone + uv on the PC

Inside WSL:

```bash
# GitHub auth from the PC (so the clone can pull/push)
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_github
# add the pubkey at https://github.com/settings/keys

mkdir -p ~/GitHub && cd ~/GitHub
git clone git@github.com:<user>/<repo>.git

# uv (installs to ~/.local/bin, which the kit adds to PATH for jobs)
curl -LsSf https://astral.sh/uv/install.sh | sh
cd ~/GitHub/<repo> && uv sync
```

## 6. Profile in each repo's `.env`

```bash
CLUSTER_PC_EXECUTOR=ssh
CLUSTER_PC_SYNC_MODE=git
CLUSTER_PC_HOST=pc
CLUSTER_PC_USER=<wsl-user>
CLUSTER_PC_REMOTE_BASE=/home/<wsl-user>/GitHub/<repo>
CLUSTER_PC_SSH_KEY=~/.ssh/id_ed25519_pc
```

Check with `cluster-kit -p pc --config`, then see
[ssh-executor.md](ssh-executor.md) for daily usage.
