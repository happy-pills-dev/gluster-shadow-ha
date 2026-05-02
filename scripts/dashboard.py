#!/usr/bin/env python3
"""
dashboard.py — gluster-shadow-ha live status dashboard

Serves a self-refreshing web UI on http://<node-ip>:9000
Shows GlusterFS heal progress, CTDB health, mount status, watchdog log.
Interactive: trigger heal, resolve split-brain, restart services, remount.

Usage:
    # Install dependency (one-time):
    pip3 install flask

    # Run (as root for gluster/ctdb commands):
    sudo python3 scripts/dashboard.py

    # Or as a systemd service:
    sudo systemctl enable --now gluster-dashboard  # (after running install.sh)

Environment variables:
    DASHBOARD_PORT   default 9000
    GLUSTER_VOL      default mydata
    CLUSTER_MOUNT    default /cluster-shared
    SHADOW_MOUNT     default /srv/shadow
    WATCHDOG_LOG     default /var/log/cluster-shared-watchdog.log
"""

import subprocess, socket, os, json, time, re
from datetime import datetime

try:
    import psutil
    PSUTIL = True
except ImportError:
    PSUTIL = False

# state for network rate calculation (persists across requests)
_net_state = {"prev": None, "t": 0.0}

try:
    from flask import Flask, jsonify, render_template_string, request as flask_request
    FLASK = True
except ImportError:
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import urllib.parse
    FLASK = False

# ── config ────────────────────────────────────────────────────────────────────
PORT          = int(os.environ.get("DASHBOARD_PORT", "9000"))
VOL           = os.environ.get("GLUSTER_VOL",   "mydata")
CLUSTER_MOUNT = os.environ.get("CLUSTER_MOUNT", "/cluster-shared")
SHADOW_MOUNT  = os.environ.get("SHADOW_MOUNT",  "/srv/shadow")
WATCHDOG_LOG  = os.environ.get("WATCHDOG_LOG",  "/var/log/cluster-shared-watchdog.log")
HOSTNAME      = socket.gethostname()

# Services that may be restarted via the UI (explicit allowlist)
RESTARTABLE_SVCS = {"glusterd", "smbd", "nmbd", "ctdb", "shadow-layer-setup"}

# ── shell helpers ─────────────────────────────────────────────────────────────
def sh(cmd, timeout=6):
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.STDOUT,
            timeout=timeout).decode(errors="replace")
    except Exception:
        return ""

def mountpoint(path):
    return os.path.ismount(path)

def _fmt_bytes(b):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024: return f"{b:.0f} {u}"
        b /= 1024
    return f"{b:.1f} PB"

def disk_free(path):
    try:
        s = os.statvfs(path)
        total = s.f_blocks * s.f_frsize
        free  = s.f_bavail * s.f_frsize
        pct   = int(100 * (total - free) / total) if total else 0
        def fmt(b):
            for u in ("B","KB","MB","GB","TB"):
                if b < 1024: return f"{b:.1f} {u}"
                b /= 1024
            return f"{b:.1f} PB"
        return {"total": fmt(total), "free": fmt(free), "used_pct": pct}
    except Exception:
        return {"total": "?", "free": "?", "used_pct": 0}

# ── data collectors ────────────────────────────────────────────────────────────
def collect_gluster():
    raw_peer  = sh("gluster peer status")
    raw_vol   = sh(f"gluster volume status {VOL} 2>/dev/null || gluster volume info {VOL} 2>/dev/null")
    raw_heal  = sh(f"gluster volume heal {VOL} info 2>/dev/null")
    raw_split = sh(f"gluster volume heal {VOL} info split-brain 2>/dev/null")
    raw_info  = sh(f"gluster volume info {VOL}")

    peers_connected = len(re.findall(r"State:.*Connected", raw_peer))
    peers_total     = len(re.findall(r"^Hostname:", raw_peer, re.M))
    vol_started     = "Started" in raw_info

    # GlusterFS 11+ uses columnar output: "Brick host:path  PORT  RDMA  Y  PID"
    # Older versions use key-value:       "Brick Path : ...\nOnline     : Y"
    bricks_total  = len(re.findall(r"^Brick\s+\S+", raw_vol, re.M))
    bricks_online = len(re.findall(r"^Brick\s+\S+.*\s+Y\s+\d+", raw_vol, re.M))
    if bricks_total == 0:  # fall back to old key-value format
        bricks_online = len(re.findall(r"Online\s*:\s*Y", raw_vol, re.I))
        bricks_total  = len(re.findall(r"Brick Path\s*:", raw_vol, re.I))

    # Healing: sum "Number of entries: N" across all bricks
    heal_counts  = [int(x) for x in re.findall(r"Number of entries:\s*(\d+)", raw_heal)]
    heal_pending = sum(heal_counts)

    # Split-brain file list (strip internal gluster paths)
    split_files = re.findall(r"^\s*/[^\n]+", raw_split, re.M)
    split_files = [f.strip() for f in split_files if "/gluster" not in f and len(f.strip()) > 1]

    return {
        "peers_connected": peers_connected,
        "peers_total":     peers_total,
        "vol_started":     vol_started,
        "bricks_online":   bricks_online,
        "bricks_total":    bricks_total,
        "heal_pending":    heal_pending,
        "split_brain_files": split_files[:10],
        "raw_vol_status":  raw_vol[:2000],
    }

def collect_ctdb():
    raw       = sh("ctdb status 2>/dev/null")
    nodes_ok  = len(re.findall(r"\bOK\b", raw))
    nodes_tot = len(re.findall(r"^pnn:", raw, re.M))
    ctdb_up   = nodes_ok > 0 or "active" == sh("systemctl is-active ctdb 2>/dev/null").strip()

    vip_raw  = sh("cat /etc/ctdb/public_addresses 2>/dev/null").strip().split()
    vip_addr = vip_raw[0].split("/")[0] if vip_raw else ""
    has_vip  = bool(vip_addr and sh(f"ip addr show | grep -qw {vip_addr} && echo yes").strip())

    return {
        "ctdb_active": ctdb_up,
        "nodes_ok":    nodes_ok,
        "nodes_total": nodes_tot,
        "holds_vip":   has_vip,
        "vip_addr":    vip_addr,
    }

