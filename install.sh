#!/usr/bin/env bash
# =============================================================================
# gluster-shadow-ha install.sh
# HA GlusterFS + CTDB + Samba cluster on Ubuntu 24.04 / 22.04
#
# Usage:
#   sudo ./install.sh --role node0              # run all phases on node0
#   sudo ./install.sh --role node1              # run all phases on node1
#   sudo ./install.sh --role node0 --step gluster-volume-create  # one phase
#   ./install.sh --verify                       # run checks without root
#   sudo ./install.sh --role node0 --dry-run    # print actions, do nothing
#
# Edit the VARIABLES section below before running.
# =============================================================================
set -euo pipefail

# =============================================================================
# VARIABLES — edit these for your environment
# =============================================================================

NODE0_IP="192.168.1.10"        # primary IP of node0
NODE1_IP="192.168.1.11"        # primary IP of node1
ARBITER_IP=""                  # optional arbiter IP — leave empty to use replica-2 without arbiter
VIP="192.168.1.100"            # CTDB floating VIP — must be an unused address on the same subnet
VIP_NIC="eth0"                 # NIC that carries the VIP (must be the same name on both nodes)
VIP_PREFIX="24"                # CIDR prefix for VIP e.g. 24 → /24

GLUSTER_VOL="mydata"           # GlusterFS volume name
BRICK_DEV="/dev/sdb"           # dedicated brick disk (raw block device)
BRICK_VG="gluster-vg"          # LVM volume group name
BRICK_LV="brick0-lv"           # LVM logical volume name
BRICK_MOUNT="/data/gluster/brick0"  # where brick disk is mounted
CLUSTER_MOUNT="/cluster-shared"     # where the GlusterFS FUSE client mounts

SMB_SHARE="myshare"            # Samba share name  (maps to \\<VIP>\myshare)
SMB_WORKGROUP="WORKGROUP"      # SMB workgroup
SMB_PATH_NODE0="/srv/shadow/data"        # node0 Samba path (through shadow overlay)
SMB_PATH_NODE1="/cluster-shared/data"    # node1 Samba path (direct GlusterFS mount)
SMB_VALID_USERS="@sambausers"            # adjust to your user/group

SHADOW_UPPER="/opt/shadow-layer/data"   # overlay upper dir (node0 only)
SHADOW_WORK="/opt/shadow-layer/work"    # overlay work dir  (node0 only)
SHADOW_MOUNT="/srv/shadow"             # overlay mount point (node0 only)

WATCHDOG_MAIL="root"          # address for watchdog failure alerts

# =============================================================================
# INTERNALS — do not edit below unless you know what you're doing
# =============================================================================

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLU='\033[0;34m'
NC='\033[0m'

ROLE=""
STEP=""
DRY_RUN=false
VERIFY_ONLY=false

log()  { echo -e "${BLU}[INFO]${NC}  $*"; }
ok()   { echo -e "${GRN}[PASS]${NC}  $*"; }
warn() { echo -e "${YLW}[WARN]${NC}  $*"; }
err()  { echo -e "${RED}[FAIL]${NC}  $*"; }
die()  { err "$*"; exit 1; }

run() {
    if $DRY_RUN; then
        echo -e "${YLW}[DRY]${NC}   $*"
    else
        eval "$@"
    fi
}

section() { echo; echo -e "${BLU}━━━  $*  ━━━${NC}"; echo; }

check_root() {
    [[ $EUID -eq 0 ]] || die "This script must be run as root (sudo ./install.sh)"
}

detect_this_node_ip() {
    THIS_IP=$(hostname -I | awk '{print $1}')
    if [[ "$THIS_IP" == "$NODE0_IP" ]]; then
        DETECTED_ROLE="node0"
    elif [[ "$THIS_IP" == "$NODE1_IP" ]]; then
        DETECTED_ROLE="node1"
    else
        DETECTED_ROLE="unknown"
    fi
}

