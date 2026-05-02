# gluster-shadow-ha

A self-healing mirror of your working directory across two Linux machines —
a laptop and a home server, two office nodes, two bare-metal VMs — that keeps
working when they're separated and merges automatically when they reconnect.

Built on GlusterFS + Samba + CTDB. No cloud dependency. No sync client.
No conflict dialogs. Just a filesystem that heals itself.

---

## The core idea

Most two-node HA storage clusters face an unsolvable dilemma: when the link
between nodes breaks, you have to choose one of three bad options.

1. **Both nodes freeze** — refuse writes until quorum is restored. Safe but
   your storage is down.
2. **One node wins, one loses** — the minority node goes read-only. Half your
   capacity disappears.
3. **Both nodes write independently** — you get split-brain data corruption
   that requires manual resolution to clean up.

This stack takes a fourth path. By giving Node A a **shadow serving layer** —
a filesystem abstraction that sits between Samba and the GlusterFS volume —
the primary node can continue accepting reads and writes into a local buffer
even when the replication link is down. When the link comes back, GlusterFS's
built-in self-heal daemon reconciles both sides automatically. No quorum
freeze, no read-only degradation, no manual merge step.

```
NORMAL OPERATION
                  Node A (primary)              Node B (secondary)
                  ┌──────────────────┐          ┌──────────────────┐
  clients ──VIP──►│ Samba            │          │ Samba            │
                  │  /srv/shadow  ───┼──────────┼──► /cluster-share│
                  │  (shadow layer)  │◄ replicate►│  (direct mount) │
                  │  /cluster-share  │          │                  │
                  └──────────────────┘          └──────────────────┘

DURING PARTITION (link between nodes is down)
                  Node A                        Node B
                  ┌──────────────────┐          ┌──────────────────┐
  clients ──VIP──►│ Samba ✓ serving  │          │ Samba ✓ serving  │
                  │  /srv/shadow     │  X link  │  /cluster-share  │
                  │  writes → local  │  broken  │  writes → brick  │
                  │  shadow buffer   │          │                  │
                  └──────────────────┘          └──────────────────┘
                       ↓ link restored ↓
                  GlusterFS self-heal merges both sides
                  shadow buffer drains into replicated volume
                  both nodes back in sync — unattended
```

The only limit on how long Node A can operate independently is the size of the
local shadow buffer disk. For typical real-world partitions (minutes to hours),
this is effectively unlimited.

---

## What it is

A self-contained recipe for turning two ordinary Ubuntu servers into a
**resilient shared-storage cluster** that any machine on your network — Linux,
Windows, or macOS — can read and write as a normal network drive.

Under the hood it combines four proven Linux subsystems:

- **GlusterFS** replicates data across both nodes in real time. When both nodes
  are connected, every write on one is immediately mirrored to the other.
  When they're not, each node writes to its own brick and the self-heal daemon
  reconciles them on reconnect using last-write-wins by timestamp.

- **Shadow layer (overlayfs)** gives Node A a local write buffer on top of the
  GlusterFS mount. Writes land in the shadow layer first, stay readable
  immediately, and sync into the replicated volume as connectivity allows.
  Node A's Samba always serves through this layer — clients never see a
  degraded view.

- **CTDB** manages the floating IP that clients connect to. It tracks which
  node is healthy, moves the VIP automatically on failure, and coordinates
  Samba's cluster state so both nodes share a consistent view of open files
  and locks.

- **Systemd** wires the boot sequence with explicit dependencies and a
  recovery watchdog, so the full stack — GlusterFS, shadow layer, Samba —
  comes up in the right order after any reboot or power cut, without anyone
  logging in.

---

## What it can do

| Capability | Detail |
|---|---|
| Shared read/write storage | Both nodes replicate the same data in real time |
| Windows drive mapping | Any client maps `\\<VIP>\share` as a drive letter over SMB |
| Transparent failover | Node goes down → VIP moves → clients reconnect, no data loss |
| **Independent operation during partition** | **Node A keeps serving read/write from shadow buffer; Node B keeps serving from its brick — neither freezes** |
| **Automatic reconciliation on rejoin** | **GlusterFS self-heal merges both sides unattended; no manual resolution step** |
| Bounded by disk, not by time | Shadow buffer size = local disk capacity; typical partitions never come close to filling it |
| Survives reboots | Hardened systemd boot chain brings everything up in the right order, every time |
| Self-healing mounts | Watchdog timer detects and re-mounts dropped volumes within 60 seconds |
| Loud failures | Samba refuses to start if the shadow layer isn't ready — visible error, not silent empty directories |
| Optional LLM compute | Node B can host a local Ollama instance or a cloud-API shim; the shared filesystem makes models and outputs immediately accessible across nodes |