def collect_mounts():
    cluster_up = mountpoint(CLUSTER_MOUNT)
    shadow_up  = mountpoint(SHADOW_MOUNT)

    node0_ip = sh("awk 'NR==1' /etc/ctdb/nodes 2>/dev/null").strip()
    this_ip  = sh("hostname -I | awk '{print $1}'").strip()
    is_node0 = (this_ip == node0_ip)

    return {
        "cluster_mounted": cluster_up,
        "shadow_mounted":  shadow_up,
        "is_node0":        is_node0,
        "cluster_disk":    disk_free(CLUSTER_MOUNT) if cluster_up else None,
        "shadow_disk":     disk_free(SHADOW_MOUNT)  if shadow_up  else None,
    }

def collect_watchdog():
    lines = []
    try:
        with open(WATCHDOG_LOG) as f:
            lines = f.readlines()[-15:]
    except Exception:
        pass
    status = "unknown"
    if lines:
        last = lines[-1]
        if "OK —"             in last: status = "ok"
        elif "recovery complete" in last: status = "recovered"
        elif "FAILED"         in last: status = "failed"
        elif "NOT mounted"    in last: status = "recovering"
    return {"lines": [l.rstrip() for l in lines], "status": status}

def collect_services():
    svcs = ["glusterd", "smbd", "nmbd", "ctdb",
            "shadow-layer-setup", "cluster-shared-watchdog.timer"]
    result = {}
    for s in svcs:
        active = sh(f"systemctl is-active {s} 2>/dev/null").strip()
        result[s] = (active == "active")
    return result

def collect_splitbrain_detail():
    """Per-file split-brain info with local-brick file stats for the UI."""
    raw_info  = sh(f"gluster volume info {VOL}")
    raw_split = sh(f"gluster volume heal {VOL} info split-brain 2>/dev/null", timeout=15)

    # Parse brick specs from volume info: "Brick1: host:/path"
    brick_specs = []
    for line in raw_info.splitlines():
        m = re.match(r"Brick\d+:\s+(\S+):(\S+)", line)
        if m:
            brick_specs.append({
                "host": m.group(1),
                "path": m.group(2),
                "spec": f"{m.group(1)}:{m.group(2)}",
            })

    # Determine which bricks are on this node
    this_ips = set(sh("hostname -I").split()) | {HOSTNAME, "localhost", "127.0.0.1"}

    # Parse split-brain output per-brick file lists
    current_brick = None
    files_by_brick = {}
    for line in raw_split.splitlines():
        ls = line.strip()
        m  = re.match(r"^Brick\s+(\S+:\S+)", ls)
        if m:
            current_brick = m.group(1)
            files_by_brick.setdefault(current_brick, [])
        elif ls.startswith("/") and current_brick and "/gluster" not in ls:
            files_by_brick[current_brick].append(ls)

    all_files = sorted({f for fl in files_by_brick.values() for f in fl})

    def fmt_size(b):
        for u in ("B", "KB", "MB", "GB"):
            if b < 1024: return f"{b:.0f} {u}"
            b /= 1024
        return f"{b:.1f} GB"

    result = []
    for fpath in all_files:
        bricks_info = []
        for bspec in brick_specs:
            is_local = bspec["host"] in this_ips
            if is_local:
                full = bspec["path"].rstrip("/") + "/" + fpath.lstrip("/")
                try:
                    s = os.stat(full)
                    bricks_info.append({
                        "brick":    bspec["spec"],
                        "local":    True,
                        "exists":   True,
                        "size_fmt": fmt_size(s.st_size),
                        "mtime":    datetime.utcfromtimestamp(s.st_mtime)
                                    .strftime("%Y-%m-%d %H:%M UTC"),
                    })
                except Exception:
                    bricks_info.append({
                        "brick":    bspec["spec"],
                        "local":    True,
                        "exists":   False,
                        "size_fmt": "—",
                        "mtime":    "—",
                    })
            else:
                bricks_info.append({
                    "brick":    bspec["spec"],
                    "local":    False,
                    "exists":   None,
                    "size_fmt": "remote",
                    "mtime":    "—",
                })
        result.append({"path": fpath, "bricks": bricks_info})

    return result

def collect_system():
    """CPU %, RAM %, disk %, and network throughput via psutil."""
    if not PSUTIL:
        return {"available": False}

    cpu_pct = psutil.cpu_percent(interval=None)   # non-blocking; samples since last call
    mem     = psutil.virtual_memory()

    # disk — prefer cluster mount, fall back to /
    try:
        dp   = CLUSTER_MOUNT if mountpoint(CLUSTER_MOUNT) else "/"
        du   = psutil.disk_usage(dp)
        disk_pct = round(du.percent, 1)
    except Exception:
        disk_pct = 0

    # network rate — delta since last call
    net = psutil.net_io_counters()
    now = time.time()
    rx_bps = tx_bps = 0
    prev = _net_state["prev"]
    if prev is not None:
        dt = now - _net_state["t"]
        if dt > 0.05:
            rx_bps = max(0, int((net.bytes_recv - prev.bytes_recv) / dt))
            tx_bps = max(0, int((net.bytes_sent - prev.bytes_sent) / dt))
    _net_state["prev"] = net
    _net_state["t"]    = now

    return {
        "available": True,
        "cpu_pct":   round(cpu_pct, 1),
        "mem_pct":   round(mem.percent, 1),
        "mem_used":  _fmt_bytes(mem.used),
        "mem_total": _fmt_bytes(mem.total),
        "disk_pct":  disk_pct,
        "rx_bps":    rx_bps,
        "tx_bps":    tx_bps,
        "rx_fmt":    _fmt_bytes(rx_bps) + "/s",
        "tx_fmt":    _fmt_bytes(tx_bps) + "/s",
    }

def get_all_status():
    return {
        "hostname":  HOSTNAME,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "gluster":   collect_gluster(),
        "ctdb":      collect_ctdb(),
        "mounts":    collect_mounts(),
        "watchdog":  collect_watchdog(),
        "services":  collect_services(),
        "system":    collect_system(),
    }

