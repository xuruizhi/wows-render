"""
WoWs render web UI — local server.

Serves a single page where you pick a ship, choose resolution / views / light
preset, and hit "generate". Rendering runs in a background worker; the page
polls for progress and shows the finished images inline.

Run:
    python server.py [--port 8770]

Then open http://127.0.0.1:8770
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
WARSHIPS = os.path.dirname(ROOT)
TOOLS = os.path.join(WARSHIPS, "_tools")
BLENDER = os.path.join(TOOLS, "blender-4.5.10-windows-x64", "blender.exe")
EXPORTER = os.path.join(TOOLS, "model-exporter", "wows-gltf-exporter.exe")
LIST_SHIPS = os.path.join(TOOLS, "model-exporter", "wows-list-ships.exe")
RENDER_PY = os.path.join(ROOT, "render_ship.py")
SANITIZE_PY = os.path.join(ROOT, "sanitize_glb.py")
PYTHON = sys.executable

# Game install location. Override with the WOWS_GAME_DIR environment variable
# (or --game-dir on the command line); the default matches the author's setup.
DEFAULT_GAME_DIR = r"D:\SteamLibrary\steamapps\common\World of Warships"
GAME_DIR = os.environ.get("WOWS_GAME_DIR") or DEFAULT_GAME_DIR
GLB_DIR = os.path.join(ROOT, "glb")
OUT_ROOT = os.path.join(WARSHIPS, "WoWs_Renders")

os.makedirs(GLB_DIR, exist_ok=True)
os.makedirs(OUT_ROOT, exist_ok=True)

# ---------------------------------------------------------------- ship cache
_ship_lock = threading.Lock()
_ship_cache = {"data": None, "ts": 0}

NATION_MAP = {"United_Kingdom": "UK", "USA": "USA", "Russia": "USSR"}
TYPE_MAP = {
    "AirCarrier": "AircraftCarriers", "Battleship": "Battleships",
    "Cruiser": "Cruisers", "Destroyer": "Destroyers", "Submarine": "Submarines",
}
TIER_DIR = {
    "1": "Tier I", "2": "Tier II", "3": "Tier III", "4": "Tier IV", "5": "Tier V",
    "6": "Tier VI", "7": "Tier VII", "8": "Tier VIII", "9": "Tier IX",
    "10": "Tier X", "11": "Tier Star",
}


def load_ships():
    """Return the ship catalogue built by build_catalog.py (cached)."""
    with _ship_lock:
        if _ship_cache["data"] and (time.time() - _ship_cache["ts"]) < 3600:
            return _ship_cache["data"]

        path = os.path.join(ROOT, "ships.json")
        if not os.path.exists(path):
            raise RuntimeError("ships.json 不存在，请先运行 build_catalog.py")
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        _ship_cache["data"] = payload["ships"]
        _ship_cache["payload"] = payload
        _ship_cache["ts"] = time.time()
        return _ship_cache["data"]


def load_catalog():
    """Full catalogue payload: ships + UI label maps."""
    load_ships()
    return _ship_cache.get("payload")


def out_dir_for(ship):
    n = NATION_MAP.get(ship["nation"], ship["nation"])
    t = TYPE_MAP.get(ship["type"], ship["type"])
    tier = TIER_DIR.get(ship["tier"], "Tier " + ship["tier"])
    return os.path.join(OUT_ROOT, n, t, tier, ship["key"])


# ---------------------------------------------------------------- job manager
_jobs = {}
_jobs_lock = threading.Lock()


class Job:
    def __init__(self, jid, ship, params):
        self.id = jid
        self.ship = ship
        self.params = params
        self.status = "queued"     # queued|exporting|sanitizing|rendering|done|error
        self.log = []
        self.error = None
        self.images = []
        self.started = time.time()
        self.finished = None

    def to_dict(self):
        return {
            "id": self.id,
            "ship": self.ship,
            "status": self.status,
            "log": self.log[-40:],
            "error": self.error,
            "images": self.images,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "params": self.params,
        }

    def say(self, msg):
        stamp = time.strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {msg}")


def run_job(job):
    ship = job.ship
    p = job.params
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in ship["key"])
    raw_glb = os.path.join(GLB_DIR, safe + ".raw.glb")
    clean_glb = os.path.join(GLB_DIR, safe + ".glb")
    outdir = out_dir_for(ship)
    os.makedirs(outdir, exist_ok=True)
    views = p.get("views", ["hero"])
    hero = os.path.join(outdir, views[0] + ".png")

    try:
        # 1 ---- export
        job.status = "exporting"
        job.say(f"导出模型 {ship['key']} (贴图上限 {p.get('tex', 4096)}px)…")
        exp = subprocess.run(
            [EXPORTER, "-W", GAME_DIR, "-s", ship["key"], "-o", raw_glb, "-Z", str(p.get("tex", 4096))],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if not os.path.exists(raw_glb):
            tail = "\n".join((exp.stdout or "").splitlines()[-5:])
            raise RuntimeError(f"模型导出失败: {tail or exp.stderr}")

        # 2 ---- sanitize
        job.status = "sanitizing"
        job.say("清理空 mesh…")
        san = subprocess.run(
            [PYTHON, SANITIZE_PY, raw_glb, clean_glb],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if san.returncode != 0 or not os.path.exists(clean_glb):
            raise RuntimeError(f"清理失败: {san.stderr or san.stdout}")

        # 3 ---- render
        job.status = "rendering"
        job.say(f"渲染 {p.get('res')} × {len(views)} 视角 (samples={p.get('samples')})…")
        cmd = [
            BLENDER, "-b", "--factory-startup", "-P", RENDER_PY, "--",
            "--glb", clean_glb,
            "--out", hero,
            "--res", p["res"],
            "--samples", str(p["samples"]),
            "--views", ",".join(views),
            "--preset", p.get("preset", "day"),
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        okflag = False
        for line in proc.stdout:
            line = line.strip()
            if "RENDER_INFO" in line:
                job.say(line.split("RENDER_INFO:")[-1].strip())
            if "RENDER_OK" in line:
                okflag = True
        proc.wait()
        if not okflag:
            raise RuntimeError("Blender 渲染失败（未返回 RENDER_OK）")

        # 4 ---- collect
        rel_base = "/renders/" + "/".join(
            os.path.relpath(outdir, OUT_ROOT).split(os.sep)
        )
        made = []
        for v in views:
            fp = os.path.join(outdir, v + ".png")
            if os.path.exists(fp):
                made.append({
                    "view": v,
                    "url": rel_base + "/" + v + ".png",
                    "bytes": os.path.getsize(fp),
                })
        job.images = made
        job.say(f"完成，共 {len(made)} 张")
        job.status = "done"
        job.finished = time.time()

    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.say("错误: " + str(e))
        job.finished = time.time()
    finally:
        for f in (raw_glb, clean_glb):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass


# ---------------------------------------------------------------- HTTP
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>战舰世界 · 高清渲染</title>
<style>
:root{--bg:#0f1419;--panel:#1a2029;--line:#2b3644;--fg:#e6edf3;--mute:#8b98a5;
      --accent:#4a9eff;--ok:#3fb950;--err:#f85149;--warn:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.55 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:22px 20px 60px}
h1{font-size:19px;font-weight:600;margin:0 0 3px}
.sub{color:var(--mute);font-size:12.5px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:400px 1fr;gap:18px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:15px}
label{display:block;font-size:12px;color:var(--mute);margin:13px 0 5px;font-weight:500}
label:first-child{margin-top:0}
select,input{width:100%;background:#11161d;color:var(--fg);border:1px solid var(--line);
     border-radius:7px;padding:8px 9px;font-size:13px;font-family:inherit}
select:focus,input:focus{outline:none;border-color:var(--accent)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:5px}
.chip{padding:5px 11px;border:1px solid var(--line);border-radius:999px;cursor:pointer;
      font-size:12.5px;user-select:none;color:var(--mute);transition:.13s}
.chip:hover{border-color:#3d4a5c;color:var(--fg)}
.chip.on{background:rgba(74,158,255,.16);border-color:var(--accent);color:#9fcaff}
.chip.alt{border-style:dashed}
.btn{width:100%;margin-top:17px;padding:11px;border:none;border-radius:8px;
     background:var(--accent);color:#04101f;font-size:14px;font-weight:650;cursor:pointer;
     transition:.15s}
.btn:hover{filter:brightness(1.1)}
.btn:disabled{background:#2b3644;color:#5b6673;cursor:not-allowed}
.meta{font-size:12px;color:var(--mute);margin-top:10px;line-height:1.7}
.status{display:flex;align-items:center;gap:9px;font-size:13px;margin-bottom:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mute);flex:none}
.dot.busy{background:var(--warn);animation:pulse 1.1s infinite}
.dot.done{background:var(--ok)}.dot.err{background:var(--err)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
pre.log{background:#0b0f14;border:1px solid var(--line);border-radius:8px;
        padding:11px;font-size:11.5px;line-height:1.6;max-height:190px;overflow:auto;
        color:#93a1b0;margin:0;white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace}
.shots{display:grid;gap:11px;margin-top:14px}
.shot{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#0b0f14}
.shot img{display:block;width:100%;height:auto}
.shot .cap{display:flex;justify-content:space-between;padding:7px 10px;
           font-size:11.5px;color:var(--mute);border-top:1px solid var(--line)}
.shot a{color:var(--accent);text-decoration:none}
.shot a:hover{text-decoration:underline}
.hint{font-size:11.5px;color:var(--mute);margin-top:7px}
</style>
</head>
<body>
<div class="wrap">
  <h1>战舰世界 · 高清渲染</h1>
  <div class="sub">从游戏安装目录导出 3D 模型 · Blender Cycles OptiX 渲染</div>

  <div class="grid">
    <div class="card">
      <label>国家</label>
      <select id="nation"><option value="">全部国家</option></select>

      <label>舰种</label>
      <select id="type"><option value="">全部舰种</option></select>

      <label>级别</label>
      <select id="tier"><option value="">全部级别</option></select>

      <label>战舰 <span id="count" class="meta"></span></label>
      <select id="ship" size="9" style="height:auto"></select>

      <label>分辨率</label>
      <select id="res"></select>

      <label>视角 <span id="viewhint" class="meta"></span></label>
      <div class="chips" id="views"></div>

      <div class="row">
        <div>
          <label>采样</label>
          <select id="samples">
            <option value="32">32（快）</option>
            <option value="64">64</option>
            <option value="128" selected>128（推荐）</option>
            <option value="256">256（慢）</option>
          </select>
        </div>
        <div>
          <label>光照</label>
          <select id="preset"></select>
        </div>
      </div>

      <button class="btn" id="go">开始生成</button>
      <div class="meta" id="est"></div>
    </div>

    <div class="card">
      <div class="status">
        <span class="dot" id="dot"></span>
        <span id="stat">就绪 — 选择战舰后点击「开始生成」</span>
      </div>
      <pre class="log" id="log">等待任务…</pre>
      <div class="shots" id="shots"></div>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
let CAT = null, SHIPS = [], CUR = null, POLL = null, VIEWS = [];

async function init(){
  CAT = await (await fetch('/api/catalog')).json();
  SHIPS = CAT.ships;
  VIEWS = CAT.views;

  fillSel('#nation', CAT.nations.map(n => {
    const f = SHIPS.find(s => s.nation === n);
    return [n, f ? `${f.nationZh} ${n}` : n];
  }));
  fillSel('#type', CAT.types.map(t => {
    const f = SHIPS.find(s => s.type === t);
    return [t, f ? `${f.typeZh} ${t}` : t];
  }));
  fillSel('#tier', CAT.tiers.map(t => {
    const f = SHIPS.find(s => s.tier === t);
    return [t, f ? `${f.tierZh} (Tier ${t})` : t];
  }));
  fillSel('#res', CAT.resolutions.map(r => [r.value, r.zh]), '3840x2160');
  fillSel('#preset', CAT.presets.map(p => [p.value, p.zh]), 'day');

  buildViewChips();
  refresh();
}

function fillSel(sel, pairs, keep){
  const el = $(sel);
  // keep any pre-existing placeholder option (e.g. "全部国家")
  const placeholder = el.querySelector('option[value=""]');
  el.innerHTML = '';
  if(placeholder) el.appendChild(placeholder);
  for(const [v, label] of pairs){
    const o = document.createElement('option');
    o.value = v; o.textContent = label; el.appendChild(o);
  }
  el.value = keep !== undefined ? keep : '';
}

function buildViewChips(){
  const box = $('#views');
  box.innerHTML = '';
  const all = document.createElement('div');
  all.className = 'chip alt on'; all.dataset.all = '1';
  all.textContent = '全部视角';
  box.appendChild(all);
  VIEWS.forEach((v, i) => {
    const c = document.createElement('div');
    c.className = 'chip on'; c.dataset.v = v.key;
    c.textContent = `${v.zh}（${v.key}）`;
    c.title = v.desc;
    box.appendChild(c);
  });
  box.querySelectorAll('.chip').forEach(c => c.onclick = () => {
    if(c.dataset.all){
      const on = !c.classList.contains('on');
      c.classList.toggle('on', on);
      box.querySelectorAll('.chip:not(.alt)').forEach(x => x.classList.toggle('on', on));
    } else {
      c.classList.toggle('on');
      syncAll();
    }
    estimate();
  });
}

function syncAll(){
  const sub = [...document.querySelectorAll('.chip:not(.alt)')];
  const allOn = sub.every(c => c.classList.contains('on'));
  document.querySelector('.chip.alt').classList.toggle('on', allOn);
}

function filtered(){
  return SHIPS.filter(s =>
    (!$('#nation').value || s.nation === $('#nation').value) &&
    (!$('#type').value   || s.type   === $('#type').value) &&
    (!$('#tier').value   || s.tier   === $('#tier').value));
}

function refresh(){
  const list = filtered();
  const sel = $('#ship');
  sel.innerHTML = '';
  for(const s of list){
    const o = document.createElement('option');
    o.value = s.key;
    o.textContent = `${s.zh} ${s.en}  ·  ${s.tierZh} ${s.typeZh}  ·  ${s.nationZh} ${s.nation}`;
    sel.appendChild(o);
  }
  $('#count').textContent = `（共 ${list.length} 艘）`;
  CUR = list[0] || null;
  estimate();
}

function estimate(){
  const res = $('#res').value || '3840x2160';
  const nv = document.querySelectorAll('.chip[data-v].on').length;
  const samp = +($('#samples').value || 128);
  const px = res.split('x').reduce((a,b) => a*(+b), 1);
  const views = Math.max(nv, 1);
  // measured: 1080p / 64 samples / 2 views ≈ 180 s
  const base = 180 * (px / (1920*1080)) * (samp/64) * views * 0.55;
  const s = base < 60 ? `${Math.round(base)} 秒`
          : base < 3600 ? `${(base/60).toFixed(1)} 分钟`
          : `${(base/3600).toFixed(1)} 小时`;
  $('#viewhint').textContent = views ? `已选 ${views} 个` : '未选';
  $('#est').textContent = CUR
    ? `已选：${CUR.nationZh} ${CUR.nation} / ${CUR.typeZh} / ${CUR.tierZh} · 预计约 ${s}`
    : '';
}

$('#nation').onchange = $('#type').onchange = $('#tier').onchange = refresh;
$('#ship').onchange = e => {
  CUR = SHIPS.find(s => s.key === e.target.value);
  estimate();
};
$('#res').onchange = estimate;
$('#samples').onchange = estimate;

$('#go').onclick = async () => {
  if(!CUR) return alert('请先选择战舰');
  const views = [...document.querySelectorAll('.chip[data-v].on')].map(c => c.dataset.v);
  if(!views.length) return alert('请至少选择一个视角');
  $('#go').disabled = true;
  $('#shots').innerHTML = '';
  $('#log').textContent = '提交中…';
  setStatus('busy', '提交任务…');

  const r = await fetch('/api/render', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      key: CUR.key,
      res: $('#res').value,
      samples: +$('#samples').value,
      views,
      preset: $('#preset').value,
      tex: 4096
    })
  });
  const j = await r.json();
  if(j.error){ setStatus('err', j.error); $('#go').disabled = false; return; }
  poll(j.id);
};

function setStatus(cls, text){
  $('#dot').className = 'dot ' + cls;
  $('#stat').textContent = text;
}

const STATUS_ZH = {
  queued: '排队中', exporting: '导出模型', sanitizing: '清理网格',
  rendering: '渲染中', done: '完成', error: '出错'
};

function poll(id){
  clearInterval(POLL);
  POLL = setInterval(async () => {
    const j = await (await fetch('/api/job/' + id)).json();
    $('#log').textContent = (j.log || []).join('\n') || '…';
    $('#log').scrollTop = $('#log').scrollHeight;

    const label = STATUS_ZH[j.status] || j.status;
    setStatus(j.status === 'done' ? 'done' : (j.status === 'error' ? 'err' : 'busy'),
              `${label}  ${j.elapsed.toFixed(0)} 秒  ·  ${j.ship.zh || j.ship.key}${j.ship.en ? " " + j.ship.en : ""}`);

    if(j.images && j.images.length) renderShots(j.images);

    if(j.status === 'done'){
      clearInterval(POLL); $('#go').disabled = false;
      setStatus('done', `完成 · 用时 ${j.elapsed.toFixed(0)} 秒 · ${j.images.length} 张`);
    } else if(j.status === 'error'){
      clearInterval(POLL); $('#go').disabled = false;
      setStatus('err', '失败：' + (j.error || '未知错误'));
    }
  }, 1500);
}

function renderShots(imgs){
  const box = $('#shots');
  const have = [...box.children].map(c => c.dataset.v);
  for(const im of imgs){
    if(have.includes(im.view)) continue;
    const v = VIEWS.find(v => v.key === im.view);
    const d = document.createElement('div');
    d.className = 'shot'; d.dataset.v = im.view;
    d.innerHTML =
      `<a href="${im.url}" target="_blank"><img src="${im.url}" loading="lazy"></a>
       <div class="cap">
         <span>${v ? v.zh + '（' + im.view + '）' : im.view}</span>
         <span>${(im.bytes/1048576).toFixed(1)} MB · <a href="${im.url}" download>下载</a></span>
       </div>`;
    box.appendChild(d);
  }
}

init();
setInterval(() => { if(!POLL) estimate(); }, 4000);
</script>
</body>
</html>
"""