---

## Who it's for

In plain terms: **if you have two Linux machines that should share a working
directory and you want that directory to just work — whether the machines are
on the same network or not — this is the stack.**

**Laptop + home server**  
The most natural fit. Your laptop is Node A (primary). Your always-on home
server is Node B. At home, both are in sync in real time. You unplug and leave
— the laptop keeps writing to its shadow layer, nothing breaks. You come back,
plug in, and within seconds the home server has everything you wrote while you
were away. No Dropbox, no rsync cron job, no "which version is newer" dialog.
Works over your local network at full disk speed.

**Two office workstations**  
Both machines on the same desk or same switch. One is your main machine, one
is your build server or secondary workstation. They share a common project
drive. If the secondary goes down for maintenance, the primary keeps working.
When it comes back, it catches up silently.

**Development node + backup node**  
A small production server plus a standby. The production node uses the shadow
layer as a staging buffer — it can absorb writes during a secondary failure or
a planned maintenance window. Writes land immediately, replicate in the
background, drain when the secondary returns.

**Home lab NAS**  
Two repurposed mini-PCs or old laptops. A Windows machine on the same network
maps the share as a drive letter. Full HA SMB share with floating IP for under
the cost of a consumer NAS device, with no vendor lock-in and complete
visibility into everything that's happening.

---

## Why this approach

Most HA storage tutorials point you at Ceph, NFS-Ganesha with Pacemaker, or a
hyperconverged Kubernetes stack. Those are the right tools at scale. For a
two-node lab, home office, or small production workload they are significant
over-engineering: complex to configure, expensive to monitor, and fragile when
you only have two nodes (quorum becomes painful).

This stack takes the opposite bet:

**Use boring, well-understood tools with explicit configuration over smart
abstractions with hidden behaviour.**

- GlusterFS replica-2 (or replica-3 with a lightweight arbiter) is trivially
  simple to operate. You can inspect the state of every brick with one command.
  Recovery from a failed node is: start the node, watch it self-heal.

- CTDB has been the standard HA layer for Samba clusters since 2007. Every
  edge case in a two-node failover scenario has already been hit and documented.

- Systemd unit files with explicit `Requires=` and `After=` are readable by any
  Linux administrator. There is no hidden state machine; the dependency graph is
  exactly what you wrote.

The tradeoff is that you manage the pieces yourself. The payoff is that when
something goes wrong at 2 AM, you can read the journal, understand what
happened, and fix it in minutes — not hours of Kubernetes event archaeology.

---

## What makes it different

### Split-brain that doesn't require a decision

Every two-node storage cluster eventually faces a split-brain event: the
replication link breaks and both nodes have data the other doesn't. The
standard answers are all painful — freeze one node, require quorum (which
means a third node), or accept corruption and clean it up manually later.

This stack avoids the dilemma with the **primary/shadow architecture**:

- **Node A (primary)** serves Samba through a shadow layer — a filesystem
  abstraction that sits between the share and the GlusterFS volume. When the
  replication link is healthy, the shadow layer is transparent: clients see the
  full replicated dataset. When the link breaks, writes land in the shadow
  buffer on Node A's local disk instead. Node A keeps serving normally.
  Clients don't see any interruption.

- **Node B (secondary)** serves directly from its local GlusterFS brick.
  When the link breaks, it keeps serving from its own copy. Writes go to its
  brick. It keeps serving.

- **On reconnect**, GlusterFS's self-heal daemon compares both bricks,
  identifies divergent files, and reconciles them automatically using
  last-write-wins by timestamp (`cluster.favorite-child-policy: ctime`).
  The shadow buffer on Node A drains back into the replicated volume.
  No operator intervention. No manual merge. No data loss.

The only constraint is disk space: Node A can operate independently for as
long as the shadow buffer has room. For typical real-world scenarios — a
switch reboot, a NIC failure, a cable swap — the buffer never comes close
to filling.

**This is the main differentiating feature of this stack.** Most two-node HA
NAS setups degrade gracefully on a partition. This one continues operating
at full capacity on both nodes and reconciles silently when the partition heals.