# ── HTML template ─────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>gluster-shadow-ha — {{ data.hostname }}</title>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#0d1117;--surface:#161b22;--border:#30363d;
    --text:#c9d1d9;--muted:#8b949e;
    --green:#56d364;--red:#f85149;--yellow:#d29922;--blue:#388bfd;
    --radius:8px;
  }
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:var(--bg);color:var(--text);padding:1.5rem 1rem;line-height:1.6}
  .container{max-width:960px;margin:0 auto}
  header{display:flex;align-items:center;justify-content:space-between;
    border-bottom:1px solid var(--border);padding-bottom:1rem;margin-bottom:1.5rem;flex-wrap:wrap;gap:.5rem}
  header h1{font-size:1.3rem;color:#e6edf3}
  header h1 span{color:var(--blue)}
  .meta{font-size:.8rem;color:var(--muted)}
  .refresh-badge{font-size:.75rem;color:var(--muted);background:var(--surface);
    border:1px solid var(--border);border-radius:2rem;padding:.2rem .7rem;cursor:pointer}
  .refresh-badge:hover{border-color:var(--blue);color:var(--blue)}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin-bottom:1rem}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:1.25rem}
  .card.alert{border-color:var(--red)}
  .card.warn{border-color:var(--yellow)}
  .card h2{font-size:.85rem;text-transform:uppercase;letter-spacing:.05em;
    color:var(--muted);margin-bottom:1rem}
  .row{display:flex;justify-content:space-between;align-items:center;
    padding:.3rem 0;border-bottom:1px solid var(--border);font-size:.88rem}
  .row:last-child{border-bottom:none}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:.4rem;flex-shrink:0}
  .dot.green{background:var(--green)}.dot.red{background:var(--red)}
  .dot.yellow{background:var(--yellow)}.dot.muted{background:var(--muted)}
  .pill{font-size:.75rem;padding:.15rem .5rem;border-radius:2rem;font-weight:600}
  .pill.green{background:rgba(86,211,100,.15);color:var(--green)}
  .pill.red{background:rgba(248,81,73,.15);color:var(--red)}
  .pill.yellow{background:rgba(210,153,34,.15);color:var(--yellow)}
  .pill.blue{background:rgba(56,139,253,.15);color:var(--blue)}
  .bar-wrap{margin:.75rem 0 .25rem;background:var(--bg);border-radius:4px;height:8px;overflow:hidden}
  .bar{height:8px;border-radius:4px;transition:width .5s ease;background:var(--green)}
  .bar.warn{background:var(--yellow)}.bar.full{background:var(--red)}
  .log{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);
    padding:.75rem;font-family:monospace;font-size:.75rem;max-height:200px;overflow-y:auto;margin-top:.5rem}
  .log-ok{color:var(--green)}.log-warn{color:var(--yellow)}
  .log-err{color:var(--red)}.log-info{color:var(--muted)}
  .split-alert{background:rgba(248,81,73,.06);border:1px solid rgba(248,81,73,.5);
    border-radius:var(--radius);padding:.75rem;font-size:.82rem;color:var(--red);margin-top:.75rem}
  .split-alert code{font-family:monospace;font-size:.78rem;display:block;
    margin-top:.2rem;padding-left:.5rem;opacity:.8;word-break:break-all}
  .vip-badge{background:rgba(56,139,253,.15);color:var(--blue);border:1px solid var(--blue);
    border-radius:2rem;padding:.15rem .6rem;font-size:.75rem;font-weight:600}
  .heal-label{font-size:.8rem;color:var(--muted);margin-top:.3rem}
  #countdown{display:inline-block;min-width:1.5em;text-align:center}

  /* ── action buttons ── */
  .btn{border:none;border-radius:4px;cursor:pointer;font-size:.75rem;
    padding:.28rem .65rem;font-weight:600;transition:opacity .15s,transform .1s;white-space:nowrap;line-height:1.4}
  .btn:hover{opacity:.82}.btn:active{transform:scale(.96)}
  .btn:disabled{opacity:.35;cursor:not-allowed;pointer-events:none}
  .btn-blue{background:rgba(56,139,253,.18);color:var(--blue);border:1px solid rgba(56,139,253,.4)}
  .btn-warn{background:rgba(210,153,34,.18);color:var(--yellow);border:1px solid rgba(210,153,34,.4)}
  .btn-danger{background:rgba(248,81,73,.18);color:var(--red);border:1px solid rgba(248,81,73,.4)}
  .btn-green{background:rgba(86,211,100,.18);color:var(--green);border:1px solid rgba(86,211,100,.4)}
  .btn-sm{padding:.15rem .4rem;font-size:.7rem}
  .card-actions{margin-top:.75rem;display:flex;gap:.5rem;flex-wrap:wrap;padding-top:.5rem;
    border-top:1px solid var(--border)}

  /* ── toast notifications ── */
  #toast-wrap{position:fixed;bottom:1.5rem;right:1.5rem;z-index:9999;
    display:flex;flex-direction:column;gap:.5rem;pointer-events:none;max-width:380px;width:calc(100% - 3rem)}
  .toast{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
    padding:.6rem 1rem;font-size:.82rem;box-shadow:0 4px 20px rgba(0,0,0,.6);pointer-events:all;
    transition:opacity .35s,transform .35s;display:flex;align-items:flex-start;gap:.6rem}
  .toast.ok{border-left:3px solid var(--green)}.toast.err{border-left:3px solid var(--red)}
  .toast.info{border-left:3px solid var(--blue)}
  .toast .t-icon{font-size:.9rem;line-height:1.5;flex-shrink:0}
  .toast .t-msg{flex:1;word-break:break-word}
  .toast .t-x{cursor:pointer;opacity:.5;flex-shrink:0;align-self:center;
    background:none;border:none;color:inherit;font-size:1rem;line-height:1;padding:0}
  .toast .t-x:hover{opacity:1}

  /* ── modal ── */
  .overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);
    z-index:1000;align-items:center;justify-content:center;padding:1rem}
  .overlay.open{display:flex}
  .modal{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
    padding:1.5rem;max-width:680px;width:100%;max-height:88vh;overflow-y:auto;
    box-shadow:0 8px 40px rgba(0,0,0,.7)}
  .modal-hdr{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1rem}
  .modal-hdr h3{font-size:1.05rem;color:#e6edf3;line-height:1.3}
  .modal-x{cursor:pointer;color:var(--muted);font-size:1.2rem;background:none;
    border:none;color:var(--muted);line-height:1;padding:.1rem .3rem;border-radius:4px}
  .modal-x:hover{color:var(--text);background:var(--bg)}
  .modal-body{font-size:.85rem;color:var(--text)}
  .modal-body p{color:var(--muted);margin-bottom:.9rem;line-height:1.5}
  .modal-section{margin-bottom:1.25rem}
  .modal-section h4{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;
    color:var(--muted);margin-bottom:.6rem;padding-bottom:.3rem;border-bottom:1px solid var(--border)}
  .modal-footer{display:flex;justify-content:flex-end;gap:.5rem;
    padding-top:1rem;margin-top:.5rem;border-top:1px solid var(--border)}
  .policy-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:.5rem}

  /* ── system metrics chart ── */
  .chart-wrap{width:100%;overflow:hidden}
  .chart-wrap svg{display:block;width:100%;height:auto}
  .sys-legend{display:flex;flex-wrap:wrap;gap:.35rem 1rem;margin-top:.6rem;font-size:.72rem;color:var(--muted)}
  .sys-legend span::before{content:"●";margin-right:.25rem}
  .sys-legend .lg{color:var(--green)}.sys-legend .ly{color:var(--yellow)}.sys-legend .lr{color:var(--red)}.sys-legend .lb{color:var(--blue)}
  .file-card{background:var(--bg);border:1px solid var(--border);border-radius:4px;
    padding:.75rem;margin-bottom:.6rem}
  .file-card .fc-path{font-family:monospace;font-weight:600;margin-bottom:.5rem;
    word-break:break-all;color:#e6edf3;font-size:.82rem}
  .brick-row{display:flex;justify-content:space-between;align-items:center;
    padding:.25rem 0;border-bottom:1px solid var(--border);gap:.5rem;flex-wrap:wrap}
  .brick-row:last-child{border-bottom:none}
  .brick-id{font-family:monospace;font-size:.73rem;color:var(--muted);flex:1;min-width:0;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .brick-meta{font-size:.75rem;color:var(--text);white-space:nowrap}
</style>
</head>
<body>
<div class="container">

<header>
  <div>
    <h1><span>gluster</span>-shadow-ha &nbsp;·&nbsp; {{ data.hostname }}</h1>
    <div class="meta">{{ data.timestamp }}
      {% if data.ctdb.holds_vip %}
      &nbsp;&nbsp;<span class="vip-badge">⚡ holds VIP {{ data.ctdb.vip_addr }}</span>
      {% endif %}
    </div>
  </div>
  <span class="refresh-badge" onclick="location.reload()" title="Click to refresh now">
    ↻ refresh in <span id="countdown">30</span>s
  </span>
</header>

<!-- Overall health bar -->
{% set total_checks = 7 %}
{% set passing = (data.services['glusterd']|int) + (data.gluster.vol_started|int) +
    (data.mounts.cluster_mounted|int) + (data.services['smbd']|int) +
    (data.services['ctdb']|int) + (data.ctdb.ctdb_active|int) +
    ((data.watchdog.status == 'ok')|int) %}
{% set pct = (passing / total_checks * 100)|int %}
<div class="card" style="margin-bottom:1rem">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <h2 style="margin:0">cluster health</h2>
    <span class="pill {% if pct == 100 %}green{% elif pct >= 70 %}yellow{% else %}red{% endif %}">
      {{ passing }}/{{ total_checks }} checks passing
    </span>
  </div>
  <div class="bar-wrap" style="margin:.5rem 0 0">
    <div class="bar {% if pct < 100 %}warn{% endif %}" style="width:{{ pct }}%"></div>
  </div>
</div>

<div class="grid">

<!-- ── GlusterFS card ───────────────────────────────────────────────────────── -->
<div class="card {% if not data.gluster.vol_started %}alert{% endif %}">
  <h2>GlusterFS</h2>

  <div class="row">
    <span><span class="dot {% if data.gluster.peers_connected > 0 %}green{% else %}red{% endif %}"></span>Peers connected</span>
    <span class="pill {% if data.gluster.peers_connected > 0 %}green{% else %}red{% endif %}">
      {{ data.gluster.peers_connected }} / {{ data.gluster.peers_total }}
    </span>
  </div>

  <div class="row">
    <span><span class="dot {% if data.gluster.vol_started %}green{% else %}red{% endif %}"></span>Volume {{ VOL }}</span>
    <span class="pill {% if data.gluster.vol_started %}green{% else %}red{% endif %}">
      {% if data.gluster.vol_started %}Started{% else %}Stopped{% endif %}
    </span>
  </div>

  <div class="row">
    <span>
      <span class="dot {% if data.gluster.bricks_online == data.gluster.bricks_total and data.gluster.bricks_total > 0 %}green{% elif data.gluster.bricks_online > 0 %}yellow{% else %}red{% endif %}"></span>
      Bricks online
    </span>
    <span class="pill {% if data.gluster.bricks_online == data.gluster.bricks_total and data.gluster.bricks_total > 0 %}green{% elif data.gluster.bricks_online > 0 %}yellow{% else %}red{% endif %}">
      {{ data.gluster.bricks_online }} / {{ data.gluster.bricks_total }}
    </span>
  </div>

  <!-- Self-heal status -->
  <div style="margin-top:.75rem">
    {% if data.gluster.heal_pending == 0 %}
      <div style="color:var(--green);font-size:.85rem">✓ Fully in sync — no pending heal entries</div>
    {% else %}
      <div style="font-size:.85rem;color:var(--yellow)">⟳ Self-heal in progress</div>
      <div class="bar-wrap">
        <div class="bar warn" style="width:100%;animation:pulse 1.5s infinite alternate"></div>
      </div>
      <div class="heal-label">{{ data.gluster.heal_pending }} entries pending reconciliation</div>
    {% endif %}
  </div>

  <!-- Split-brain alert -->
  {% if data.gluster.split_brain_files %}
  <div class="split-alert">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.4rem">
      <strong>⚠ Split-brain: {{ data.gluster.split_brain_files|length }} file(s)</strong>
      <button class="btn btn-danger btn-sm" onclick="openSbModal()">Resolve…</button>
    </div>
    {% for f in data.gluster.split_brain_files %}
    <code>{{ f }}</code>
    {% endfor %}
  </div>
  {% endif %}

  <!-- GlusterFS actions -->
  <div class="card-actions">
    <button class="btn btn-blue" id="btn-heal"
      onclick="doAction('heal',{},'Trigger a full self-heal cycle on {{ VOL }}?','btn-heal')">
      ⟳ Trigger Heal
    </button>
    {% if data.gluster.split_brain_files %}
    <button class="btn btn-warn" onclick="openSbModal()">⚠ Resolve Split-Brain</button>
    {% endif %}
  </div>
</div>

<!-- ── CTDB / VIP card ───────────────────────────────────────────────────────── -->
<div class="card {% if not data.ctdb.ctdb_active %}alert{% endif %}">
  <h2>CTDB / Floating VIP</h2>

  <div class="row">
    <span><span class="dot {% if data.ctdb.ctdb_active %}green{% else %}red{% endif %}"></span>CTDB daemon</span>
    <div style="display:flex;align-items:center;gap:.4rem">
      <span class="pill {% if data.ctdb.ctdb_active %}green{% else %}red{% endif %}">
        {% if data.ctdb.ctdb_active %}Active{% else %}Down{% endif %}
      </span>
      {% if not data.ctdb.ctdb_active %}
      <button class="btn btn-warn btn-sm" id="btn-ctdb"
        onclick="doAction('restart_service',{service:'ctdb'},'Restart CTDB daemon?','btn-ctdb')">↺ restart</button>
      {% endif %}
    </div>
  </div>

  <div class="row">
    <span><span class="dot {% if data.ctdb.nodes_ok > 0 %}green{% else %}yellow{% endif %}"></span>Nodes healthy</span>
    <span class="pill {% if data.ctdb.nodes_ok == data.ctdb.nodes_total and data.ctdb.nodes_total > 0 %}green{% else %}yellow{% endif %}">
      {{ data.ctdb.nodes_ok }} / {{ data.ctdb.nodes_total }}
    </span>
  </div>

  <div class="row">
    <span><span class="dot {% if data.ctdb.holds_vip %}blue{% else %}muted{% endif %}"></span>VIP ownership</span>
    {% if data.ctdb.holds_vip %}
      <span class="pill blue">This node holds VIP</span>
    {% elif data.ctdb.vip_addr %}
      <span class="pill" style="color:var(--muted)">VIP on other node</span>
    {% else %}
      <span class="pill red">VIP unassigned</span>
    {% endif %}
  </div>
</div>

<!-- ── Mounts card ───────────────────────────────────────────────────────────── -->
<div class="card {% if not data.mounts.cluster_mounted %}alert{% endif %}">
  <h2>Storage mounts</h2>

  <div class="row">
    <span><span class="dot {% if data.mounts.cluster_mounted %}green{% else %}red{% endif %}"></span>{{ CLUSTER_MOUNT }}</span>
    <div style="display:flex;align-items:center;gap:.4rem">
      <span class="pill {% if data.mounts.cluster_mounted %}green{% else %}red{% endif %}">
        {% if data.mounts.cluster_mounted %}Mounted{% else %}Not mounted{% endif %}
      </span>
      {% if not data.mounts.cluster_mounted %}
      <button class="btn btn-warn btn-sm" id="btn-mnt-cluster"
        onclick="doAction('remount',{target:'cluster'},'Remount {{ CLUSTER_MOUNT }}?','btn-mnt-cluster')">↺ mount</button>
      {% endif %}
    </div>
  </div>

  {% if data.mounts.cluster_mounted and data.mounts.cluster_disk %}
  {% set d = data.mounts.cluster_disk %}
  <div class="bar-wrap">
    <div class="bar {% if d.used_pct > 90 %}full{% elif d.used_pct > 75 %}warn{% endif %}"
         style="width:{{ d.used_pct }}%"></div>
  </div>
  <div class="heal-label">{{ d.free }} free of {{ d.total }} ({{ d.used_pct }}% used)</div>
  {% endif %}

  {% if data.mounts.is_node0 %}
  <div class="row" style="margin-top:.5rem">
    <span><span class="dot {% if data.mounts.shadow_mounted %}green{% else %}yellow{% endif %}"></span>{{ SHADOW_MOUNT }} (shadow)</span>
    <div style="display:flex;align-items:center;gap:.4rem">
      <span class="pill {% if data.mounts.shadow_mounted %}green{% else %}yellow{% endif %}">
        {% if data.mounts.shadow_mounted %}Mounted{% else %}Not mounted{% endif %}
      </span>
      {% if not data.mounts.shadow_mounted %}
      <button class="btn btn-warn btn-sm" id="btn-mnt-shadow"
        onclick="doAction('remount',{target:'shadow'},'Remount {{ SHADOW_MOUNT }}?','btn-mnt-shadow')">↺ mount</button>
      {% endif %}
    </div>
  </div>
  {% endif %}
</div>

<!-- ── Services card ─────────────────────────────────────────────────────────── -->
<div class="card">
  <h2>Services</h2>
  {% for svc, active in data.services.items() %}
  <div class="row">
    <span><span class="dot {% if active %}green{% else %}red{% endif %}"></span>{{ svc }}</span>
    <div style="display:flex;align-items:center;gap:.4rem">
      <span class="pill {% if active %}green{% else %}red{% endif %}">
        {% if active %}active{% else %}inactive{% endif %}
      </span>
      {% if not active and svc in ['glusterd','smbd','nmbd','ctdb','shadow-layer-setup'] %}
      <button class="btn btn-warn btn-sm" id="btn-svc-{{ svc }}"
        onclick="doAction('restart_service',{service:'{{ svc }}'},'Restart {{ svc }}?','btn-svc-{{ svc }}')">↺</button>
      {% endif %}
    </div>
  </div>
  {% endfor %}
</div>

</div><!-- /grid -->

<!-- ── System metrics card ───────────────────────────────────────────────────── -->
<div class="card" style="margin-bottom:1rem">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:.75rem">
    <h2 style="margin:0">System metrics</h2>
    <span style="font-size:.72rem;color:var(--muted)" id="sys-ts">—</span>
  </div>
  <div class="chart-wrap" id="sys-chart">
    <div style="color:var(--muted);font-size:.85rem;padding:.5rem 0">Loading…</div>
  </div>
  <div class="sys-legend">
    <span class="lg">≤ 70% — normal</span>
    <span class="ly">70–90% — elevated</span>
    <span class="lr">&gt; 90% — critical</span>
    <span class="lb">network (auto-scaled)</span>
  </div>
</div>

<!-- ── Watchdog log card ─────────────────────────────────────────────────────── -->
<div class="card">
  <h2>Watchdog log
    <span class="pill
      {% if data.watchdog.status == 'ok' %}green
      {% elif data.watchdog.status == 'recovered' %}blue
      {% elif data.watchdog.status == 'failed' %}red
      {% else %}yellow{% endif %}" style="margin-left:.5rem">
      {{ data.watchdog.status }}
    </span>
  </h2>
  <div class="log" id="wdlog">
    {% for line in data.watchdog.lines %}
    <div class="{% if 'OK —' in line %}log-ok{% elif 'FAILED' in line or 'failed' in line %}log-err{% elif 'recovery' in line or 'NOT mounted' in line %}log-warn{% else %}log-info{% endif %}">{{ line }}</div>
    {% endfor %}
    {% if not data.watchdog.lines %}
    <div class="log-info">(no entries yet — timer fires 2 min after boot)</div>
    {% endif %}
  </div>
</div>

</div><!-- /container -->

<!-- ── Toast container ──────────────────────────────────────────────────────── -->
<div id="toast-wrap"></div>

<!-- ── Split-brain modal ────────────────────────────────────────────────────── -->
<div class="overlay" id="sb-overlay" onclick="closeSbModal()">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-hdr">
      <h3>⚠ Resolve Split-Brain</h3>
      <button class="modal-x" onclick="closeSbModal()">✕</button>
    </div>
    <div class="modal-body">
      <p>
        Split-brain means both nodes have conflicting versions of the same file and GlusterFS
        cannot decide which is authoritative. Choose a resolution strategy below.
        <strong>The winning copy overwrites the other — this is irreversible.</strong>
      </p>

      <div class="modal-section">
        <h4>Automatic policy &mdash; resolves all affected files at once</h4>
        <div class="policy-grid">
          <button class="btn btn-blue" onclick="resolvePolicy('mtime')">📅 Newest mtime<br><small style="font-weight:400;opacity:.75">last write time</small></button>
          <button class="btn btn-blue" onclick="resolvePolicy('ctime')">🕐 Newest ctime<br><small style="font-weight:400;opacity:.75">last metadata change</small></button>
          <button class="btn btn-blue" onclick="resolvePolicy('size')">📦 Largest file<br><small style="font-weight:400;opacity:.75">most data wins</small></button>
          <button class="btn btn-blue" onclick="resolvePolicy('majority')">🗳 Majority<br><small style="font-weight:400;opacity:.75">quorum-based</small></button>
        </div>
      </div>

      <div class="modal-section">
        <h4>Per-file &mdash; choose source brick manually</h4>
        <div id="sb-files">
          <div style="color:var(--muted);font-size:.85rem;padding:.5rem 0">Loading file details…</div>
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-blue" onclick="closeSbModal()">Close</button>
    </div>
  </div>
</div>

<style>@keyframes pulse{from{opacity:.6}to{opacity:1}}</style>
<script>
// ── globals ────────────────────────────────────────────────────────────────
var VOL = {{ VOL|tojson }};
var autoSec = 30;
var cdEl    = document.getElementById('countdown');
var cdTimer = null;

function startTimer() {
  clearInterval(cdTimer);
  autoSec = 30;
  cdEl.textContent = autoSec;
  cdTimer = setInterval(function() {
    autoSec--;
    cdEl.textContent = autoSec;
    if (autoSec <= 0) { clearInterval(cdTimer); location.reload(); }
  }, 1000);
}
function pauseTimer() {
  clearInterval(cdTimer);
  cdEl.textContent = '—';
}
startTimer();

// Scroll watchdog log to bottom
var wdlog = document.getElementById('wdlog');
if (wdlog) wdlog.scrollTop = wdlog.scrollHeight;

// ── toast system ──────────────────────────────────────────────────────────
function toast(msg, type) {
  type = type || 'info';
  var icons = { ok: '✓', err: '✗', info: 'ℹ' };
  var wrap = document.getElementById('toast-wrap');
  var el = document.createElement('div');
  el.className = 'toast ' + type;
  el.innerHTML =
    '<span class="t-icon">' + (icons[type] || 'ℹ') + '</span>' +
    '<span class="t-msg">' + esc(msg) + '</span>' +
    '<button class="t-x" onclick="this.parentNode.remove()">✕</button>';
  wrap.appendChild(el);
  setTimeout(function() {
    el.style.opacity = '0';
    el.style.transform = 'translateX(2rem)';
    setTimeout(function() { if (el.parentNode) el.remove(); }, 400);
  }, 6000);
}

// ── action dispatcher ─────────────────────────────────────────────────────
function doAction(action, params, confirmMsg, btnId) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  var btn = btnId ? document.getElementById(btnId) : null;
  if (btn) btn.disabled = true;
  pauseTimer();
  toast('Sending ' + action + '…', 'info');
  var body = Object.assign({ action: action }, params);
  fetch('/api/action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.ok) {
      toast(d.output || (action + ' completed'), 'ok');
      setTimeout(function() { location.reload(); }, 1800);
    } else {
      toast('Error: ' + (d.output || 'unknown error'), 'err');
      if (btn) btn.disabled = false;
      startTimer();
    }
  })
  .catch(function(e) {
    toast('Request failed: ' + e, 'err');
    if (btn) btn.disabled = false;
    startTimer();
  });
}