# =============================================================================
# ARGUMENT PARSING
# =============================================================================

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --role)   ROLE="$2"; shift 2 ;;
            --step)   STEP="$2"; shift 2 ;;
            --dry-run) DRY_RUN=true;  shift ;;
            --verify)  VERIFY_ONLY=true; shift ;;
            --help|-h)
                grep '^# ' "$0" | head -10 | sed 's/^# //'
                exit 0
                ;;
            *) die "Unknown argument: $1" ;;
        esac
    done
}

# =============================================================================
# PHASE 0 — packages
# =============================================================================

step_packages() {
    section "Phase 0: Install packages"
    run apt-get update -qq
    run apt-get install -y \
        glusterfs-server \
        glusterfs-client \
        ctdb \
        samba \
        samba-common-bin \
        lvm2 \
        xfsprogs \
        net-tools \
        iproute2 \
        curl \
        wget \
        attr \
        acl
    # mailutils for watchdog alerts (optional — don't fail if unavailable)
    apt-get install -y mailutils 2>/dev/null || warn "mailutils not available — watchdog email alerts disabled"
    ok "Packages installed"
}

# =============================================================================
# PHASE 1 — LVM + brick filesystem
# =============================================================================

step_brick_disk() {
    section "Phase 1: Prepare GlusterFS brick disk"

    if mountpoint -q "$BRICK_MOUNT" 2>/dev/null; then
        ok "Brick disk already mounted at $BRICK_MOUNT — skipping"
        return
    fi

    log "Checking brick device $BRICK_DEV"
    [[ -b "$BRICK_DEV" ]] || die "$BRICK_DEV is not a block device. Set BRICK_DEV in the VARIABLES section."

    log "Creating LVM: VG=$BRICK_VG, LV=$BRICK_LV"
    run pvcreate -y "$BRICK_DEV"
    run vgcreate -y "$BRICK_VG" "$BRICK_DEV"
    run lvcreate -l 100%FREE -n "$BRICK_LV" "$BRICK_VG"

    log "Formatting as XFS"
    run mkfs.xfs "/dev/$BRICK_VG/$BRICK_LV"

    log "Mounting at $BRICK_MOUNT"
    run mkdir -p "$BRICK_MOUNT"

    FSTAB_LINE="/dev/$BRICK_VG/$BRICK_LV  $BRICK_MOUNT  xfs  defaults  0 2"
    if ! grep -qF "$BRICK_MOUNT" /etc/fstab; then
        run echo "$FSTAB_LINE" \>> /etc/fstab
    fi
    run mount -a
    run mkdir -p "$BRICK_MOUNT/vol"

    ok "Brick disk ready at $BRICK_MOUNT/vol"
}

# =============================================================================
# PHASE 2 — GlusterFS server + peer
# =============================================================================

step_gluster_server() {
    section "Phase 2: Start GlusterFS and probe peers"

    run systemctl enable glusterd
    run systemctl start glusterd

    # Wait for glusterd to be ready
    for i in $(seq 1 10); do
        if gluster peer status &>/dev/null; then break; fi
        log "Waiting for glusterd... ($i/10)"
        sleep 3
    done

    if [[ "$ROLE" == "node0" ]]; then
        log "Probing node1 ($NODE1_IP)"
        run gluster peer probe "$NODE1_IP" || warn "Peer probe failed — run manually after node1 is up"
        if [[ -n "${ARBITER_IP:-}" ]]; then
            log "Probing arbiter ($ARBITER_IP)"
            run gluster peer probe "$ARBITER_IP" || warn "Arbiter probe failed — run manually after arbiter is available"
        fi
    fi

    ok "GlusterFS server started"
}

# =============================================================================
# PHASE 3 — create GlusterFS volume (run ONCE from node0)
# =============================================================================

