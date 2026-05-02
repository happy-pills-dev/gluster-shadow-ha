#!/usr/bin/env bash
# =============================================================================
# gluster-shadow-ha verify.sh
# Run post-install and post-reboot checks on a single node.
# Can be run without root for read-only checks.
#
# Usage:
#   ./verify.sh                   # auto-detect role from IP
#   ./verify.sh --role node0      # force role
#   ./verify.sh --json            # output JSON (for CI or monitoring)
#   ./verify.sh --quiet           # only print failures
# =============================================================================
set -euo pipefail

NODE0_IP="192.168.1.10"
NODE1_IP="192.168.1.11"
VIP="192.168.1.100"
CLUSTER_MOUNT="/cluster-shared"
SHADOW_MOUNT="/srv/shadow"
GLUSTER_VOL="mydata"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; BLU='\033[0;34m'; NC='\033[0m'
ROLE=""; JSON=false; QUIET=false
PASS=0; FAIL=0; WARN=0
RESULTS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --role)  ROLE="$2"; shift 2 ;;
        --json)  JSON=true; shift ;;
        --quiet) QUIET=true; shift ;;
        *)       echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Auto-detect role
THIS_IP=$(hostname -I | awk '{print $1}')
if [[ -z "$ROLE" ]]; then
    if [[ "$THIS_IP" == "$NODE0_IP" ]]; then ROLE="node0"
    elif [[ "$THIS_IP" == "$NODE1_IP" ]]; then ROLE="node1"
    else ROLE="unknown"; fi
fi

pass() { (( PASS++ )) || true; $QUIET || echo -e "${GRN}  PASS${NC}  $1"; RESULTS+=("{\"check\":\"$1\",\"result\":\"pass\"}"); }
fail() { (( FAIL++ )) || true; echo -e "${RED}  FAIL${NC}  $1"; RESULTS+=("{\"check\":\"$1\",\"result\":\"fail\"}"); }
warn_() { (( WARN++ )) || true; $QUIET || echo -e "${YLW}  WARN${NC}  $1"; RESULTS+=("{\"check\":\"$1\",\"result\":\"warn\"}"); }

chk() {
    local label="$1"; shift
    if eval "$@" &>/dev/null 2>&1; then pass "$label"; else fail "$label"; fi
}
chk_warn() {
    local label="$1"; shift
    if eval "$@" &>/dev/null 2>&1; then pass "$label"; else warn_ "$label"; fi
}

section() { $QUIET || { echo; echo -e "${BLU}--- $* ---${NC}"; }; }

echo -e "${BLU}gluster-shadow-ha verify — $(hostname) [$ROLE] — $(date -u '+%Y-%m-%d %H:%M:%S UTC')${NC}"

# ---------- GlusterFS server ----------
section "GlusterFS"
chk     "glusterd.service active"             "systemctl is-active glusterd"
chk     "gluster peer: at least 1 connected"  "gluster peer status 2>/dev/null | grep -q 'Connected'"
chk     "volume $GLUSTER_VOL is Started"      "gluster volume info $GLUSTER_VOL 2>/dev/null | grep -q 'Status: Started'"
chk     "brick process running"               "pgrep -x glusterfsd"

# ---------- Cluster mount ----------
section "Cluster mount"
chk     "$CLUSTER_MOUNT is a mountpoint"      "mountpoint -q $CLUSTER_MOUNT"
chk     "glusterfs FUSE process running"      "pgrep -f 'glusterfs.*fuse' &>/dev/null || pgrep -f 'process-name fuse'"
chk_warn "$CLUSTER_MOUNT accessible (has content)"  "[ \$(ls $CLUSTER_MOUNT 2>/dev/null | wc -l) -gt 0 ]"

# ---------- Systemd unit state ----------
section "Systemd units"
chk     "smbd.service active"                 "systemctl is-active smbd"
chk     "nmbd.service active"                 "systemctl is-active nmbd"
chk     "ctdb.service active"                 "systemctl is-active ctdb"
chk     "cluster-shared-watchdog.timer active" "systemctl is-active cluster-shared-watchdog.timer"

