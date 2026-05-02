#!/usr/bin/env python3
"""
dashboard.py — gluster-shadow-ha live status dashboard

Serves a self-refreshing web UI on http://<node-ip>:9000
Shows GlusterFS heal progress, CTDB health, mount status, watchdog log.

Usage:
    # Install dependency (one-time):
    pip3 install flask

    # Run (as root for gluster/ctdb commands):
    sudo python3 scripts/dashboard.py

    # Or as a systemd service:
    sudo cp scripts/dashboard.py /usr/local/sbin/
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
    from flask import Flask, jsonify, render_template_string
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
    raw_peer   = sh(f"gluster peer status")
    raw_vol    = sh(f"gluster volume status {VOL} 2>/dev/null || gluster volume info {VOL} 2>/dev/null")
    raw_heal   = sh(f"gluster volume heal {VOL} info 2>/dev/null")
    raw_split  = sh(f"gluster volume heal {VOL} info split-brain 2>/dev/null")
    raw_info   = sh(f"gluster volume info {VOL}")

    peers_connected = len(re.findall(r"State:.*Connected", raw_peer))
    peers_total     = len(re.findall(r"^Hostname:", raw_peer, re.M))
    vol_started     = "Started" in raw_info
    # GlusterFS 11+ uses columnar output: "Brick host:path  PORT  RDMA  Y  PID"
    # Older versions use key-value:       "Brick Path : ...\nOnline     : Y"
    bricks_total  = len(re.findall(r"^Brick\s+\S+", raw_vol, re.M))
    bricks_online = len(re.findall(r"^Brick\s+\S+.*\s+Y\s+\d+", raw_vol, re.M))
    if bricks_total == 0:  # fall back to old format
        bricks_online = len(re.findall(r"Online\s*:\s*Y", raw_vol, re.I))
        bricks_total  = len(re.findall(r"Brick Path\s*:", raw_vol, re.I))

    # Healing: sum "Number of entries: N" across all bricks
    heal_counts = [int(x) for x in re.findall(r"Number of entries:\s*(\d+)", raw_heal)]
    heal_pending = sum(heal_counts)

    # Split-brain
    split_files = re.findall(r"^\s*/[^\n]+", raw_split, re.M)
    split_files = [f.strip() for f in split_files if "/gluster" not in f and len(f.strip()) > 1]

    return {
        "peers_connected": peers_connected,
        "peers_total": peers_total,
        "vol_started": vol_started,
        "bricks_online": bricks_online,
        "bricks_total": bricks_total,
        "heal_pending": heal_pending,
        "split_brain_files": split_files[:10],
        "raw_vol_status": raw_vol[:2000],
    }

def collect_ctdb():
    raw = sh("ctdb status 2>/dev/null")
    has_vip = bool(sh(f"ip addr show | grep -w '{sh('cat /etc/ctdb/public_addresses 2>/dev/null').split()[0].split('/')[0] if sh('cat /etc/ctdb/public_addresses 2>/dev/null') else ''}' 2>/dev/null").strip())
    nodes_ok    = len(re.findall(r"\bOK\b", raw))
    nodes_total = len(re.findall(r"^pnn:", raw, re.M))
    ctdb_up = nodes_ok > 0 or "OK" in sh("systemctl is-active ctdb")

    # Get VIP from public_addresses
    vip_raw = sh("cat /etc/ctdb/public_addresses 2>/dev/null").strip().split()
    vip_addr = vip_raw[0].split("/")[0] if vip_raw else ""
    has_vip = vip_addr and bool(sh(f"ip addr show | grep -qw {vip_addr} && echo yes").strip())

    return {
        "ctdb_active": ctdb_up,
        "nodes_ok": nodes_ok,
        "nodes_total": nodes_total,
        "holds_vip": has_vip,
        "vip_addr": vip_addr,
    }

def collect_mounts():
    cluster_up = mountpoint(CLUSTER_MOUNT)
    shadow_up  = mountpoint(SHADOW_MOUNT)

    # Detect role
    node0_ip = sh("awk 'NR==1' /etc/ctdb/nodes 2>/dev/null").strip()
    this_ip  = sh("hostname -I | awk '{print $1}'").strip()
    is_node0 = (this_ip == node0_ip)

    return {
        "cluster_mounted": cluster_up,
        "shadow_mounted": shadow_up,
        "is_node0": is_node0,
        "cluster_disk": disk_free(CLUSTER_MOUNT) if cluster_up else None,
        "shadow_disk":  disk_free(SHADOW_MOUNT)  if shadow_up  else None,
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
        if "OK —" in last:         status = "ok"
        elif "recovery complete" in last: status = "recovered"
        elif "FAILED" in last:     status = "failed"
        elif "NOT mounted" in last: status = "recovering"
    return {"lines": [l.rstrip() for l in lines], "status": status}

def collect_services():
    svcs = ["glusterd", "smbd", "nmbd", "ctdb",
            "shadow-layer-setup", "cluster-shared-watchdog.timer"]
    result = {}
    for s in svcs:
        active = sh(f"systemctl is-active {s} 2>/dev/null").strip()
        result[s] = active == "active"
    return result

def get_all_status():
    return {
        "hostname":  HOSTNAME,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        "gluster":   collect_gluster(),
        "ctdb":      collect_ctdb(),
        "mounts":    collect_mounts(),
        "watchdog":  collect_watchdog(),
        "services":  collect_services(),
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
    border:1px solid var(--border);border-radius:2rem;padding:.2rem .6rem}
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
  .dot.green{background:var(--green)}
  .dot.red{background:var(--red)}
  .dot.yellow{background:var(--yellow)}
  .dot.muted{background:var(--muted)}
  .pill{font-size:.75rem;padding:.15rem .5rem;border-radius:2rem;font-weight:600}
  .pill.green{background:rgba(86,211,100,.15);color:var(--green)}
  .pill.red{background:rgba(248,81,73,.15);color:var(--red)}
  .pill.yellow{background:rgba(210,153,34,.15);color:var(--yellow)}
  .pill.blue{background:rgba(56,139,253,.15);color:var(--blue)}
  .bar-wrap{margin:.75rem 0 .25rem;background:var(--bg);border-radius:4px;height:8px;overflow:hidden}
  .bar{height:8px;border-radius:4px;transition:width .5s ease;background:var(--green)}
  .bar.warn{background:var(--yellow)}
  .bar.full{background:var(--red)}
  .log{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);
    padding:.75rem;font-family:monospace;font-size:.75rem;max-height:200px;overflow-y:auto;margin-top:.5rem}
  .log-ok{color:var(--green)}
  .log-warn{color:var(--yellow)}
  .log-err{color:var(--red)}
  .log-info{color:var(--muted)}
  .split-cmd{background:var(--bg);border:1px solid var(--red);border-radius:var(--radius);
    padding:.75rem;font-family:monospace;font-size:.8rem;color:var(--red);margin-top:.75rem}
  .vip-badge{background:rgba(56,139,253,.15);color:var(--blue);border:1px solid var(--blue);
    border-radius:2rem;padding:.15rem .6rem;font-size:.75rem;font-weight:600}
  .heal-label{font-size:.8rem;color:var(--muted);margin-top:.3rem}
  #countdown{display:inline-block;min-width:1.5em;text-align:center}
</style>
</head>
<body>
<div class="container">
<header>
  <div>
    <h1><span>gluster</span>-shadow-ha &nbsp;·&nbsp; {{ data.hostname }}</h1>
    <div class="meta">{{ data.timestamp }}
      {% if data.ctdb.holds_vip %}&nbsp;&nbsp;<span class="vip-badge">⚡ holds VIP {{ data.ctdb.vip_addr }}</span>{% endif %}
    </div>
  </div>
  <span class="refresh-badge">auto-refresh in <span id="countdown">10</span>s</span>
</header>

<!-- Overall health bar -->
{% set total_checks = 7 %}
{% set passing = (data.services['glusterd']|int) + (data.gluster.vol_started|int) +
    (data.mounts.cluster_mounted|int) + (data.services['smbd']|int) +
    (data.services['ctdb']|int) + (data.ctdb.ctdb_active|int) +
    (data.watchdog.status == 'ok')|int %}
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
<!-- GlusterFS card -->
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
    <span><span class="dot {% if data.gluster.bricks_online == data.gluster.bricks_total and data.gluster.bricks_total > 0 %}green{% elif data.gluster.bricks_online > 0 %}yellow{% else %}red{% endif %}"></span>Bricks online</span>
    <span class="pill {% if data.gluster.bricks_online == data.gluster.bricks_total and data.gluster.bricks_total > 0 %}green{% elif data.gluster.bricks_online > 0 %}yellow{% else %}red{% endif %}">
      {{ data.gluster.bricks_online }} / {{ data.gluster.bricks_total }}
    </span>
  </div>

  <!-- Self-heal progress -->
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

  {% if data.gluster.split_brain_files %}
  <div class="split-cmd">
    ⚠ Split-brain detected on {{ data.gluster.split_brain_files|length }} file(s).<br>
    To resolve:<br>
    gluster volume heal {{ VOL }} split-brain latest-mtime<br>
    gluster volume heal {{ VOL }} full
  </div>
  {% endif %}
</div>

<!-- CTDB + VIP card -->
<div class="card {% if not data.ctdb.ctdb_active %}alert{% endif %}">
  <h2>CTDB / Floating VIP</h2>
  <div class="row">
    <span><span class="dot {% if data.ctdb.ctdb_active %}green{% else %}red{% endif %}"></span>CTDB daemon</span>
    <span class="pill {% if data.ctdb.ctdb_active %}green{% else %}red{% endif %}">
      {% if data.ctdb.ctdb_active %}Active{% else %}Down{% endif %}
    </span>
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

<!-- Mounts card -->
<div class="card {% if not data.mounts.cluster_mounted %}alert{% endif %}">
  <h2>Storage mounts</h2>
  <div class="row">
    <span><span class="dot {% if data.mounts.cluster_mounted %}green{% else %}red{% endif %}"></span>{{ CLUSTER_MOUNT }}</span>
    <span class="pill {% if data.mounts.cluster_mounted %}green{% else %}red{% endif %}">
      {% if data.mounts.cluster_mounted %}Mounted{% else %}Not mounted{% endif %}
    </span>
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
    <span class="pill {% if data.mounts.shadow_mounted %}green{% else %}yellow{% endif %}">
      {% if data.mounts.shadow_mounted %}Mounted{% else %}Not mounted{% endif %}
    </span>
  </div>
  {% endif %}
</div>

<!-- Services card -->
<div class="card">
  <h2>Services</h2>
  {% for svc, active in data.services.items() %}
  <div class="row">
    <span><span class="dot {% if active %}green{% else %}red{% endif %}"></span>{{ svc }}</span>
    <span class="pill {% if active %}green{% else %}red{% endif %}">
      {% if active %}active{% else %}inactive{% endif %}
    </span>
  </div>
  {% endfor %}
</div>
</div>

<!-- Watchdog log card -->
<div class="card">
  <h2>Watchdog log
    <span class="pill {% if data.watchdog.status == 'ok' %}green
      {% elif data.watchdog.status == 'recovered' %}blue
      {% elif data.watchdog.status == 'failed' %}red
      {% else %}yellow{% endif %}" style="margin-left:.5rem">
      {{ data.watchdog.status }}
    </span>
  </h2>
  <div class="log" id="wdlog">
    {% for line in data.watchdog.lines %}
    <div class="{% if 'OK —' in line %}log-ok{% elif 'FAILED' in line or 'failed' in line %}log-err{% elif 'recovery' in line or 'NOT mounted' in line %}log-warn{% else %}log-info{% endif %}">
      {{ line }}
    </div>
    {% endfor %}
    {% if not data.watchdog.lines %}
    <div class="log-info">(no entries yet — timer fires 2 min after boot)</div>
    {% endif %}
  </div>
</div>

</div><!-- /container -->
<script>
  // Auto-refresh countdown
  var t = 10;
  var el = document.getElementById('countdown');
  setInterval(function(){
    t--;
    el.textContent = t;
    if(t <= 0){ location.reload(); }
  }, 1000);
  // Scroll watchdog log to bottom
  var log = document.getElementById('wdlog');
  if(log) log.scrollTop = log.scrollHeight;
</script>
<style>
@keyframes pulse{from{opacity:.6}to{opacity:1}}
</style>
</body></html>"""