class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        path = unquote(urlparse(self.path).path)

        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
            return

        if path == "/api/catalog":
            try:
                self._json(load_catalog())
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        if path == "/api/ships":
            try:
                self._json(load_ships())
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return

        if path.startswith("/api/job/"):
            jid = path.rsplit("/", 1)[-1]
            job = _jobs.get(jid)
            if not job:
                self._json({"error": "unknown job"}, 404)
                return
            self._json(job.to_dict())
            return

        # static render passthrough: /renders/<nation>/<type>/<tier>/<ship>/<view>.png
        if path.startswith("/renders/"):
            rel = path[len("/renders/"):]
            fp = os.path.normpath(os.path.join(OUT_ROOT, *rel.split("/")))
            if not fp.startswith(os.path.normpath(OUT_ROOT)) or not os.path.isfile(fp):
                self._send(404, "not found")
                return
            with open(fp, "rb") as f:
                self._send(200, f.read(), "image/png")
            return

        self._send(404, "not found")

    def do_POST(self):
        if urlparse(self.path).path != "/api/render":
            self._send(404, "not found")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._json({"error": f"bad request: {e}"}, 400)
            return

        key = req.get("key")
        ships = load_ships()
        ship = next((s for s in ships if s["key"] == key), None)
        if not ship:
            self._json({"error": f"unknown ship: {key}"}, 400)
            return

        # validate resolution against the measured ceiling (16K)
        res = req.get("res", "3840x2160")
        try:
            w, h = [int(v) for v in res.lower().split("x")]
        except ValueError:
            self._json({"error": f"bad resolution: {res}"}, 400)
            return
        if w * h > 15360 * 8640:
            self._json({"error": "超出 16K 上限，请降低分辨率"}, 400)
            return

        views = [v for v in req.get("views", ["hero"]) if v][:6] or ["hero"]
        params = {
            "res": f"{w}x{h}",
            "samples": max(1, min(int(req.get("samples", 128)), 1024)),
            "views": views,
            "preset": req.get("preset", "day"),
            "tex": int(req.get("tex", 4096)),
        }

        jid = uuid.uuid4().hex[:12]
        job = Job(jid, ship, params)
        _jobs[jid] = job
        threading.Thread(target=run_job, args=(job,), daemon=True).start()
        self._json({"id": jid})


def main():
    global GAME_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--game-dir", default=None,
                    help="游戏安装目录（默认取 WOWS_GAME_DIR 环境变量，再退回内置默认值）")
    a = ap.parse_args()

    if a.game_dir:
        GAME_DIR = a.game_dir

    print(f"game dir        ->  {GAME_DIR}")
    for name, p in [("blender", BLENDER), ("exporter", EXPORTER), ("list-ships", LIST_SHIPS),
                    ("game dir", GAME_DIR)]:
        if not os.path.exists(p):
            print(f"[warn] missing {name}: {p}")

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"WoWs render UI  ->  http://127.0.0.1:{a.port}")
    print("Ctrl+C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
