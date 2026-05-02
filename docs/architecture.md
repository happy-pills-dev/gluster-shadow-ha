# Architecture

## Overview

```
Windows / Linux / macOS client
           │
           │  SMB  \\<VIP>\myshare
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

---

## Component roles

### GlusterFS

A distributed filesystem that replicates data across both nodes in real time.
When both nodes are connected, every write on one is immediately mirrored to
the other. When they're not, each node writes independently to its own brick
and the self-heal daemon reconciles them on reconnect.

Key settings:
- `cluster.self-heal-daemon: on` — background reconciliation after partition
- `cluster.favorite-child-policy: ctime` — last-write-wins on conflict
- Replica-3 arbiter mode (optional): lightweight third node for split-brain
  prevention without a full third replica

### Shadow layer (overlayfs — node A only)

Node A's Samba serves through a local `overlayfs` mount rather than directly
from the GlusterFS FUSE mount. This overlay sits between the share and the
replicated volume:

```
  clients → Samba → /srv/shadow (overlayfs)
                         │
               ┌─────────┴──────────┐
               │ upper (local disk) │ ← writes land here during partition
               │ lower (GlusterFS)  │ ← reads fall through to here
               └────────────────────┘
```

When the replication link is healthy, the overlay is transparent — writes pass
through to GlusterFS immediately. When the link breaks, writes land in the
overlay's upper directory on Node A's local disk. On reconnect, GlusterFS
self-heal picks up the divergence and reconciles.

This is the core differentiating feature: Node A never needs to freeze, go
read-only, or lose writes during a partition. The only limit is local disk
capacity (the upper directory).

### CTDB

Cluster Trivial Database — manages the floating VIP and coordinates Samba's
cluster state (open files, locks) across both nodes. When a node becomes
unhealthy, CTDB moves the VIP to the surviving node automatically. Clients
reconnect to the same IP; from their perspective, the outage is brief.

### Systemd

The boot chain is wired with explicit `Requires=` and `After=` dependencies
so the stack always comes up in the right order:

```
network-online.target
       │
   glusterd.service
       │
cluster-shared.mount  (fstab or manual unit)
       │
shadow-layer-setup.service  (node A only)
       │
smbd.service  nmbd.service  ctdb.service
```

A watchdog timer (`cluster-shared-watchdog.timer`) fires every minute starting
two minutes after boot. If `/cluster-shared` is not mounted, it recovers via
`systemctl start 'cluster\x2dshared.mount'` — never raw `mount`, which would
bypass the unit lifecycle and trigger an unmount loop.

---

## Partition behaviour

### Normal operation

Both nodes are connected. Writes on either node replicate to the other within
milliseconds. The VIP floats to the node CTDB considers healthiest (usually
Node A). Both nodes serve from the same consistent dataset.

### During partition (link down)

- **Node A**: continues serving through the shadow layer. New writes land in
  `/opt/shadow-layer/data` (the overlay upper directory). Reads fall through to
  the GlusterFS lower layer, which still has the pre-partition state. Clients
  see no interruption.

- **Node B**: continues serving directly from its GlusterFS brick. New writes
  go to the brick as normal.

Neither node freezes. Neither goes read-only. Both keep serving independently
until the link is restored.

### On reconnect

GlusterFS's self-heal daemon detects the divergence between the two bricks and
begins reconciling. Files modified on Node A drain from the shadow overlay back
into the GlusterFS volume. Files modified on Node B are visible on Node A.
Conflicts are resolved by `ctime` (last-write-wins).

The entire process is unattended. No operator steps required.

---

## Boot sequence timing

On a heavily loaded node (many LVM volumes, Docker stack), `glusterd` can take
1.5–2 minutes to reach `active`. The watchdog fires at `OnBootSec=2min`, which
naturally coincides with glusterd becoming ready on most hardware.

The `ExecStartPost` in the glusterd hardening drop-in adds a `sleep 5` + force
volume-start to ensure the volume is started before the mount unit fires.

Reboot-test procedure: after any configuration change, reboot the node and run
`./verify.sh`. A pass on all checks confirms the boot chain is working.