// ── split-brain modal ─────────────────────────────────────────────────────
function openSbModal() {
  pauseTimer();
  document.getElementById('sb-overlay').classList.add('open');
  document.getElementById('sb-files').innerHTML =
    '<div style="color:var(--muted);font-size:.85rem;padding:.5rem 0">Loading file details…</div>';
  fetch('/api/splitbrain_detail')
    .then(function(r) { return r.json(); })
    .then(renderSbFiles)
    .catch(function(e) {
      document.getElementById('sb-files').innerHTML =
        '<div style="color:var(--red)">Failed to load: ' + esc(String(e)) + '</div>';
    });
}

function closeSbModal() {
  document.getElementById('sb-overlay').classList.remove('open');
  startTimer();
}

function renderSbFiles(files) {
  var el = document.getElementById('sb-files');
  if (!files || files.length === 0) {
    el.innerHTML =
      '<div style="color:var(--green);padding:.4rem 0">' +
      '✓ No local brick data found for split-brain files.<br>' +
      '<span style="color:var(--muted)">Use an automatic policy above, or check the other node\'s dashboard.</span></div>';
    return;
  }
  var html = '';
  files.forEach(function(f) {
    html += '<div class="file-card"><div class="fc-path">' + esc(f.path) + '</div>';
    if (f.bricks && f.bricks.length) {
      f.bricks.forEach(function(b) {
        html += '<div class="brick-row"><span class="brick-id" title="' + esc(b.brick) + '">'
          + esc(b.brick) + '</span>';
        if (b.local && b.exists) {
          html += '<span class="brick-meta">' + esc(b.size_fmt) + ' &nbsp;·&nbsp; ' + esc(b.mtime) + '</span>'
            + '<button class="btn btn-green btn-sm" '
            + 'onclick="resolveBrick(' + JSON.stringify(b.brick) + ',' + JSON.stringify(f.path) + ')">'
            + 'Use this</button>';
        } else if (b.local && b.exists === false) {
          html += '<span class="brick-meta" style="color:var(--red)">file missing on brick</span>'
            + '<span></span>';
        } else {
          html += '<span class="brick-meta" style="color:var(--muted)">remote node</span>'
            + '<span></span>';
        }
        html += '</div>';
      });
    }
    html += '</div>';
  });
  el.innerHTML = html;
}