step_gluster_volume_create() {
    section "Phase 3: Create GlusterFS volume"

    [[ "$ROLE" == "node0" ]] || die "Volume creation must be run on node0"

    if gluster volume info "$GLUSTER_VOL" &>/dev/null; then
        ok "Volume $GLUSTER_VOL already exists — skipping"
        return
    fi

    # Ensure arbiter dir exists on arbiter host (if reachable via ssh)
    if [[ -n "${ARBITER_IP:-}" ]]; then
        log "Creating arbiter brick directory on $ARBITER_IP"
        ssh -o StrictHostKeyChecking=no root@"$ARBITER_IP" \
            "mkdir -p /data/gluster/arbiter" 2>/dev/null || \
            warn "Could not create arbiter dir via SSH — create /data/gluster/arbiter on $ARBITER_IP manually"

        log "Creating replica-3 arbiter volume"
        run gluster volume create "$GLUSTER_VOL" \
            replica 3 arbiter 1 \
            "${NODE0_IP}:${BRICK_MOUNT}/vol" \
            "${NODE1_IP}:${BRICK_MOUNT}/vol" \
            "${ARBITER_IP}:/data/gluster/arbiter" \
            force
    else
        log "Creating replica-2 volume (no arbiter)"
        run gluster volume create "$GLUSTER_VOL" \
            replica 2 \
            "${NODE0_IP}:${BRICK_MOUNT}/vol" \
            "${NODE1_IP}:${BRICK_MOUNT}/vol" \
            force
    fi

    run gluster volume start "$GLUSTER_VOL"
    run gluster volume status "$GLUSTER_VOL"
    ok "Volume $GLUSTER_VOL created and started"
}

# =============================================================================
# PHASE 4 — fstab mount with glusterd dependency
# =============================================================================

step_fstab_mount() {
    section "Phase 4: Configure /etc/fstab cluster-shared mount"

    if [[ "$ROLE" == "node0" ]]; then
        VOLFILE_SERVER="$NODE0_IP"
        BACKUP_SERVER="$NODE1_IP"
    else
        VOLFILE_SERVER="$NODE1_IP"
        BACKUP_SERVER="$NODE0_IP"
    fi

    FSTAB_LINE="${VOLFILE_SERVER}:/${GLUSTER_VOL}  ${CLUSTER_MOUNT}  glusterfs  defaults,_netdev,x-systemd.requires=glusterd.service,x-systemd.after=glusterd.service,x-systemd.mount-timeout=30s,backup-volfile-servers=${BACKUP_SERVER}  0 0"

    run mkdir -p "$CLUSTER_MOUNT"

    if grep -qF "$CLUSTER_MOUNT" /etc/fstab; then
        ok "fstab entry for $CLUSTER_MOUNT already exists — skipping"
    else
        run echo "$FSTAB_LINE" \>> /etc/fstab
    fi

    run systemctl daemon-reload
    run mount "$CLUSTER_MOUNT" 2>/dev/null || log "Mount may already be mounted or volume not ready yet"

    if mountpoint -q "$CLUSTER_MOUNT"; then
        ok "$CLUSTER_MOUNT is mounted"
    else
        warn "$CLUSTER_MOUNT is NOT mounted — volume may not be started yet. Run 'mount $CLUSTER_MOUNT' after volume is ready."
    fi
}

# =============================================================================
# PHASE 4b — node1 manual .mount unit (critical: no _netdev in Options)
# =============================================================================

step_node1_mount_unit() {
    section "Phase 4b: Install manual cluster-shared.mount unit (node1 only)"

    [[ "$ROLE" == "node1" ]] || { log "Skipping node1 .mount unit on $ROLE"; return; }

    UNIT_FILE="/etc/systemd/system/cluster\x2dshared.mount"
    UNIT_DIR="/etc/systemd/system"

    # Use printf to write the literal filename with \x2d
    UNIT_PATH="${UNIT_DIR}/$(printf 'cluster\x2dshared.mount')"

    if [[ -f "$UNIT_PATH" ]] || [[ -L "$UNIT_PATH" ]]; then
        warn "Unit file $UNIT_PATH already exists — checking for _netdev"
        if grep -q '_netdev' "$UNIT_PATH" 2>/dev/null; then
            warn "Unit contains _netdev in Options — REMOVING (causes implicit Condition failure on systemd 255)"
            run sed -i 's/_netdev,//g; s/,_netdev//g; s/_netdev//g' "$UNIT_PATH"
            ok "_netdev removed from unit Options"
        else
            ok "Unit file is clean (no _netdev)"
        fi
        run systemctl daemon-reload
        return
    fi

    log "Writing $UNIT_PATH"
    $DRY_RUN || cat > "$UNIT_PATH" << EOF
[Unit]
Description=Cluster Shared GlusterFS Volume
Documentation=man:glusterfs(8)
After=network-online.target glusterd.service
Wants=network-online.target
Requires=glusterd.service
Before=ctdb.service smbd.service nmbd.service

[Mount]
What=${NODE1_IP}:/${GLUSTER_VOL}
Where=${CLUSTER_MOUNT}
Type=glusterfs
# DO NOT add _netdev here — it injects an implicit ConditionNetworkConnectivity
# in systemd 255 that races at boot and causes the unit to be skipped silently.
# Use explicit After=/Requires= above instead.
Options=defaults,backup-volfile-servers=${NODE0_IP},log-level=WARNING,log-file=/var/log/glusterfs/cluster-shared-mount.log
TimeoutSec=30

[Install]
WantedBy=multi-user.target
EOF

    run systemctl daemon-reload
    run systemctl enable "$(printf 'cluster\x2dshared.mount')"
    ok "Manual .mount unit installed on node1"
}

