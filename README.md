# gluster-shadow-ha — HA GlusterFS + Samba cluster on Ubuntu

A two-node high-availability NAS/compute cluster:
- **GlusterFS** replica-3 (arbiter) shared filesystem  
- **CTDB + Samba** floating-VIP SMB share, Windows-accessible  
- **Systemd boot-chain** hardened against client/server race conditions  
- **Watchdog timer** for automatic mount recovery  
- **Overlay filesystem** (shadow mode) on node 0 for write isolation  
- Optional **LLM stack** (LiteLLM router + Gemini shim or local Ollama)

Tested on **Ubuntu 24.04 LTS (systemd 255)**.  
Compatible with Ubuntu 22.04 (minor differences noted).

---

## Architecture

> Full component deep-dive and partition behaviour: **[docs/architecture.md](docs/architecture.md)**

```
Win/Linux/macOS client  ──  network drive  ──►  \\<VIP>\myshare   (CIFS/Samba)
                                              │
                                     CTDB VIP floats between
                                       node0 and node1
                                              │
               ┌──────────────────────────────┴─────────────────────────────┐
               │ node0 (PRIMARY)                         │ node1 (SECONDARY) │
               │ smbd → /srv/shadow/data                 │ smbd → /cluster-shared/data
               │          │                              │          │
               │     overlayfs:                          │     GlusterFS FUSE
               │     upper=/opt/shadow-layer/data        │     /cluster-shared
               │     lower=/cluster-shared               │
               └──────────────────┬──────────────────────┘
                                  │
                         GlusterFS  mydata
                         replica-3 arbiter volume
                    ┌─────────────┼────────────────┐
              node0 brick    node1 brick       arbiter brick
         /data/gluster/   /data/gluster/    (Windows host or
          brick0/vol        brick0/vol       3rd Linux node)
```

---

## Hardware requirements

| Role | Min spec | Notes |
|---|---|---|
| node0 | 4-core CPU, 16 GB RAM, 2 disks | Primary brick + overlay; AMD GPU optional for LLM |
| node1 | 4-core CPU, 8 GB RAM, 2 disks | Secondary brick |
| arbiter | Any Linux/Windows host | Only stores metadata (~1 GB), no data traffic |

**Disks per node:**
- Disk 1 — OS (`/`) — any size
- Disk 2 — GlusterFS brick — size determines shared storage capacity

All nodes must be on the same L2 subnet for CTDB.

---

## Network layout

```
NODE0_IP   = 192.168.1.10   # primary IP of node0
NODE1_IP   = 192.168.1.11   # primary IP of node1
ARBITER_IP = 192.168.1.12   # arbiter host — leave empty to skip (replica-2)
VIP        = 192.168.1.100  # CTDB floating VIP — must be unused at start
```

Both nodes need the VIP IP to be on the same NIC as their primary IP.  
CTDB will manage adding/removing it automatically.

---

## Quick start

```bash
# On node0:
curl -O https://raw.githubusercontent.com/happy-pills-dev/gluster-shadow-ha/main/install.sh
chmod +x install.sh
sudo ./install.sh --role node0

# On node1 (after node0 completes phase 1-3):
sudo ./install.sh --role node1

# From node0 — create GlusterFS volume (run ONCE after both nodes are ready):
sudo ./install.sh --role node0 --step gluster-volume-create

# Verify both nodes:
./install.sh --verify
```

Edit the `VARIABLES` block at the top of `install.sh` before running.

---

## Step-by-step guide

### Phase 0 — prepare both nodes

Run on **each node**:

```bash
# 1. Update + install required packages
apt update && apt upgrade -y
apt install -y \
    glusterfs-server glusterfs-client \
    ctdb samba samba-common-bin \
    lvm2 xfsprogs \
    net-tools iproute2 curl wget \
    mailutils postfix   # optional, for watchdog alerts

# 2. Enable services (do NOT start glusterd yet)
systemctl enable glusterd
systemctl enable ctdb
systemctl enable smbd nmbd
```

