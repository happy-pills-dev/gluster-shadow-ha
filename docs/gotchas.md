# Known Gotchas

These are real failure modes found in a production deployment of this stack.
Every item in this list caused a silent or confusing outage, was root-caused,
and is now prevented by the installer. Reading this before you build will
save you hours.

---

## 1. The boot-order race (mount fires before glusterd is ready)

**Symptom:** After a reboot, `/cluster-shared` is not mounted. Samba starts,
serves an empty directory. No errors anywhere. Everything looks healthy but
clients see no files.

**Root cause:** The fstab `_netdev` flag tells systemd to wait for
`network-online.target` before mounting. But on a self-referencing topology
(client and server on the same machine), the GlusterFS client needs the local
`glusterd` to be running — not just the network. `glusterd` starts after the
network but the exact timing is not deterministic.

**Fix:** Always include `x-systemd.requires=glusterd.service` in the fstab
Options field, and correspondingly `x-systemd.after=glusterd.service`:

```
<NODE_IP>:/<VOLUME>  /cluster-shared  glusterfs  \
defaults,_netdev,x-systemd.requires=glusterd.service,x-systemd.after=glusterd.service,\
x-systemd.mount-timeout=30s,backup-volfile-servers=<OTHER_NODE_IP>  0 0
```

The `install.sh` adds this automatically. If you write the fstab entry by hand,
don't forget it.

---

## 2. `_netdev` in a manual `.mount` unit injects a hidden condition (systemd 255+)

**Symptom:** Mount works manually but not at boot. Journal shows:
```
Condition check resulted in cluster\x2dshared.mount being skipped.
```
Watchdog remounts it, but it disappears again 60 seconds later. The watchdog
log says "mount OK" every minute but the share is always empty. Infinite loop.

**Root cause:** In systemd 255 (Ubuntu 24.04), the `_netdev` flag in a
manually-written `.mount` unit's `[Mount] Options` field causes systemd to
inject an implicit `ConditionNetworkConnectivity` check. This condition:
- Is not visible in the unit file (no `Condition*=` line is written)
- Is not documented in standard systemd man pages
- Fails transiently at boot, causing the unit to be silently skipped
- Is re-evaluated when the watchdog calls raw `mount`, causing systemd to
  detect an "untracked" FUSE mount and unmount it immediately

**Fix:** Never include `_netdev` in `[Mount] Options` of a manually-written
`.mount` unit. Use explicit `After=` and `Requires=` in `[Unit]` instead:

```ini
[Unit]
After=network-online.target glusterd.service
Wants=network-online.target
Requires=glusterd.service

[Mount]
# NO _netdev in Options — see docs/gotchas.md #2
Options=defaults,backup-volfile-servers=<OTHER_NODE_IP>,...
```

See `configs/systemd/cluster-shared.mount` for the correct template.

---

## 3. Watchdog must use `systemctl start`, not raw `mount`

**Symptom:** Watchdog script calls `mount /cluster-shared`. Journal shows the
mount succeeding. 5–10 seconds later it's gone. Loop repeats every minute.

**Root cause:** Calling `mount` directly creates a FUSE mount outside the
systemd unit lifecycle. Systemd detects the "untracked" mount, tries to
activate the corresponding `.mount` unit, and — if that unit has any Condition
failures or dependency issues — immediately unmounts the filesystem.

**Fix:** Use `systemctl start 'cluster\x2dshared.mount'` in the watchdog:

```bash
# WRONG — bypasses systemd unit lifecycle:
mount /cluster-shared

# CORRECT — routes through dependency resolver:
systemctl start 'cluster\x2dshared.mount'
```

Note the escaped unit name: `/cluster-shared` encodes to `cluster\x2dshared`
in systemd's path escaping scheme (the hyphen becomes `\x2d`).

---

## 4. Samba will silently serve an empty directory

**Symptom:** Windows drive is connected. No files visible. No errors on the
server or client. `smbd` is running and `ctdb status` is OK.