---

### Hardened against the failure modes most guides miss

Beyond the split-brain design, this stack is built from a production system
that ran into real operational failures. Each one is documented, understood,
and baked into the installer so you don't hit it on your own.

**1. The silent boot-order race**  
On a self-hosting topology (client and server on the same machine), the default
`_netdev` fstab mount fires after the network comes up but *before* the local
`glusterd` is ready. The mount silently fails. Samba starts, serves an empty
directory, and there is no error anywhere. One fstab option closes the race —
the installer adds it.

**2. The hidden systemd condition (Ubuntu 24.04 / systemd 255)**  
The `_netdev` flag in a manually-written `.mount` unit injects an implicit
network-connectivity check that is invisible in the unit file. It fails
transiently at boot, the unit is silently skipped, and any watchdog that calls
raw `mount` to recover gets its FUSE mount immediately cleaned up by systemd —
creating an infinite loop where the watchdog logs "success" every minute but
the share stays empty. The fix is a single omission and a different recovery
command. Documented in full in the Known Gotchas section.

**3. Samba serves empty directories silently**  
Out of the box, `smbd` starts whether or not the underlying storage is mounted.
A missing mount → empty share → no errors → confused users. A two-line systemd
drop-in makes Samba refuse to start if the storage layer isn't ready, turning
a silent-fail-open into a loud-fail-closed.

**4. Recovery watchdogs must use `systemctl start`, not `mount`**  
Calling `mount` directly bypasses the systemd unit lifecycle. Systemd sees an
untracked FUSE mount and removes it. The watchdog has to route through
`systemctl start` to keep the mount in the correct active state. One command
difference; the wrong one loops forever.

**The result:** a cluster that works on a clean install, keeps working through
reboots, power cuts, network partitions, and node failures — and tells you
loudly when something actually needs attention.

---

## Architecture

```
Windows / Linux / macOS client
           │
           │  SMB  \\<VIP>\share
           ▼
    ┌─────────────────────────────────────────────────────────────┐
    │                    CTDB floating VIP                        │
    │          moves to the healthy node automatically            │
    └──────────────────┬──────────────────────────────────────────┘
                       │
         ┌─────────────┴──────────────┐
         │                            │
         ▼                            ▼
  ┌─────────────────┐          ┌─────────────────┐
  │   Node A        │          │   Node B        │
  │   PRIMARY       │          │   SECONDARY     │
  │                 │          │                 │
  │ smbd            │          │ smbd            │
  │  │              │          │  │              │
  │  ▼              │          │  ▼              │
  │ /srv/shadow ◄───┼── sync ──┼─ /cluster-share │
  │  │  shadow      │          │  (direct mount) │
  │  │  layer       │          │                 │
  │  ▼              │          └────────┬────────┘
  │ /cluster-share  │                   │
  │  (GlusterFS)    │                   │
  └────────┬────────┘                   │
           │                            │
           └──────── GlusterFS ─────────┘
              replica volume (self-heal on reconnect)
              ┌──────────────┬──────────────┐
           brick A        brick B      arbiter (optional)
       /data/gluster/  /data/gluster/  lightweight,
         brick0/vol      brick0/vol    metadata only

During partition:
  Node A → shadow layer buffers writes locally  →  keeps serving ✓
  Node B → writes to its own brick              →  keeps serving ✓
  On reconnect: self-heal merges both sides     →  unattended   ✓
```

Each node runs a full brick (data storage) and a FUSE client that mounts the
replicated volume locally at `/srv/share`. Samba serves that local mount.
CTDB keeps one floating IP active and coordinates Samba's cluster state.

---

## Requirements

**Hardware** — two machines (physical or VM) with:
- Ubuntu 24.04 LTS (also works on 22.04 with minor differences)
- 2+ CPU cores, 4+ GB RAM each
- Two disks per node: one for the OS, one dedicated for the GlusterFS brick
- 1 Gbps network (same L2 segment for CTDB heartbeat)

**Optional:** a third, lighter machine (or VM) as a GlusterFS arbiter to
prevent split-brain without the storage cost of a full third replica.

**Network:** three IP addresses on the same subnet:
- One for Node A
- One for Node B
- One floating VIP (must be unused — CTDB takes ownership)

---

## Quick start