function resolvePolicy(policy) {
  closeSbModal();
  var msg = 'Resolve ALL split-brain files using the "' + policy + '" policy?\n\n'
    + '  cluster.favorite-child-policy = ' + policy + '\n'
    + '  gluster volume heal ' + VOL + '\n\n'
    + 'The losing copy on each brick will be overwritten.';
  doAction('heal_splitbrain_policy', { policy: policy }, msg, null);
}

function resolveBrick(brick, file) {
  closeSbModal();
  var msg = 'Use brick\n  ' + brick + '\nas the authoritative source for\n  ' + file
    + '\n\nThe copy on the other brick will be overwritten. This cannot be undone.';
  doAction('heal_splitbrain_brick', { brick: brick, file: file }, msg, null);
}

// ── helpers ───────────────────────────────────────────────────────────────
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── system metrics bar chart ──────────────────────────────────────────────
// server-baked initial data — chart renders before any fetch completes
var initSys = {{ data.system|tojson }};
var netPeak = 1024 * 1024; // auto-scale floor: 1 MB/s

function pctColor(v) {
  return v >= 90 ? '#f85149' : v >= 70 ? '#d29922' : '#56d364';
}

function renderSysChart(sys) {
  if (!sys || !sys.available) {
    document.getElementById('sys-chart').innerHTML =
      '<div style="color:var(--muted);font-size:.82rem;padding:.4rem 0">' +
      'psutil not installed — run: pip install psutil</div>';
    return;
  }

  // auto-scale network
  netPeak = Math.max(netPeak, sys.rx_bps, sys.tx_bps);

  var bars = [
    { label: 'CPU',    sub: sys.cpu_pct + '%',  pct: sys.cpu_pct,  color: pctColor(sys.cpu_pct) },
    { label: 'RAM',    sub: sys.mem_pct + '%',  pct: sys.mem_pct,  color: pctColor(sys.mem_pct) },
    { label: 'Disk',   sub: sys.disk_pct + '%', pct: sys.disk_pct, color: pctColor(sys.disk_pct) },
    { label: '↓ Net',  sub: sys.rx_fmt,          pct: Math.min(100, sys.rx_bps / netPeak * 100), color: '#388bfd' },
    { label: '↑ Net',  sub: sys.tx_fmt,          pct: Math.min(100, sys.tx_bps / netPeak * 100), color: '#388bfd' },
  ];

  // SVG layout
  var BW  = 64;   // bar width
  var GAP = 22;   // gap between bars
  var CH  = 110;  // chart height (bar area)
  var PT  = 22;   // pad top (value label)
  var PB  = 38;   // pad bottom (name + sub-label)
  var PL  = 16;   // pad left
  var n   = bars.length;
  var svgW = PL * 2 + n * BW + (n - 1) * GAP;
  var svgH = PT + CH + PB;

  // horizontal grid lines at 25 / 50 / 75 %
  var gridLines = '';
  [25, 50, 75].forEach(function(g) {
    var y = PT + CH - (g / 100 * CH);
    gridLines +=
      '<line x1="' + PL + '" y1="' + y + '" x2="' + (svgW - PL) + '" y2="' + y + '" ' +
      'stroke="rgba(255,255,255,0.06)" stroke-width="1"/>' +
      '<text x="' + (PL - 3) + '" y="' + (y + 4) + '" text-anchor="end" ' +
      'font-size="8" fill="rgba(255,255,255,0.25)">' + g + '</text>';
  });

  var rects = '';
  bars.forEach(function(b, i) {
    var x  = PL + i * (BW + GAP);
    var bh = Math.max(2, b.pct / 100 * CH);
    var by = PT + CH - bh;
    var cx = x + BW / 2;

    // background track
    rects +=
      '<rect x="' + x + '" y="' + PT + '" width="' + BW + '" height="' + CH + '" ' +
      'rx="5" fill="rgba(255,255,255,0.04)"/>';
    // bar (rounded top only)
    rects +=
      '<rect x="' + x + '" y="' + by + '" width="' + BW + '" height="' + bh + '" ' +
      'rx="5" fill="' + b.color + '" opacity="0.88"/>';
    // percentage label above bar
    rects +=
      '<text x="' + cx + '" y="' + (by - 5) + '" text-anchor="middle" ' +
      'font-size="10" font-weight="600" fill="' + b.color + '">' + esc(b.sub) + '</text>';
    // name label below chart
    rects +=
      '<text x="' + cx + '" y="' + (PT + CH + 16) + '" text-anchor="middle" ' +
      'font-size="11" fill="#c9d1d9">' + esc(b.label) + '</text>';
    // secondary info below name
    if (b.label === 'RAM') {
      rects +=
        '<text x="' + cx + '" y="' + (PT + CH + 29) + '" text-anchor="middle" ' +
        'font-size="9" fill="#8b949e">' + esc(sys.mem_used + ' / ' + sys.mem_total) + '</text>';
    } else if (b.label === '↓ Net' || b.label === '↑ Net') {
      rects +=
        '<text x="' + cx + '" y="' + (PT + CH + 29) + '" text-anchor="middle" ' +
        'font-size="9" fill="#8b949e">peak ' + esc(_fmt(netPeak) + '/s') + '</text>';
    }
  });

  document.getElementById('sys-chart').innerHTML =
    '<svg viewBox="0 0 ' + svgW + ' ' + svgH + '" xmlns="http://www.w3.org/2000/svg">' +
    gridLines + rects + '</svg>';

  document.getElementById('sys-ts').textContent =
    'updated ' + new Date().toLocaleTimeString();
}