### Phase 1 — prepare the GlusterFS brick disk

Run on **each node** (substitute your block device):

```bash
BRICK_DEV=/dev/sdb           # your dedicated brick disk
VG_NAME=gluster-vg
LV_NAME=brick0-lv
BRICK_MOUNT=/data/gluster/brick0

# Create LVM volume group + logical volume
pvcreate $BRICK_DEV
vgcreate $VG_NAME $BRICK_DEV
lvcreate -l 100%FREE -n $LV_NAME $VG_NAME

# Format as XFS (required by GlusterFS)
mkfs.xfs /dev/$VG_NAME/$LV_NAME

# Mount it persistently
mkdir -p $BRICK_MOUNT
echo "/dev/$VG_NAME/$LV_NAME  $BRICK_MOUNT  xfs  defaults  0 2" >> /etc/fstab
mount -a

# Create the volume subdirectory (GlusterFS requires a subdir, not the root)
mkdir -p $BRICK_MOUNT/vol
```

### Phase 2 — start GlusterFS and peer

```bash
# On node0:
systemctl start glusterd
gluster peer probe 192.168.1.11    # probe node1
gluster peer probe 192.168.1.12    # probe arbiter (skip if no arbiter)

# On node1:
systemctl start glusterd
gluster peer probe 192.168.1.10    # probe node0 (establishes bidirectional trust)

# Verify (from either node):
gluster peer status
# Should show: 2 Peers in state: Peer in Cluster (Connected)
```

### Phase 3 — create the GlusterFS volume

Run **once**, from node0:

```bash
gluster volume create mydata \
    replica 3 arbiter 1 \
    192.168.1.10:/data/gluster/brick0/vol \
    192.168.1.11:/data/gluster/brick0/vol \
    192.168.1.12:/data/gluster/arbiter \
    force

gluster volume start mydata
gluster volume status mydata
# All bricks should show "Y" in Online column
```

### Phase 4 — mount the GlusterFS volume on both nodes

**Critical:** use `x-systemd.requires` so the mount waits for glusterd.

```bash
# On node0 — add to /etc/fstab:
echo "192.168.1.10:/mydata  /cluster-shared  glusterfs  \
defaults,_netdev,x-systemd.requires=glusterd.service,x-systemd.after=glusterd.service,\
x-systemd.mount-timeout=30s,backup-volfile-servers=192.168.1.11  0 0" >> /etc/fstab

# On node1 — add to /etc/fstab (source IP is node1's own IP):
echo "192.168.1.11:/mydata  /cluster-shared  glusterfs  \
defaults,_netdev,x-systemd.requires=glusterd.service,x-systemd.after=glusterd.service,\
x-systemd.mount-timeout=30s,backup-volfile-servers=192.168.1.10  0 0" >> /etc/fstab

# Create mountpoint and mount:
mkdir -p /cluster-shared
systemctl daemon-reload
mount /cluster-shared
mountpoint /cluster-shared   # must print "is a mountpoint"
```

> **Why `x-systemd.requires=glusterd.service`?**  
> Without it, `systemd-fstab-generator` creates a mount unit that only depends on
> `network-online.target` — not on `glusterd.service`. On this topology the client
> connects to *its own* glusterd. The mount fires before glusterd is ready and
> silently fails. This flag closes that race window.

**node1 — manual `.mount` unit override (if needed):**  
If node1 already has a manual systemd `.mount` unit at
`/etc/systemd/system/cluster\x2dshared.mount`, it overrides the fstab generator.
Ensure that unit does **NOT** contain `_netdev` in its `[Mount] Options`
(see `configs/systemd/cluster-shared.mount` for the canonical template):