# ---------- Boot-journal checks (current boot) ----------
section "Boot journal"
chk     "no Condition skip for cluster-shared" \
    "! journalctl -b 0 2>/dev/null | grep -qi 'condition.*cluster.*skip\|cluster.*being skipped'"
chk_warn "cluster-shared mounted at/before multi-user.target" \
    "journalctl -b 0 2>/dev/null | grep -q 'Mounted cluster'"

# ---------- Role-specific checks ----------
if [[ "$ROLE" == "node0" ]]; then
    section "node0 specific"
    chk     "shadow-layer-setup.service active"   "systemctl is-active shadow-layer-setup"
    chk     "$SHADOW_MOUNT is a mountpoint"       "mountpoint -q $SHADOW_MOUNT"
    chk_warn "shadow visible matches cluster" \
        "[ \"\$(ls $SHADOW_MOUNT 2>/dev/null | wc -l)\" -ge \"\$(ls $CLUSTER_MOUNT 2>/dev/null | wc -l)\" ]"
    chk_warn "glusterd hardening drop-in present" \
        "[ -f /etc/systemd/system/glusterd.service.d/wait-for-internal-net.conf ]"
fi

if [[ "$ROLE" == "node1" ]]; then
    section "node1 specific"
    UNIT_PATH="$(printf '/etc/systemd/system/cluster\x2dshared.mount')"
    chk     "manual .mount unit exists"            "[ -f '$UNIT_PATH' ] || [ -L '$UNIT_PATH' ]"
    chk     "_netdev NOT in unit Options"          "! grep -q '_netdev' '$UNIT_PATH' 2>/dev/null"
    chk     "cluster\x2dshared.mount active"       "systemctl is-active 'cluster\x2dshared.mount'"
fi

# ---------- CTDB / VIP ----------
section "CTDB / VIP"
chk_warn "VIP $VIP assigned to this node (one node only)" \
    "ip addr show | grep -q '$VIP'"
chk_warn "ctdb all nodes OK"                   "ctdb status 2>/dev/null | grep -v '#' | grep -qv 'UNHEALTHY'"

# ---------- Watchdog log ----------
section "Watchdog"
LOG=/var/log/cluster-shared-watchdog.log
if [[ -f "$LOG" ]]; then
    LAST=$(tail -1 "$LOG" 2>/dev/null || echo "(empty)")
    if echo "$LAST" | grep -q "OK —"; then
        pass "watchdog last entry: OK"
    elif echo "$LAST" | grep -q "recovery complete"; then
        warn_ "watchdog last entry: recovery (mount needed recovery at last run)"
    elif echo "$LAST" | grep -q "FAILED"; then
        fail "watchdog last entry: FAILED"
    else
        warn_ "watchdog last entry: $LAST"
    fi
    $QUIET || { echo "    ↳ Last 3 entries:"; tail -3 "$LOG" | sed 's/^/      /'; }
else
    warn_ "watchdog log not yet created (timer may not have fired)"
fi

# ---------- Summary ----------
echo
echo -e "${BLU}━━━ Summary ━━━${NC}"
$QUIET || echo -e "  Host:  $(hostname)  [$ROLE]  $THIS_IP"
echo -e "  Pass:  ${GRN}${PASS}${NC}   Fail: ${RED}${FAIL}${NC}   Warn: ${YLW}${WARN}${NC}"

if $JSON; then
    echo
    printf '{"host":"%s","role":"%s","ip":"%s","pass":%d,"fail":%d,"warn":%d,"checks":[' \
        "$(hostname)" "$ROLE" "$THIS_IP" "$PASS" "$FAIL" "$WARN"
    printf '%s,' "${RESULTS[@]}" | sed 's/,$//'
    echo ']}'
fi

if [[ $FAIL -gt 0 ]]; then
    echo
    echo "  Suggested diagnostics:"
    echo "    journalctl -b 0 | grep -E '(cluster.shared|glusterd|ctdb|smbd)' | tail -30"
    echo "    gluster peer status"
    echo "    gluster volume status $GLUSTER_VOL"
    echo "    systemctl status 'cluster\x2dshared.mount'"
    exit 1
fi

exit 0