# =============================================================================
# PHASE 5 — glusterd boot hardening (node0)
# =============================================================================

step_glusterd_hardening() {
    section "Phase 5: Harden glusterd boot ordering"

    DROPIN_DIR="/etc/systemd/system/glusterd.service.d"
    DROPIN="$DROPIN_DIR/wait-for-internal-net.conf"

    if [[ -f "$DROPIN" ]]; then
        ok "glusterd hardening drop-in already exists — skipping"
        return
    fi

    # Detect the primary NIC (the one with the node's IP)
    NIC=$(ip route get "$NODE0_IP" 2>/dev/null | grep -oP 'dev \K\S+' | head -1 || echo "$VIP_NIC")
    log "Using NIC: $NIC for glusterd after= dependency"

    run mkdir -p "$DROPIN_DIR"
    $DRY_RUN || cat > "$DROPIN" << EOF
[Unit]
After=sys-subsystem-net-devices-${NIC}.device network-online.target
Wants=sys-subsystem-net-devices-${NIC}.device

[Service]
ExecStartPost=/bin/bash -c 'sleep 5 && /usr/sbin/gluster volume start ${GLUSTER_VOL} force 2>&1 | logger -t glusterd-poststart || true'
EOF

    run systemctl daemon-reload
    ok "glusterd boot hardening installed"
}

# =============================================================================
# PHASE 6 — CTDB
# =============================================================================

step_ctdb() {
    section "Phase 6: Configure CTDB"

    if [[ "$ROLE" == "node0" ]]; then
        THIS_IP="$NODE0_IP"
    else
        THIS_IP="$NODE1_IP"
    fi

    # Nodes file
    $DRY_RUN || cat > /etc/ctdb/nodes << EOF
${NODE0_IP}
${NODE1_IP}
EOF

    # Public addresses (floating VIP)
    $DRY_RUN || cat > /etc/ctdb/public_addresses << EOF
${VIP}/${VIP_PREFIX} ${VIP_NIC}
EOF

    # Minimal ctdb.conf
    $DRY_RUN || cat > /etc/ctdb/ctdb.conf << EOF
[logging]
  location = file:/var/log/ctdb/ctdb.log
  log level = NOTICE

[cluster]
  transport = tcp
  node address = ${THIS_IP}

[legacy]
  lmaster role = yes
EOF

    run mkdir -p /var/lib/ctdb/persistent /var/lib/ctdb/volatile /var/run/ctdb

    run systemctl enable ctdb
    run systemctl restart ctdb 2>/dev/null || run systemctl start ctdb

    log "Waiting for CTDB to establish cluster..."
    for i in $(seq 1 12); do
        if ctdb status 2>/dev/null | grep -q "OK"; then break; fi
        sleep 5
    done

    if ctdb status 2>/dev/null | grep -q "OK"; then
        ok "CTDB cluster is OK"
    else
        warn "CTDB status not fully OK yet — this is normal on first boot. Re-check after both nodes are running: ctdb status"
    fi
}

# =============================================================================
# PHASE 7 — Samba
# =============================================================================