```bash
# 1. Clone on both nodes
git clone https://github.com/happy-pills-dev/gluster-shadow-ha
cd gluster-shadow-ha

# 2. Edit the 15-line variables block at the top of install.sh
nano install.sh

# 3. Run on Node A
sudo ./install.sh --role node0

# 4. Run on Node B  (can run in parallel with step 3)
sudo ./install.sh --role node1

# 5. Create the GlusterFS volume — run ONCE from Node A
#    (after both nodes have completed the above)
sudo ./install.sh --role node0 --step gluster-volume-create

# 6. Add a Samba user and verify
sudo smbpasswd -a youruser
./verify.sh
```

That's it. Map a drive on Windows to `\\<VIP>\share`.

---

## Configuration

Open `install.sh` and change the block at the top:

```bash
NODE0_IP="192.168.1.10"    # Node A primary IP
NODE1_IP="192.168.1.11"    # Node B primary IP
ARBITER_IP="192.168.1.12"  # Optional — set to "" to skip
VIP="192.168.1.100"        # Floating IP — must be unused
VIP_NIC="eth0"             # NIC that carries the VIP on both nodes

GLUSTER_VOL="mydata"       # Name for the GlusterFS volume
BRICK_DEV="/dev/sdb"       # Dedicated block device for the brick
CLUSTER_MOUNT="/srv/share" # Where the volume is mounted locally

SMB_SHARE="myshare"        # Windows share name  \\VIP\myshare
SMB_WORKGROUP="WORKGROUP"  # Workgroup / domain
```

Everything else is derived from these values.

---

## Step-by-step (manual)

If you prefer to run phases individually rather than the full script:

```bash
sudo ./install.sh --role node0 --step packages
sudo ./install.sh --role node0 --step brick-disk
sudo ./install.sh --role node0 --step gluster-server
# -- wait for node1 to complete the same steps --
sudo ./install.sh --role node0 --step gluster-volume-create
sudo ./install.sh --role node0 --step fstab-mount
sudo ./install.sh --role node0 --step ctdb
sudo ./install.sh --role node0 --step samba
sudo ./install.sh --role node0 --step smbd-dropin
sudo ./install.sh --role node0 --step watchdog
```

Use `--dry-run` with any command to see what it would do without making changes.

---

## Verify

```bash
./verify.sh            # auto-detects this node's role
./verify.sh --json     # machine-readable output
./verify.sh --quiet    # only print failures (for cron monitoring)
```

Sample passing output:
```
gluster-shadow-ha verify — nodeA [node0] — 2026-05-02 12:15:00 UTC
  PASS  glusterd.service active
  PASS  gluster peer: at least 1 connected
  PASS  volume mydata is Started
  PASS  /srv/share is a mountpoint
  PASS  smbd.service active
  PASS  ctdb.service active
  PASS  cluster-shared-watchdog.timer active
  PASS  no Condition skip for cluster-shared

  Pass: 8   Fail: 0   Warn: 0
```

**Always run verify.sh after every reboot** — this is how you confirm the boot
chain is actually working, not just that services appear active.

---

## Known gotchas

These are real failure modes found in production. The install script works
around all of them, but knowing why helps if you ever debug a deviation.

### 1. The fstab mount fires before glusterd is ready

GlusterFS uses `_netdev` in fstab to mean "wait for network". But on a
self-referencing topology (node is both client and server), the client
needs the local glusterd to be running — not just the network. Without an
explicit `x-systemd.requires=glusterd.service` in the fstab options, the
mount fires, gets connection-refused, and silently fails. You get an empty
share with no errors in any log.

**The fix is one fstab option.** The install script adds it. If you write
your own fstab entry, always include:
```
x-systemd.requires=glusterd.service,x-systemd.after=glusterd.service
```

### 2. `_netdev` in a manual `.mount` unit injects a hidden condition (systemd 255+)

If you write a systemd `.mount` unit by hand (instead of using fstab), do
not put `_netdev` in the `Options=` field. In systemd 255 (Ubuntu 24.04),
this injects an implicit `ConditionNetworkConnectivity` check that is not
visible in the unit file, is not documented, and fails transiently at boot.

The symptom: the unit is silently skipped. The watchdog remounts it. Then
systemd unmounts it again because the unit is in a skipped state. Repeating
every minute indefinitely. The watchdog log says "mount OK" every minute,
but the share is always empty.

**Use explicit `After=` and `Requires=` in `[Unit]` instead.** See
`configs/systemd/cluster-shared.mount` for the correct template.