```ini
# /etc/systemd/system/cluster\x2dshared.mount
[Unit]
Description=Cluster Shared GlusterFS Volume
After=network-online.target glusterd.service
Wants=network-online.target
Requires=glusterd.service
Before=ctdb.service smbd.service nmbd.service

[Mount]
What=<NODE1_IP>:/<VOLUME_NAME>
Where=/cluster-shared
Type=glusterfs
# DO NOT add _netdev here — it injects an implicit ConditionNetworkConnectivity
# in systemd 255 that races at boot. Use explicit After=/Requires= instead.
Options=defaults,backup-volfile-servers=<NODE0_IP>,log-level=WARNING,log-file=/var/log/glusterfs/cluster-shared-mount.log
TimeoutSec=30

[Install]
WantedBy=multi-user.target
```

### Phase 5 — harden glusterd boot ordering (node0)

This ensures the GlusterFS volume is force-started before dependent mounts fire:

```bash
mkdir -p /etc/systemd/system/glusterd.service.d
cat > /etc/systemd/system/glusterd.service.d/wait-for-internal-net.conf << 'EOF'
[Unit]
After=sys-subsystem-net-devices-<YOUR_NIC>.device network-online.target
Wants=sys-subsystem-net-devices-<YOUR_NIC>.device

[Service]
ExecStartPost=/bin/bash -c 'sleep 5 && /usr/sbin/gluster volume start mydata force 2>&1 | logger -t glusterd-poststart || true'
EOF

systemctl daemon-reload
```

> Replace `<YOUR_NIC>` with your NIC name (run `ip link` to find it, e.g. `eth0`, `ens3`, `enp3s0`).

### Phase 6 — CTDB

CTDB manages the floating VIP and ensures Samba is coordinated across nodes.

```bash
# On both nodes — write the public addresses file (same on both):
cat > /etc/ctdb/public_addresses << 'EOF'
192.168.1.100/24 eth0
EOF
# Replace 192.168.1.100 with your VIP and eth0 with your NIC name

# Write the nodes file (same on both):
cat > /etc/ctdb/nodes << 'EOF'
192.168.1.10
192.168.1.11
EOF

# Minimal ctdb.conf (Ubuntu 24.04):
cat > /etc/ctdb/ctdb.conf << 'EOF'
[logging]
location = file:/var/log/ctdb/ctdb.log
log level = NOTICE

[cluster]
transport = tcp
node address = <THIS_NODE_IP>    # change per node

[legacy]
lmaster role = yes
EOF

systemctl enable --now ctdb
ctdb status   # both nodes should eventually show OK
```

### Phase 7 — Samba

```bash
# /etc/samba/smb.conf — minimal HA config
cat > /etc/samba/smb.conf << 'EOF'
[global]
    workgroup = WORKGROUP
    server string = Cluster Node %h
    clustering = yes
    idmap config * : backend = tdb

    # CTDB + cluster-aware settings
    ctdbd socket = /var/run/ctdb/ctdbd.socket
    private dir = /var/lib/samba/private

[myshare]
    comment = Cluster Shared Storage
    # node0: path = /srv/shadow/data   (through shadow overlay, see Phase 10)
    # node1: path = /cluster-shared/data
    path = /cluster-shared/data
    browseable = yes
    read only = no
    valid users = @sambausers
    create mask = 0664
    directory mask = 0775
EOF

# On node0: override the path to use the overlay
sed -i 's|path = /cluster-shared/data|path = /srv/shadow/data|' /etc/samba/smb.conf

# Add samba user
smbpasswd -a <username>

testparm -s           # must return no errors
systemctl restart smbd nmbd
```

### Phase 8 — systemd: smbd must not serve stale content

Prevent Samba from starting if the cluster mount failed silently:

**node0** (depends on overlay setup service):
```bash
mkdir -p /etc/systemd/system/smbd.service.d
cat > /etc/systemd/system/smbd.service.d/cluster-shared-required.conf << 'EOF'
[Unit]
Requires=shadow-layer-setup.service
After=shadow-layer-setup.service
EOF
```

**node1** (depends directly on the mount unit):
```bash
mkdir -p /etc/systemd/system/smbd.service.d
cat > /etc/systemd/system/smbd.service.d/cluster-shared-required.conf << 'EOF'
[Unit]
After=cluster-shared.mount
Wants=cluster-shared.mount
EOF
```