**Root cause:** `smbd.service` has no dependency on the GlusterFS mount or the
overlay setup service. If `/cluster-shared` failed to mount at boot (for any
reason), smbd starts anyway and serves whatever is at the share path — which
may be an empty directory, a stub tree from a prior run, or a failed overlayfs
mount that appears empty.

**Fix:** Add the drop-in from `configs/systemd/smbd.service.d/`:

```bash
# node0 (overlay):
cp configs/systemd/smbd.service.d/override-node0.conf \
   /etc/systemd/system/smbd.service.d/cluster-shared-required.conf

# node1 (direct mount):
cp configs/systemd/smbd.service.d/override-node1.conf \
   /etc/systemd/system/smbd.service.d/cluster-shared-required.conf

systemctl daemon-reload
```

With this in place, smbd refuses to start if the storage layer isn't ready.
The error is visible immediately on the client and in `systemctl status smbd`.

---

## 5. `systemctl is-enabled 'cluster-shared.mount'` returns "not-found"

**Symptom:** Running `systemctl is-enabled 'cluster-shared.mount'` returns
`not-found` even though the unit exists and the mount works.

**Root cause:** Systemd path escaping: `/cluster-shared` contains a hyphen,
which systemd encodes as `\x2d` in unit names. The unit on disk is named
`cluster\x2dshared.mount`. When you pass `cluster-shared.mount` to systemctl,
it interprets the hyphen as a path separator and looks for `/cluster/shared` —
a completely different path.

**Fix:** Always use the escaped form:

```bash
systemctl status 'cluster\x2dshared.mount'
systemctl is-active 'cluster\x2dshared.mount'
systemctl is-enabled 'cluster\x2dshared.mount'
```

Or use `printf` to write the filename correctly:
```bash
printf 'cluster\x2dshared.mount'  # → cluster-shared.mount (the actual filename)
```

---

## 6. glusterd takes ~2 minutes to start on a loaded node

**Symptom:** After a reboot, the mount fails even with `x-systemd.requires`.
The watchdog catches it ~2 minutes later and recovers. Everything is fine at
steady state but the first 2 minutes after boot the share is missing.

**Root cause:** On a node with many LVM volumes, a running Docker stack, or
significant disk I/O at boot, `glusterd` can take 1.5–2 minutes to become
`active`. The mount unit correctly waits for it but the `ExecStartPost` that
force-starts the volume hasn't run yet when dependent units first try.

**Fix:** The glusterd hardening drop-in (`Phase 5` in the install guide) adds
an `ExecStartPost` that sleeps 5 seconds after glusterd starts and then
force-starts the volume. This provides a buffer. The watchdog's `OnBootSec=2min`
fires simultaneously and provides a second recovery path if the mount still
isn't ready.

---

## 7. BIOS "Restore on AC loss" causes `shutdown -h` to reboot

**Symptom:** `sudo shutdown -h now` appears to halt the machine, but it comes
back up 30 seconds later.

**Root cause:** Many ASUS/MSI/ASRock/Gigabyte boards have a BIOS setting
"Restore on AC loss: Power On". With this enabled, the machine powers on
automatically whenever power is applied. `shutdown -h` halts the OS cleanly,
the PSU briefly resets during the halt, and the BIOS immediately boots again.

**Impact for this stack:** A planned "halt and don't come back" of one node
can unexpectedly leave both nodes running. CTDB may split-brain briefly if
the node comes up mid-shutdown of the other.

**Fix:** If you need a true power-off: halt the node, wait for the shutdown
to complete, then physically hold the power button for 5 seconds (or pull the
PSU cable). Alternatively, disable "Restore on AC loss" in the BIOS.

---

## 8. Always reboot-test after any configuration change

**These failure modes are invisible during normal operation.** The _netdev
condition bug (#2), the watchdog mount loop (#3), and the boot-order race (#1)
only appear on a cold reboot. A system that looks healthy in steady state can
be silently broken for the next reboot.

**Mandatory procedure after any infrastructure change:**

1. Apply the change
2. `sudo systemctl reboot`
3. Reconnect (wait ~3 minutes)
4. `./verify.sh`

If all checks pass, the change is done. If any fail, you have a clean signal
before it causes an outage.
