#!/usr/bin/env bash
# =============================================================================
# setup-wizard.sh — Interactive TUI installer for gluster-shadow-ha
#
# Uses whiptail (pre-installed on Ubuntu) to guide you through configuration.
# Writes your settings to config.local, then calls install.sh.
#
# Usage:  sudo bash scripts/setup-wizard.sh
# =============================================================================
set -euo pipefail

TITLE="gluster-shadow-ha Setup Wizard"
INSTALL_SH="$(cd "$(dirname "$0")/.." && pwd)/install.sh"
CONFIG_OUT="$(cd "$(dirname "$0")/.." && pwd)/config.local"
W=72  # dialog width

# ─── colour helpers (for non-dialog output) ──────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
BLU='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLU}[INFO]${NC}  $*"; }
ok()    { echo -e "${GRN}[OK]${NC}    $*"; }
warn()  { echo -e "${YLW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

# ─── prerequisites ────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run with sudo: sudo bash scripts/setup-wizard.sh"
command -v whiptail &>/dev/null || apt-get install -y -qq whiptail

# ─── auto-detect network defaults ────────────────────────────────────────────
detect_defaults() {
    AUTO_NIC=$(ip route get 8.8.8.8 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}' \
        | head -1 || echo "eth0")
    AUTO_IP=$(ip route get 8.8.8.8 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' \
        | head -1 || hostname -I | awk '{print $1}')
    # Suggest a VIP by incrementing last octet of local IP by 10
    IP_BASE="${AUTO_IP%.*}"
    IP_LAST="${AUTO_IP##*.}"
    AUTO_VIP="${IP_BASE}.$((IP_LAST + 10))"
}

# ─── list physical block devices for menu ────────────────────────────────────
list_disks() {
    # Returns: "sdb 500G(Samsung_SSD) sdc 2T(WDC_WD20)"
    lsblk -dno NAME,SIZE,MODEL 2>/dev/null \
        | grep -v '^loop\|^sr\|^fd' \
        | awk '{gsub(/ /,"_",$3); printf "%s \"%s %s\" OFF ", $1, $2, $3}'
}

# ─── validate IP format ───────────────────────────────────────────────────────
valid_ip() {
    [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    IFS='.' read -ra O <<< "$1"
    for o in "${O[@]}"; do (( o <= 255 )) || return 1; done
    return 0
}

# ─── check VIP is free (not pingable) ────────────────────────────────────────
check_vip_free() {
    local vip="$1"
    if ping -c1 -W1 "$vip" &>/dev/null; then
        whiptail --title "$TITLE" --msgbox \
"⚠  WARNING: $vip responded to ping.

A VIP must be an UNUSED IP address on your subnet.
CTDB will take ownership of this IP — if another device
already uses it, you will get an IP conflict.

Go back and choose a different VIP." 14 $W
        return 1
    fi
    return 0
}

# ─── list available NICs ──────────────────────────────────────────────────────
list_nics() {
    ip link show 2>/dev/null \
        | awk '/^[0-9]+:/{gsub(/:$/,"",$2); if($2!="lo") printf "%s \"\" OFF ", $2}'
}

# =============================================================================
# WIZARD SCREENS
# =============================================================================

detect_defaults

# ── Welcome ──────────────────────────────────────────────────────────────────
whiptail --title "$TITLE" --msgbox \
"Welcome to the gluster-shadow-ha setup wizard.

This wizard will guide you through configuring a two-node
HA storage cluster with automatic split-brain recovery.

What you need before starting:
  • Two Ubuntu 22.04 or 24.04 servers on the same subnet
  • A dedicated unused block device on each node for the brick
  • Three IP addresses: node0, node1, and a free VIP

Press OK to begin." 18 $W

# ── Role selection ────────────────────────────────────────────────────────────
ROLE=$(whiptail --title "$TITLE" \
    --menu "Which role is THIS node?" 12 $W 2 \
    "node0" "Primary node (shadow layer + shadow overlay)" \
    "node1" "Secondary node (direct GlusterFS mount)" \
    3>&1 1>&2 2>&3) || exit 0

# ── Node IPs ─────────────────────────────────────────────────────────────────
if [[ "$ROLE" == "node0" ]]; then
    DEFAULT_NODE0_IP="$AUTO_IP"
    DEFAULT_NODE1_IP="${IP_BASE}.$((IP_LAST + 1))"
else
    DEFAULT_NODE0_IP="${IP_BASE}.$((IP_LAST - 1))"
    DEFAULT_NODE1_IP="$AUTO_IP"
fi

while true; do
    NODE0_IP=$(whiptail --title "$TITLE" \
        --inputbox "Node 0 (primary) IP address:\n(auto-detected from your network)" \
        10 $W "$DEFAULT_NODE0_IP" 3>&1 1>&2 2>&3) || exit 0
    valid_ip "$NODE0_IP" && break
    whiptail --title "$TITLE" --msgbox "Invalid IP address: $NODE0_IP" 8 $W
done

while true; do
    NODE1_IP=$(whiptail --title "$TITLE" \
        --inputbox "Node 1 (secondary) IP address:" \
        10 $W "$DEFAULT_NODE1_IP" 3>&1 1>&2 2>&3) || exit 0
    valid_ip "$NODE1_IP" && break
    whiptail --title "$TITLE" --msgbox "Invalid IP address: $NODE1_IP" 8 $W
done

# ── VIP ───────────────────────────────────────────────────────────────────────
while true; do
    VIP=$(whiptail --title "$TITLE" \
        --inputbox "Floating VIP (CTDB managed — must be UNUSED on your network):\n\nSuggested free address based on your subnet:" \
        12 $W "$AUTO_VIP" 3>&1 1>&2 2>&3) || exit 0
    valid_ip "$VIP" || { whiptail --title "$TITLE" --msgbox "Invalid IP: $VIP" 8 $W; continue; }
    check_vip_free "$VIP" && break
done

# ── VIP NIC ───────────────────────────────────────────────────────────────────
NIC_LIST=$(list_nics)
if [[ -n "$NIC_LIST" ]]; then
    VIP_NIC=$(eval whiptail --title '"$TITLE"' \
        --radiolist '"Select the network interface for the VIP:"' \
        14 $W 6 $NIC_LIST \
        3>&1 1>&2 2>&3) || exit 0
    # Default to auto-detected if nothing selected
    [[ -z "$VIP_NIC" ]] && VIP_NIC="$AUTO_NIC"
else
    VIP_NIC=$(whiptail --title "$TITLE" \
        --inputbox "Network interface for the VIP (run 'ip link' to list):" \
        10 $W "$AUTO_NIC" 3>&1 1>&2 2>&3) || exit 0
fi

# ── VIP prefix ────────────────────────────────────────────────────────────────
VIP_PREFIX=$(whiptail --title "$TITLE" \
    --inputbox "Subnet prefix length (e.g. 24 for /24 = 255.255.255.0):" \
    10 $W "24" 3>&1 1>&2 2>&3) || exit 0

# ── GlusterFS volume name ────────────────────────────────────────────────────
GLUSTER_VOL=$(whiptail --title "$TITLE" \
    --inputbox "GlusterFS volume name:" \
    10 $W "mydata" 3>&1 1>&2 2>&3) || exit 0

# ── Brick disk ────────────────────────────────────────────────────────────────
DISK_LIST=$(list_disks)
if [[ -n "$DISK_LIST" ]]; then
    BRICK_DEV_NAME=$(eval whiptail --title '"$TITLE"' \
        --radiolist '"Choose the dedicated brick disk.\n(This disk will be FULLY ERASED and formatted as XFS.)"' \
        16 $W 8 $DISK_LIST \
        3>&1 1>&2 2>&3) || exit 0
    BRICK_DEV="/dev/${BRICK_DEV_NAME}"
else
    BRICK_DEV=$(whiptail --title "$TITLE" \
        --inputbox "Block device for the GlusterFS brick (e.g. /dev/sdb):\nWARNING: This disk will be fully erased." \
        12 $W "/dev/sdb" 3>&1 1>&2 2>&3) || exit 0
fi

# ── Arbiter ───────────────────────────────────────────────────────────────────
USE_ARBITER=$(whiptail --title "$TITLE" \
    --yesno "Use a GlusterFS arbiter node?

An arbiter is a lightweight third node (Raspberry Pi, cheap VPS)
that stores only metadata — not data. It prevents split-brain
without the cost of a full third replica.

See templates/arbiter-cloud-init.sh for easy arbiter setup." \
    14 $W 3>&1 1>&2 2>&3 && echo "yes" || echo "no")

ARBITER_IP=""
if [[ "$USE_ARBITER" == "yes" ]]; then
    while true; do
        ARBITER_IP=$(whiptail --title "$TITLE" \
            --inputbox "Arbiter node IP address:" \
            10 $W "" 3>&1 1>&2 2>&3) || exit 0
        valid_ip "$ARBITER_IP" && break
        whiptail --title "$TITLE" --msgbox "Invalid IP: $ARBITER_IP" 8 $W
    done
fi

# ── Samba ─────────────────────────────────────────────────────────────────────
SMB_SHARE=$(whiptail --title "$TITLE" \
    --inputbox "Samba share name  (maps to \\\\<VIP>\\<name>):" \
    10 $W "myshare" 3>&1 1>&2 2>&3) || exit 0

SMB_WORKGROUP=$(whiptail --title "$TITLE" \
    --inputbox "SMB workgroup name:" \
    10 $W "WORKGROUP" 3>&1 1>&2 2>&3) || exit 0

SMB_VALID_USERS=$(whiptail --title "$TITLE" \
    --inputbox "Samba valid users (e.g. @sambausers or a specific username):" \
    10 $W "@sambausers" 3>&1 1>&2 2>&3) || exit 0

# ── DISK FORMAT WARNING ───────────────────────────────────────────────────────
whiptail --title "⚠  DISK FORMAT WARNING" \
    --yesno \
"ALL DATA ON THE FOLLOWING DISK WILL BE PERMANENTLY ERASED:

    $BRICK_DEV

The disk will be partitioned, formatted as XFS, and used
exclusively as a GlusterFS brick.

THIS CANNOT BE UNDONE.

Are you absolutely sure you want to continue?" \
    16 $W || { info "Aborted by user."; exit 0; }

# ── Summary ───────────────────────────────────────────────────────────────────
ARBITER_LINE=""
[[ -n "$ARBITER_IP" ]] && ARBITER_LINE="\n  Arbiter IP:    $ARBITER_IP"

whiptail --title "$TITLE — Summary" --yesno \
"Review your settings before installation begins:

  Role:          $ROLE
  Node 0 IP:     $NODE0_IP
  Node 1 IP:     $NODE1_IP
  VIP:           $VIP / $VIP_PREFIX  on $VIP_NIC$ARBITER_LINE

  GlusterFS vol: $GLUSTER_VOL
  Brick disk:    $BRICK_DEV  ← WILL BE ERASED

  Samba share:   \\\\$VIP\\$SMB_SHARE
  Workgroup:     $SMB_WORKGROUP
  Valid users:   $SMB_VALID_USERS

Press Yes to start installation, No to abort." \
    22 $W || { info "Aborted by user."; exit 0; }

# =============================================================================
# WRITE CONFIG + RUN INSTALL
# =============================================================================

info "Writing config to $CONFIG_OUT"
cat > "$CONFIG_OUT" << EOF
# Generated by setup-wizard.sh on $(date -Iseconds)
# Re-run wizard to regenerate, or edit manually.
NODE0_IP="$NODE0_IP"
NODE1_IP="$NODE1_IP"
ARBITER_IP="$ARBITER_IP"
VIP="$VIP"
VIP_NIC="$VIP_NIC"
VIP_PREFIX="$VIP_PREFIX"
GLUSTER_VOL="$GLUSTER_VOL"
BRICK_DEV="$BRICK_DEV"
SMB_SHARE="$SMB_SHARE"
SMB_WORKGROUP="$SMB_WORKGROUP"
SMB_VALID_USERS="$SMB_VALID_USERS"
EOF
ok "Config saved."

# Inject config into install.sh by sourcing it before the variable block
info "Launching install.sh --role $ROLE ..."
echo
# shellcheck source=/dev/null
set -a; source "$CONFIG_OUT"; set +a
exec bash "$INSTALL_SH" --role "$ROLE"