```bash
systemctl daemon-reload
```

> **Why this matters:** Without this, smbd starts regardless of whether
> `/cluster-shared` is mounted. It serves an empty (or stub-content) overlay,
> clients see truncated directory listings, and there is no error anywhere.
> This converts a silent-fail-open into a loud-fail-closed.

### Phase 9 — watchdog timer (both nodes)

Catches runtime mount drops, network blips, FUSE crashes:

```bash
# The watchdog script
cat > /usr/local/sbin/cluster-shared-watchdog.sh << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
LOG=/var/log/cluster-shared-watchdog.log
log() { echo "[$(date -Iseconds)] $*" >> "$LOG"; }

if ! mountpoint -q /cluster-shared; then
    log "/cluster-shared NOT mounted — attempting recovery"
    # Route through systemctl, NOT raw 'mount', to respect systemd unit lifecycle
    if systemctl start 'cluster\x2dshared.mount' >> "$LOG" 2>&1; then
        log "mount OK — restarting samba"
        # node0: also restart shadow-layer-setup before smbd
        # systemctl restart shadow-layer-setup smbd nmbd >> "$LOG" 2>&1
        systemctl restart smbd nmbd >> "$LOG" 2>&1
        log "recovery complete"
    else
        log "mount FAILED — manual intervention required"
        echo "CLUSTER: /cluster-shared mount failed on $(hostname) at $(date)" | \
            mail -s "[CLUSTER] mount failure on $(hostname)" root 2>/dev/null || true
    fi
else
    log "OK — /cluster-shared is mounted"
fi
EOF
chmod +x /usr/local/sbin/cluster-shared-watchdog.sh

# The oneshot service unit
cat > /etc/systemd/system/cluster-shared-watchdog.service << 'EOF'
[Unit]
Description=Re-mount /cluster-shared if it dropped
After=glusterd.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/cluster-shared-watchdog.sh
EOF

# The timer unit
cat > /etc/systemd/system/cluster-shared-watchdog.timer << 'EOF'
[Unit]
Description=Check /cluster-shared mount health every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now cluster-shared-watchdog.timer
systemctl status cluster-shared-watchdog.timer
```

> **Use `systemctl start 'cluster\x2dshared.mount'` — not `mount /cluster-shared`.**  
> Raw `mount` bypasses systemd's unit lifecycle. If the unit has dependencies or
> is in a condition-skipped state, systemd may immediately unmount the FUSE
> filesystem after the script exits, creating an infinite 1-minute remount cycle.
> Routing through `systemctl start` respects dependency ordering and keeps
> the unit in the correct active state.

### Phase 10 — node0 overlay filesystem (shadow mode)

This allows node0 to serve an overlay where writes are buffered locally without
touching the GlusterFS lower layer during active sessions:

```bash
# Create the overlay upper dir + work dir
mkdir -p /opt/shadow-layer/data
mkdir -p /opt/shadow-layer/work

# Create the mount point
mkdir -p /srv/shadow/data

# The setup service mounts the overlay
cat > /etc/systemd/system/shadow-layer-setup.service << 'EOF'
[Unit]
Description=Mount shadow overlay filesystem
After=cluster-shared.mount
Requires=cluster-shared.mount
ConditionPathIsMountPoint=/cluster-shared

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/mount -t overlay overlay \
    -o lowerdir=/cluster-shared,upperdir=/opt/shadow-layer/data,workdir=/opt/shadow-layer/work \
    /srv/shadow
ExecStop=/bin/umount /srv/shadow

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now shadow-layer-setup
```

---

## Verification checklist

Run after every reboot or configuration change:

```bash
# Both nodes:
mountpoint /cluster-shared             && echo "PASS: cluster-shared mounted" || echo "FAIL"
ls /cluster-shared | wc -l            # should be > 0 and match between nodes
systemctl is-active glusterd           && echo "PASS: glusterd active"        || echo "FAIL"
systemctl is-active smbd               && echo "PASS: smbd active"            || echo "FAIL"
systemctl is-active cluster-shared-watchdog.timer && echo "PASS: watchdog active" || echo "FAIL"
tail -5 /var/log/cluster-shared-watchdog.log       # should show "OK" entries

# node0 only:
mountpoint /srv/shadow                 && echo "PASS: shadow overlay mounted"  || echo "FAIL"
systemctl is-active shadow-layer-setup && echo "PASS: overlay setup active"    || echo "FAIL"
ls /srv/shadow | wc -l                 # must match ls /cluster-shared | wc -l

# From Windows:
# dir Y:\    (or \\<VIP>\myshare) — must list full directory contents
```

---

## Reboot test procedure

Always test after any infrastructure change:

```bash
# Reboot node0, wait ~3 minutes, then verify:
sudo systemctl reboot
# ... reconnect ...
mountpoint /cluster-shared && ls /cluster-shared | wc -l
systemctl is-active shadow-layer-setup
ls /srv/shadow | wc -l
journalctl -b 0 | grep -i 'cluster.shared\|glusterd' | head -20

# Check there are NO "Condition check resulted in ... being skipped" messages:
journalctl -b 0 | grep -i 'condition.*skip\|skipped.*condition' | grep -i cluster
# Must return nothing.
```

---

## Lessons learned / known gotchas

> Full root-cause analysis for all failure modes: **[docs/gotchas.md](docs/gotchas.md)**

These are real failure modes discovered in production. Read before you build.

### 1. The boot-order race (the original incident)

**Problem:** The fstab `_netdev` mount fires after `network-online.target` but
before `glusterd.service` is ready. On self-referencing GlusterFS (client on
same host as server), the mount gets connection-refused and silently fails.

**Fix:** Always use `x-systemd.requires=glusterd.service` in the fstab Options,
OR write an explicit `.mount` unit with `Requires=glusterd.service`.

### 2. `_netdev` in a manual `.mount` unit injects an implicit Condition (systemd 255+)

**Problem:** In systemd 255 (Ubuntu 24.04), the `_netdev` flag in a manually-written
`.mount` unit's `[Mount] Options` field causes systemd to inject an implicit
`ConditionNetworkConnectivity` check. This condition is not visible in `systemctl cat`
output (no explicit `Condition*=` line), races against the network stack at boot,
and causes the unit to be silently skipped with:
```
Condition check resulted in cluster\x2dshared.mount being skipped.
```

When a watchdog then calls `mount /cluster-shared` (raw), systemd detects the
FUSE mount, tries to activate the unit, hits the Condition failure again, and
**unmounts the filesystem** — creating a 1-minute remount/unmount cycle that
looks like: watchdog logs "mount OK" but the mountpoint is empty 60 seconds later.

**Fix:** Never include `_netdev` in `[Mount] Options` of a manually-written
`.mount` unit. Use explicit `After=` and `Requires=` in `[Unit]` instead.

### 3. Watchdog must use `systemctl start`, not raw `mount`

**Problem:** `mount /cluster-shared` bypasses systemd's unit lifecycle.
If the mount unit's Condition fails, systemd sees an "untracked" FUSE
mount and cleans it up, making the watchdog's recovery ineffective.

**Fix:** Use `systemctl start 'cluster\x2dshared.mount'` in the watchdog.
This respects dependency ordering, avoids the Condition bypass problem, and
keeps the unit in the correct `active (mounted)` state.

### 4. Samba will serve empty (stub) content silently

**Problem:** `smbd.service` has no dependency on the GlusterFS mount or the
overlay setup service. If the mount fails at boot, smbd starts, serves the
overlay's upper directory (which may contain a stub tree from a prior run),
and Windows clients see partial directory listings with no error.

**Fix:** Add a `Requires=` or `After=` drop-in to `smbd.service.d/` that
makes smbd depend on the mount unit. Fail loud → operator notices fast.

### 5. glusterd takes ~2 minutes to start on a loaded node

**Problem:** On a node with many LVM volumes and a running Docker stack,
glusterd consistently takes 1.5–2 minutes to reach `active` state. Any
dependent mount unit fires and fails if it doesn't wait long enough.