step_samba() {
    section "Phase 7: Configure Samba"

    if [[ "$ROLE" == "node0" ]]; then
        SHARE_PATH="$SMB_PATH_NODE0"
    else
        SHARE_PATH="$SMB_PATH_NODE1"
    fi

    # Backup original if it exists
    [[ -f /etc/samba/smb.conf ]] && cp /etc/samba/smb.conf /etc/samba/smb.conf.bak

    $DRY_RUN || cat > /etc/samba/smb.conf << EOF
[global]
    workgroup = ${SMB_WORKGROUP}
    server string = Cluster Node %h
    clustering = yes
    idmap config * : backend = tdb
    ctdbd socket = /var/run/ctdb/ctdbd.socket
    private dir = /var/lib/samba/private

    # Log settings
    log file = /var/log/samba/log.%m
    max log size = 50
    log level = 1

    # Security
    security = user
    passdb backend = tdbsam

[${SMB_SHARE}]
    comment = Cluster Shared Storage
    path = ${SHARE_PATH}
    browseable = yes
    writable = yes
    create mask = 0664
    directory mask = 0775
    valid users = ${SMB_VALID_USERS}
EOF

    run mkdir -p "$SHARE_PATH"
    run testparm -s /etc/samba/smb.conf 2>/dev/null && ok "smb.conf syntax OK" || warn "smb.conf test failed — check /etc/samba/smb.conf"

    run systemctl enable smbd nmbd
    run systemctl restart smbd nmbd
    ok "Samba configured"
    log "Add users with: smbpasswd -a <username>"
}

# =============================================================================
# PHASE 8 — smbd dependency drop-in (fail loud, not silent)
# =============================================================================

step_smbd_dropin() {
    section "Phase 8: smbd must not serve stale content"

    DROPIN_DIR="/etc/systemd/system/smbd.service.d"
    DROPIN="$DROPIN_DIR/cluster-shared-required.conf"

    run mkdir -p "$DROPIN_DIR"

    if [[ "$ROLE" == "node0" ]]; then
        $DRY_RUN || cat > "$DROPIN" << 'EOF'
[Unit]
Requires=shadow-layer-setup.service
After=shadow-layer-setup.service
EOF
    else
        $DRY_RUN || cat > "$DROPIN" << 'EOF'
[Unit]
After=cluster-shared.mount
Wants=cluster-shared.mount
EOF
    fi

    run systemctl daemon-reload
    ok "smbd dependency drop-in installed"
}

# =============================================================================
# PHASE 9 — watchdog timer
# =============================================================================

step_watchdog() {
    section "Phase 9: Install cluster-shared watchdog timer"

    # Watchdog script
    $DRY_RUN || cat > /usr/local/sbin/cluster-shared-watchdog.sh << SCRIPT
#!/usr/bin/env bash
set -euo pipefail
LOG=/var/log/cluster-shared-watchdog.log
log() { echo "[\$(date -Iseconds)] \$*" >> "\$LOG"; }

if ! mountpoint -q ${CLUSTER_MOUNT}; then
    log "${CLUSTER_MOUNT} NOT mounted — attempting recovery"
    # IMPORTANT: use systemctl start, NOT raw 'mount'
    # Raw mount bypasses the systemd unit lifecycle and causes a remount cycle
    # if the unit has any conditions or dependency ordering requirements.
    if systemctl start 'cluster\x2dshared.mount' >> "\$LOG" 2>&1; then
        log "mount OK — restarting samba"
$(if [[ "${ROLE:-}" == "node0" ]]; then
    echo "        systemctl restart shadow-layer-setup smbd nmbd >> \"\$LOG\" 2>&1"
else
    echo "        systemctl restart smbd nmbd >> \"\$LOG\" 2>&1"
fi)
        log "recovery complete"
    else
        log "mount FAILED — manual intervention required"
        echo "CLUSTER: ${CLUSTER_MOUNT} mount failed on \$(hostname) at \$(date)" | \\
            mail -s "[CLUSTER] mount failure on \$(hostname)" ${WATCHDOG_MAIL} 2>/dev/null || true
    fi
else
    log "OK — ${CLUSTER_MOUNT} is mounted"
fi
SCRIPT
    run chmod +x /usr/local/sbin/cluster-shared-watchdog.sh

    # Service unit
    $DRY_RUN || cat > /etc/systemd/system/cluster-shared-watchdog.service << 'EOF'
[Unit]
Description=Re-mount /cluster-shared if it dropped
After=glusterd.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/cluster-shared-watchdog.sh
EOF

    # Timer unit
    $DRY_RUN || cat > /etc/systemd/system/cluster-shared-watchdog.timer << 'EOF'
[Unit]
Description=Check /cluster-shared mount health every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
EOF

    run systemctl daemon-reload
    run systemctl enable --now cluster-shared-watchdog.timer
    ok "Watchdog timer installed and enabled"
}