# ── Flask app ─────────────────────────────────────────────────────────────────
if FLASK:
    app = Flask(__name__)
    app.jinja_env.globals.update(VOL=VOL, CLUSTER_MOUNT=CLUSTER_MOUNT)

    @app.route("/")
    def index():
        data = get_all_status()
        return render_template_string(HTML, data=type('D', (), data)(), VOL=VOL,
                                      CLUSTER_MOUNT=CLUSTER_MOUNT)

    @app.route("/api/status")
    def api_status():
        return jsonify(get_all_status())

    def run():
        print(f"\n  gluster-shadow-ha dashboard")
        print(f"  Open: http://{socket.gethostbyname(HOSTNAME)}:{PORT}\n")
        app.run(host="0.0.0.0", port=PORT, debug=False)

# ── stdlib fallback (no Flask) ────────────────────────────────────────────────
else:
    # Simple Jinja2-free rendering for the stdlib path
    def render_simple(data):
        g = data["gluster"]; c = data["ctdb"]; m = data["mounts"]
        w = data["watchdog"]; s = data["services"]
        def dot(ok): return "🟢" if ok else "🔴"
        lines = [
            f"<pre style='font-family:monospace;color:#c9d1d9;background:#0d1117;"
            f"padding:1rem;max-width:700px;margin:1rem auto;border-radius:8px;font-size:.85rem'>",
            f"gluster-shadow-ha  {data['hostname']}  {data['timestamp']}",
            f"",
            f"GlusterFS",
            f"  {dot(g['peers_connected']>0)} Peers connected : {g['peers_connected']}/{g['peers_total']}",
            f"  {dot(g['vol_started'])} Volume {VOL}     : {'Started' if g['vol_started'] else 'Stopped'}",
            f"  {dot(g['heal_pending']==0)} Self-heal       : {'In sync' if g['heal_pending']==0 else str(g['heal_pending'])+' entries pending'}",
            f"",
            f"CTDB / VIP",
            f"  {dot(c['ctdb_active'])} CTDB daemon     : {'Active' if c['ctdb_active'] else 'Down'}",
            f"  {dot(c['holds_vip'])} VIP ownership   : {'This node' if c['holds_vip'] else 'Other node / unassigned'}",
            f"",
            f"Mounts",
            f"  {dot(m['cluster_mounted'])} {CLUSTER_MOUNT}: {'Mounted' if m['cluster_mounted'] else 'NOT MOUNTED'}",
        ]
        if m["is_node0"]:
            lines.append(f"  {dot(m['shadow_mounted'])} {SHADOW_MOUNT}: {'Mounted' if m['shadow_mounted'] else 'not mounted'}")
        lines += ["", "Services"]
        for svc, ok in s.items():
            lines.append(f"  {dot(ok)} {svc}")
        lines += ["", "Watchdog log (last 5 lines)"]
        for l in (w["lines"] or ["(no entries yet)"])[-5:]:
            lines.append(f"  {l}")
        lines.append("</pre>")
        return "\n".join(lines)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data = get_all_status()
            if self.path == "/api/status":
                body = json.dumps(data, indent=2).encode()
                ct = "application/json"
            else:
                body = render_simple(data).encode()
                ct = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_): pass

    def run():
        print(f"\n  gluster-shadow-ha dashboard (stdlib mode — install flask for full UI)")
        print(f"  pip3 install flask")
        print(f"  Open: http://{socket.gethostbyname(HOSTNAME)}:{PORT}\n")
        HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

if __name__ == "__main__":
    run()