**Fix:** The Layer 2 watchdog (`OnBootSec=2min`) coincides naturally with this
timeline. But also add an `ExecStartPost` to glusterd that force-starts the
volume, adding a small buffer before dependent services fire.

### 6. BIOS "Restore on AC loss = Power On" — `shutdown` reboots, it doesn't halt

If your servers have this BIOS setting (common on ASUS/MSI/ASRock boards for
power-failure resilience), `sudo shutdown -h now` will halt and then immediately
reboot 30 seconds later. To truly power off: send the halt, wait for shutdown,
then physically hold the power button or pull the PSU cable.

### 7. Always reboot-test after infrastructure changes

Both issues above were invisible during normal operation and only surfaced on
the first reboot after a configuration change. Add a mandatory reboot-test
to your change procedure: apply change → reboot the affected node → run the
verification checklist.

---

## Adapting for your environment

| Variable | Default | Change in `install.sh` |
|---|---|---|
| `NODE0_IP` | `192.168.1.10` | Your node0 primary IP |
| `NODE1_IP` | `192.168.1.11` | Your node1 primary IP |
| `ARBITER_IP` | `""` (empty) | Arbiter host IP, or empty for replica-2 |
| `VIP` | `192.168.1.100` | An unused IP on the same subnet |
| `VIP_NIC` | `eth0` | NIC for the VIP (`ip link` to find it) |
| `GLUSTER_VOL` | `mydata` | GlusterFS volume name |
| `BRICK_PATH` | `/data/gluster/brick0/vol` | Brick subdirectory |
| `CLUSTER_MOUNT` | `/cluster-shared` | Local mount point |
| `SMB_SHARE` | `myshare` | Samba share name |
| `SMB_WORKGROUP` | `WORKGROUP` | Workgroup name |

**Without an arbiter node:** Use `replica 2` instead of `replica 3 arbiter 1`
in the volume create command. Split-brain protection is weaker.

**Single-subnet (no CTDB):** If you don't need a floating VIP, skip Phases 6–7.
Each node serves its own fixed IP. Map two drive letters on Windows.

---

## Optional: LLM stack

LiteLLM + Gemini shim setup summary:

- **node1:11434** — Gemini shim container (OpenAI-compatible, Ollama-format API)
- **node0:14000** — LiteLLM router, routes model aliases to node1 or Vertex AI

The shim makes the LLM endpoint transparent to callers; switching between
local Ollama and Google Gemini is a one-file change in `docker-compose.yml`.

---

## File manifest

```
gluster-shadow-ha/
├── README.md                this guide
├── install.sh               automated installation script (idempotent)
├── verify.sh                post-install and post-reboot health checks
├── index.html               GitHub Pages landing page
├── docs/
│   ├── architecture.md      component deep-dive and partition behaviour
│   └── gotchas.md           all 8 known failure modes with root causes and fixes
├── configs/
│   ├── smb.conf             Samba config template
│   ├── ctdb.conf            CTDB config template
│   ├── public_addresses     CTDB VIP file template
│   ├── systemd/
│   │   ├── cluster-shared.mount         manual mount unit (node1, no _netdev)
│   │   └── smbd.service.d/
│   │       ├── override-node0.conf      smbd dependency drop-in (node0)
│   │       └── override-node1.conf      smbd dependency drop-in (node1)
│   └── watchdog/
│       ├── cluster-shared-watchdog.sh   recovery script (uses systemctl, not mount)
│       └── cluster-shared-watchdog.timer  systemd timer unit
└── .github/
    └── workflows/
        └── shellcheck.yml   CI: lint all shell scripts on push
```

---

## License

MIT — fork, adapt, contribute back.

## Contributing

Issues and PRs welcome. When reporting a problem, include:
- `gluster peer status`
- `gluster volume status mydata`
- `journalctl -b 0 | grep -E '(cluster.shared|glusterd|ctdb|smbd)'`
- Output of `verify.sh`