# =============================================================================
# PHASE 10 — overlay filesystem (node0 only)
# =============================================================================

step_overlay() {
    section "Phase 10: Install shadow-mode overlay (node0 only)"

    [[ "$ROLE" == "node0" ]] || { log "Skipping overlay setup on $ROLE"; return; }

    run mkdir -p "$SHADOW_UPPER" "$SHADOW_WORK" "$SHADOW_MOUNT/scripts"

    $DRY_RUN || cat > /etc/systemd/system/shadow-layer-setup.service << EOF
[Unit]
Description=Mount shadow overlay filesystem
Documentation=https://github.com/happy-pills-dev/gluster-shadow-ha
After=cluster-shared.mount
Requires=cluster-shared.mount
ConditionPathIsMountPoint=${CLUSTER_MOUNT}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/mount -t overlay overlay \\
    -o lowerdir=${CLUSTER_MOUNT},upperdir=${SHADOW_UPPER},workdir=${SHADOW_WORK} \\
    ${SHADOW_MOUNT}
ExecStop=/bin/umount ${SHADOW_MOUNT}

[Install]
WantedBy=multi-user.target
EOF

    run systemctl daemon-reload
    run systemctl enable shadow-layer-setup

    if mountpoint -q "$CLUSTER_MOUNT"; then
        run systemctl start shadow-layer-setup
        ok "Shadow overlay mounted at $SHADOW_MOUNT"
    else
        warn "Cluster mount not ready — shadow-layer-setup will start automatically after next boot / mount"
    fi
}

# =============================================================================
# VERIFY — post-install / post-reboot checks
# =============================================================================

run_verify() {
    section "Verification"
    FAILS=0

    check() {
        local label="$1"; shift
        if eval "$@" &>/dev/null; then
            ok "$label"
        else
            err "$label"
            (( FAILS++ )) || true
        fi
    }

    echo "  Node: $(hostname) — $(hostname -I | awk '{print $1}')"
    echo "  Date: $(date)"
    echo

    check "glusterd is active"         "systemctl is-active glusterd"
    check "gluster peer status OK"     "gluster peer status | grep -q 'Connected'"
    check "$CLUSTER_MOUNT is mounted"  "mountpoint -q $CLUSTER_MOUNT"
    check "cluster-shared has content" "[ \$(ls $CLUSTER_MOUNT 2>/dev/null | wc -l) -gt 0 ]"
    check "smbd is active"             "systemctl is-active smbd"
    check "nmbd is active"             "systemctl is-active nmbd"
    check "ctdb is active"             "systemctl is-active ctdb"
    check "watchdog timer active"      "systemctl is-active cluster-shared-watchdog.timer"
    check "no Condition skip in boot"  "! journalctl -b 0 2>/dev/null | grep -qi 'condition.*cluster.*skip\|cluster.*being skipped'"

    # Detect this node's role for role-specific checks
    detect_this_node_ip
    if [[ "$DETECTED_ROLE" == "node0" ]]; then
        check "shadow-layer-setup active"  "systemctl is-active shadow-layer-setup"
        check "shadow overlay mounted"     "mountpoint -q $SHADOW_MOUNT"
        check "shadow data accessible"     "mountpoint -q $SHADOW_MOUNT && ls $SHADOW_MOUNT &>/dev/null"
    fi

    echo
    log "Watchdog log (last 5 entries):"
    tail -5 /var/log/cluster-shared-watchdog.log 2>/dev/null || echo "  (no log yet)"

    echo
    if [[ $FAILS -eq 0 ]]; then
        ok "All checks passed"
    else
        err "$FAILS check(s) FAILED"
        echo
        echo "  Diagnostics:"
        echo "    gluster peer status"
        gluster peer status 2>/dev/null | head -10 || true
        echo
        echo "    journalctl -b 0 | grep -E '(cluster.shared|glusterd|ctdb)' | tail -20"
        journalctl -b 0 2>/dev/null | grep -E '(cluster.shared|glusterd|ctdb)' | tail -20 || true
        return 1
    fi
}