function _fmt(b) {
  var u = ['B','KB','MB','GB'];
  for (var i = 0; i < u.length; i++) {
    if (Math.abs(b) < 1024) return b.toFixed(0) + ' ' + u[i];
    b /= 1024;
  }
  return b.toFixed(1) + ' TB';
}

function updateSysMetrics() {
  fetch('/api/status')
    .then(function(r) { return r.json(); })
    .then(function(d) { renderSysChart(d.system); })
    .catch(function() {
      document.getElementById('sys-ts').textContent = 'fetch failed';
    });
}

// render immediately with server-baked data, then poll every 5 s for live updates
renderSysChart(initSys);
updateSysMetrics();
setInterval(updateSysMetrics, 5000);
</script>
</body></html>"""

# ── Flask app ─────────────────────────────────────────────────────────────────
if FLASK:
    app = Flask(__name__)
    app.jinja_env.globals.update(
        VOL=VOL, CLUSTER_MOUNT=CLUSTER_MOUNT, SHADOW_MOUNT=SHADOW_MOUNT
    )

    @app.route("/")
    def index():
        data = get_all_status()
        return render_template_string(
            HTML,
            data=type("D", (), data)(),
            VOL=VOL,
            CLUSTER_MOUNT=CLUSTER_MOUNT,
            SHADOW_MOUNT=SHADOW_MOUNT,
        )

    @app.route("/api/status")
    def api_status():
        return jsonify(get_all_status())

    @app.route("/api/splitbrain_detail")
    def api_splitbrain_detail():
        return jsonify(collect_splitbrain_detail())

    @app.route("/api/action", methods=["POST"])
    def api_action():
        data   = flask_request.get_json(force=True, silent=True) or {}
        action = data.get("action", "")

        def run(cmd, t=30):
            return sh(cmd, timeout=t).strip()

        # ── trigger full self-heal ─────────────────────────────────────────
        if action == "heal":
            out = run(f"gluster volume heal {VOL}")
            return jsonify({"ok": True, "output": out or "Heal cycle triggered"})

        # ── resolve split-brain by policy ──────────────────────────────────
        elif action == "heal_splitbrain_policy":
            policy = data.get("policy", "")
            if policy not in ("mtime", "ctime", "size", "majority"):
                return jsonify({"ok": False, "output": "Invalid policy — use mtime/ctime/size/majority"})
            out1 = run(f"gluster volume set {VOL} cluster.favorite-child-policy {policy}")
            out2 = run(f"gluster volume heal {VOL}")
            return jsonify({
                "ok":     True,
                "output": f"Policy set to '{policy}'. {out2 or 'Heal triggered.'}",
            })

        # ── resolve single file from a chosen brick ────────────────────────
        elif action == "heal_splitbrain_brick":
            brick = data.get("brick", "").strip()
            file  = data.get("file",  "").strip()
            if (not brick or ":" not in brick
                    or not file or not file.startswith("/") or ".." in file):
                return jsonify({"ok": False, "output": "Invalid brick or file parameter"})
            out = run(
                f"gluster volume heal {VOL} split-brain source-brick {brick} {file}"
            )
            ok = ("successfully" in out.lower()
                  or "heal" in out.lower()
                  or out == "")
            return jsonify({"ok": ok, "output": out or f"Resolution triggered for {file}"})

        # ── restart an allowlisted service ─────────────────────────────────
        elif action == "restart_service":
            svc = data.get("service", "").strip()
            if svc not in RESTARTABLE_SVCS:
                return jsonify({"ok": False, "output": f"Service not in allowlist: {svc}"})
            run(f"systemctl restart {svc}", t=25)
            state = run(f"systemctl is-active {svc}")
            return jsonify({
                "ok":     state == "active",
                "output": f"{svc} is now {state}",
            })

        # ── remount a filesystem ───────────────────────────────────────────
        elif action == "remount":
            target = data.get("target", "")
            if target == "cluster":
                path = CLUSTER_MOUNT
            elif target == "shadow":
                path = SHADOW_MOUNT
            else:
                return jsonify({"ok": False, "output": "Unknown target"})
            run(f"umount -l {path} 2>/dev/null || true")
            out = run(f"mount {path}")
            ok  = mountpoint(path)
            return jsonify({
                "ok":     ok,
                "output": (f"{path} mounted successfully"
                           if ok else f"Mount failed: {out}"),
            })

        else:
            return jsonify({"ok": False, "output": f"Unknown action: {action}"})

    def run():
        print(f"\n  gluster-shadow-ha dashboard")
        print(f"  Open: http://{socket.gethostbyname(HOSTNAME)}:{PORT}\n")
        app.run(host="0.0.0.0", port=PORT, debug=False)

# ── stdlib fallback (no Flask) ────────────────────────────────────────────────
else:
    def render_simple(data):
        g = data["gluster"]; c = data["ctdb"]; m = data["mounts"]
        w = data["watchdog"]; s = data["services"]
        def dot(ok): return "🟢" if ok else "🔴"
        lines = [
            "<pre style='font-family:monospace;color:#c9d1d9;background:#0d1117;"
            "padding:1rem;max-width:700px;margin:1rem auto;border-radius:8px;font-size:.85rem'>",
            f"gluster-shadow-ha  {data['hostname']}  {data['timestamp']}",
            "",
            "GlusterFS",
            f"  {dot(g['peers_connected']>0)} Peers connected : {g['peers_connected']}/{g['peers_total']}",
            f"  {dot(g['vol_started'])} Volume {VOL}     : {'Started' if g['vol_started'] else 'Stopped'}",
            f"  {dot(g['heal_pending']==0)} Self-heal       : {'In sync' if g['heal_pending']==0 else str(g['heal_pending'])+' entries pending'}",
            "",
            "CTDB / VIP",
            f"  {dot(c['ctdb_active'])} CTDB daemon     : {'Active' if c['ctdb_active'] else 'Down'}",
            f"  {dot(c['holds_vip'])} VIP ownership   : {'This node' if c['holds_vip'] else 'Other node / unassigned'}",
            "",
            "Mounts",
            f"  {dot(m['cluster_mounted'])} {CLUSTER_MOUNT}: {'Mounted' if m['cluster_mounted'] else 'NOT MOUNTED'}",
        ]
        if m["is_node0"]:
            lines.append(
                f"  {dot(m['shadow_mounted'])} {SHADOW_MOUNT}: "
                f"{'Mounted' if m['shadow_mounted'] else 'not mounted'}"
            )
        lines += ["", "Services"]
        for svc, ok in s.items():
            lines.append(f"  {dot(ok)} {svc}")
        lines += ["", "Watchdog log (last 5 lines)"]
        for ln in (w["lines"] or ["(no entries yet)"])[-5:]:
            lines.append(f"  {ln}")
        lines.append("</pre>")
        return "\n".join(lines)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data = get_all_status()
            if self.path == "/api/status":
                body = json.dumps(data, indent=2).encode()
                ct   = "application/json"
            else:
                body = render_simple(data).encode()
                ct   = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    def run():
        print(f"\n  gluster-shadow-ha dashboard (stdlib mode — install flask for full UI)")
        print(f"  pip3 install flask")
        print(f"  Open: http://{socket.gethostbyname(HOSTNAME)}:{PORT}\n")
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

if __name__ == "__main__":
    run()