### 3. Use `systemctl start` in recovery scripts — not raw `mount`

When a watchdog or recovery script calls `mount /srv/share` directly,
it creates a FUSE mount outside systemd's unit lifecycle. Systemd detects
the mount, tries to activate the unit, and — if the unit has any conditions
or is in a failed state — immediately unmounts the filesystem. Your recovery
script logs success but the mount is gone 5 seconds later.

Always use `systemctl start 'cluster\x2dshared.mount'` in recovery scripts.
This routes the request through systemd's dependency resolver and keeps the
unit in the correct `active (mounted)` state.

### 4. Samba will silently serve an empty directory

If `/srv/share` is empty (because the GlusterFS mount failed), smbd starts
anyway and serves the empty directory. Windows clients get a connected drive
with no files, no error, no log entry. This is extremely hard to diagnose
because everything looks healthy.

The fix is a systemd drop-in that makes `smbd.service` require the mount
unit. If the mount fails, smbd refuses to start — you see an SMB connection
error immediately and know exactly what to fix.

### 5. Always reboot-test after any infrastructure change

Both issues above only appear on a cold reboot. They are invisible during
normal operation. Make it a habit: apply any configuration change, then
`sudo systemctl reboot`, then run `./verify.sh`. If all checks pass, the
change is done. If not, you have a clean signal before it causes an outage.

---

## Troubleshooting

| Symptom | First check |
|---|---|
| Windows share empty or partial | `mountpoint /srv/share` on the node serving the VIP |
| Share not accessible at all | `ctdb status` — is the VIP assigned to any node? |
| VIP not assigning | `systemctl status ctdb` — check `/etc/ctdb/nodes` IPs |
| Mount missing after reboot | `journalctl -b 0 \| grep -i 'cluster.*skip'` |
| Watchdog loop (mounts but disappears) | Check for `_netdev` in the `.mount` unit Options |
| GlusterFS split-brain | `gluster volume heal <vol> info split-brain` |
| Brick offline | `gluster volume status` — identify which peer is down |

Full diagnostics:
```bash
gluster peer status
gluster volume status
journalctl -b 0 | grep -E '(glusterd|ctdb|smbd|cluster-shared)' | tail -40
systemctl status smbd ctdb glusterd
tail -20 /var/log/cluster-shared-watchdog.log
```

---

## Extending the stack

**Overlay filesystem (write isolation):** node A can serve an overlay
(`overlayfs`) on top of the GlusterFS lower layer. This lets one node
buffer writes locally without immediately committing them to the
replicated volume — useful for LLM-generated output, build artifacts, or
anything that should be reviewed before it enters the shared store.
See `README.md` Phase 10 for the setup.

**LLM compute node:** node B can host an Ollama instance (CPU or GPU)
or a lightweight Gemini API shim, with a LiteLLM router on node A
providing a unified OpenAI-compatible endpoint to all clients. The shared
filesystem makes model files and outputs immediately available to both nodes.

---

## Repository layout

```
gluster-shadow-ha/
├── README.md              full phase-by-phase installation guide
├── install.sh             automated installer (all phases, idempotent)
├── verify.sh              post-install and post-reboot checks
├── index.html             GitHub Pages landing page
├── docs/
│   ├── architecture.md    component deep-dive and partition behaviour
│   └── gotchas.md         all 8 known failure modes with root causes and fixes
├── configs/
│   ├── smb.conf           Samba config template
│   ├── ctdb.conf          CTDB config template
│   ├── public_addresses   CTDB VIP file template
│   ├── systemd/
│   │   ├── cluster-shared.mount         manual mount unit (no _netdev — see gotchas #2)
│   │   └── smbd.service.d/
│   │       ├── override-node0.conf      smbd drop-in for node0 (shadow overlay)
│   │       └── override-node1.conf      smbd drop-in for node1 (direct mount)
│   └── watchdog/
│       ├── cluster-shared-watchdog.sh   recovery script
│       └── cluster-shared-watchdog.timer  timer unit
└── .github/
    └── workflows/shellcheck.yml         CI: lint shell scripts on push
```

---

## License

MIT — use it, adapt it, contribute back.

## Contributing

Issues and PRs welcome.  
When reporting a problem, please include the output of `./verify.sh` and:
```bash
journalctl -b 0 | grep -E '(glusterd|ctdb|smbd|cluster)' | tail -40
gluster peer status
gluster volume status
```
