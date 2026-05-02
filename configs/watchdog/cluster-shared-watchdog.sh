#!/usr/bin/env bash
# cluster-shared-watchdog.sh
# Re-mounts /cluster-shared if it has dropped, then restarts Samba.
# Runs every minute via cluster-shared-watchdog.timer.
#
# IMPORTANT: uses `systemctl start` — NOT raw `mount`.
# Raw `mount` bypasses the systemd unit lifecycle. If the unit has any
# conditions or dependencies, systemd will immediately unmount the FUSE
# filesystem, creating an infinite remount/unmount loop.
# Routing through `systemctl start` keeps the unit in the correct
# `active (mounted)` state and respects dependency ordering.
#
# Install: cp to /usr/local/sbin/cluster-shared-watchdog.sh
#          chmod +x /usr/local/sbin/cluster-shared-watchdog.sh

set -euo pipefail

CLUSTER_MOUNT="/cluster-shared"     # adjust if you changed CLUSTER_MOUNT in install.sh
LOG="/var/log/cluster-shared-watchdog.log"
WATCHDOG_MAIL="root"                 # set to your email for failure alerts

log() { echo "[$(date -Iseconds)] $*" >> "$LOG"; }

if ! mountpoint -q "$CLUSTER_MOUNT"; then
    log "$CLUSTER_MOUNT NOT mounted — attempting recovery"

    # Route through systemctl to respect the unit's dependency chain
    if systemctl start 'cluster\x2dshared.mount' >> "$LOG" 2>&1; then
        log "mount OK — restarting samba"

        # node0: also restart shadow-layer-setup before smbd
        # Uncomment on node0:
        # systemctl restart shadow-layer-setup smbd nmbd >> "$LOG" 2>&1

        # node1 (and default):
        systemctl restart smbd nmbd >> "$LOG" 2>&1

        log "recovery complete"
    else
        log "mount FAILED — manual intervention required"
        echo "CLUSTER: $CLUSTER_MOUNT mount failed on $(hostname) at $(date)" | \
            mail -s "[CLUSTER] mount failure on $(hostname)" "$WATCHDOG_MAIL" 2>/dev/null || true
    fi
else
    log "OK — $CLUSTER_MOUNT is mounted"
fi