# =============================================================================
# FULL INSTALL — run all phases in order
# =============================================================================

run_all_phases() {
    check_root

    section "gluster-shadow-ha install — role: $ROLE"
    log "NODE0_IP=$NODE0_IP  NODE1_IP=$NODE1_IP  VIP=$VIP"
    log "GLUSTER_VOL=$GLUSTER_VOL  CLUSTER_MOUNT=$CLUSTER_MOUNT"
    $DRY_RUN && warn "DRY RUN mode — no changes will be made"
    echo

    step_packages
    step_brick_disk
    step_gluster_server

    if [[ "$ROLE" == "node0" ]]; then
        warn "Phase 3 (volume create) is skipped in full run."
        warn "After BOTH nodes complete this script, run:"
        warn "  sudo ./install.sh --role node0 --step gluster-volume-create"
    fi

    step_fstab_mount
    step_node1_mount_unit

    [[ "$ROLE" == "node0" ]] && step_glusterd_hardening

    step_ctdb
    step_samba
    step_smbd_dropin
    step_watchdog

    [[ "$ROLE" == "node0" ]] && step_overlay

    section "Installation complete"
    ok "All phases done for $ROLE"
    echo
    log "Next steps:"
    if [[ "$ROLE" == "node0" ]]; then
        echo "  1. Run this script on node1: sudo ./install.sh --role node1"
        echo "  2. Create the GlusterFS volume:"
        echo "       sudo ./install.sh --role node0 --step gluster-volume-create"
        echo "  3. Mount on both nodes:"
        echo "       sudo mount $CLUSTER_MOUNT"
        echo "  4. Add Samba user:"
        echo "       sudo smbpasswd -a <username>"
        echo "  5. Reboot-test both nodes:"
        echo "       sudo systemctl reboot"
        echo "  6. Verify after reboot:"
        echo "       ./install.sh --verify"
    else
        echo "  1. Go to node0 and run step gluster-volume-create"
        echo "  2. Mount: sudo mount $CLUSTER_MOUNT"
        echo "  3. Verify: ./install.sh --verify"
    fi
}

# =============================================================================
# ENTRY POINT
# =============================================================================

parse_args "$@"

if $VERIFY_ONLY; then
    run_verify
    exit $?
fi

if [[ -z "$ROLE" ]]; then
    die "Specify --role node0|node1 or --verify"
fi

[[ "$ROLE" == "node0" || "$ROLE" == "node1" ]] || die "Role must be node0 or node1"

if [[ -n "$STEP" ]]; then
    check_root
    case "$STEP" in
        packages)              step_packages            ;;
        brick-disk)            step_brick_disk          ;;
        gluster-server)        step_gluster_server      ;;
        gluster-volume-create) step_gluster_volume_create ;;
        fstab-mount)           step_fstab_mount         ;;
        node1-mount-unit)      step_node1_mount_unit    ;;
        glusterd-hardening)    step_glusterd_hardening  ;;
        ctdb)                  step_ctdb                ;;
        samba)                 step_samba               ;;
        smbd-dropin)           step_smbd_dropin         ;;
        watchdog)              step_watchdog            ;;
        overlay)               step_overlay             ;;
        verify)                run_verify               ;;
        *)                     die "Unknown step: $STEP. Available: packages brick-disk gluster-server gluster-volume-create fstab-mount node1-mount-unit glusterd-hardening ctdb samba smbd-dropin watchdog overlay verify" ;;
    esac
else
    run_all_phases
fi
