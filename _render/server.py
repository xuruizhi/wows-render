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
import glob
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
WARSHIPS = os.path.dirname(ROOT)
TOOLS = os.path.join(WARSHIPS, "_tools")


def find_blender(tools_dir):
    """Locate blender.exe under _tools/.

    Blender's Windows zip unpacks into a versioned folder such as
    blender-4.5.10-windows-x64, so the exact name changes with every release.
    Priority:
      1. WOWS_BLENDER env var (explicit override)
      2. any _tools/blender-*/blender.exe, preferring the highest version
      3. the historically documented path, so error messages stay familiar
    """
    override = os.environ.get("WOWS_BLENDER")
    if override:
        return override

    import glob
    import re

    def version_key(path):
        # path 形如 .../_tools/blender-4.5.11-windows-x64/blender.exe，
        # 版本号在父目录名里，不能只看 basename。
        m = re.search(r"blender-(\d+)\.(\d+)\.?(\d+)?", os.path.basename(os.path.dirname(path)))
        return tuple(int(g) if g else 0 for g in m.groups()) if m else (0, 0, 0)

    found = glob.glob(os.path.join(tools_dir, "blender-*", "blender.exe"))
    if found:
        return max(found, key=version_key)
    return os.path.join(tools_dir, "blender-4.5.10-windows-x64", "blender.exe")


BLENDER = find_blender(TOOLS)
EXPORTER = os.path.join(TOOLS, "model-exporter", "wows-gltf-exporter.exe")
LIST_SHIPS = os.path.join(TOOLS, "model-exporter", "wows-list-ships.exe")
RENDER_PY = os.path.join(ROOT, "render_ship.py")
SANITIZE_PY = os.path.join(ROOT, "sanitize_glb.py")
EXTRACT_TEX_PY = os.path.join(ROOT, "extract_ship_textures.py")
TEX_DIR = os.path.join(ROOT, "textures")
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
    """Output is flat: one folder per nation, everything else in the filename."""
    n = NATION_MAP.get(ship["nation"], ship["nation"])
    return os.path.join(OUT_ROOT, n)


def file_prefix_for(ship):
    """Filename prefix carrying the info that used to be directory levels.

    Layout is now WoWs_Renders/<nation>/<Ship>_<Type>_T<tier>_<view>_<W>x<H>.png
    so a single folder stays browsable without four levels of nesting.
    """
    t = TYPE_MAP.get(ship["type"], ship["type"]).rstrip("s")
    tier = ship["tier"]
    tier_tag = "T%s" % tier if tier != "11" else "TStar"
    key = os.path.splitext(ship["key"])[0]
    name = "%s_%s_%s" % (key, t, tier_tag)
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)


# ---------------------------------------------------------------- job manager
_jobs = {}
_jobs_lock = threading.Lock()

STAGE_ZH = {
    "queued": "排队中", "exporting": "导出模型", "sanitizing": "清理网格",
    "textures": "提取贴图", "rendering": "渲染中", "running": "进行中",
    "done": "完成", "error": "出错", "skipped": "跳过", "pending": "等待中",
    "cancelled": "已取消",
}


class Job:
    """One render request.

    A request may cover a whole filtered list of ships (e.g. France /
    Battleship / all tiers -> 17 ships). They are rendered strictly in order in
    one background thread; ``ships`` carries every ship plus its own state so
    the page can show which ones are done, running or waiting.
    """

    def __init__(self, jid, ships, params):
        self.id = jid
        self.params = params
        self.status = "queued"     # queued|<stage of current ship>|running|done|error
        self.log = []
        self.error = None
        self.images = []           # images of the ship currently being rendered
        self.started = time.time()
        self.finished = None
        self.cancel = threading.Event()
        # per-ship state, rendered in this order
        self.ships = []
        for s in ships:
            self.ships.append({
                "ship": s,
                "status": "pending",   # pending|running|done|error|skipped|cancelled
                "error": None,
                "images": [],
                "elapsed": None,
                "note": "",
            })
        self.index = -1                # index of the ship being rendered

    @property
    def total(self):
        return len(self.ships)

    def counts(self):
        out = {"done": 0, "error": 0, "skipped": 0, "pending": 0,
               "running": 0, "cancelled": 0}
        for s in self.ships:
            out[s["status"]] = out.get(s["status"], 0) + 1
        return out

    def current(self):
        if 0 <= self.index < len(self.ships):
            return self.ships[self.index]
        return None

    @property
    def ship(self):
        """The ship in focus (kept for older clients polling ``job.ship``)."""
        cur = self.current()
        if cur:
            return cur["ship"]
        return self.ships[0]["ship"] if self.ships else {}

    def to_dict(self):
        cur = self.current()
        return {
            "id": self.id,
            "ship": self.ship,
            "status": self.status,
            "log": self.log[-40:],
            "error": self.error,
            "images": self.images,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "params": self.params,
            "total": self.total,
            "index": self.index,
            "current": (cur["ship"] if cur else None),
            "counts": self.counts(),
            "ships": self.ships,
        }

    def say(self, msg):
        stamp = time.strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {msg}")
        # keep memory bounded on long batches: the UI only shows the tail
        if len(self.log) > 400:
            del self.log[:-250]


def run_batch(job):
    """Render every ship of the job in order, then report a batch summary."""
    params = job.params
    views = params.get("views", ["hero"])
    res_tag = params.get("res", "")
    skip_existing = params.get("skip_existing", False)
    n = job.total

    for i, entry in enumerate(job.ships):
        ship = entry["ship"]
        label = "%d/%d %s %s" % (i + 1, n, ship.get("zh") or "", ship["key"])
        if job.cancel.is_set():
            entry["status"] = "cancelled"
            continue

        job.index = i
        job.status = "running"
        job.images = []

        if skip_existing:
            outdir = out_dir_for(ship)
            prefix = file_prefix_for(ship)
            hero = os.path.join(outdir, f"{prefix}_{views[0]}_{res_tag}.png")
            if os.path.exists(hero):
                entry["status"] = "skipped"
                entry["note"] = "已存在，跳过"
                job.say(f"[{label}] 已存在，跳过")
                continue

        entry["status"] = "running"
        job.say(f"===== [{label}] 国家 {ship['nation']} · {ship['type']} · Tier {ship['tier']} =====")
        t0 = time.time()
        run_ship(job, entry)
        entry["elapsed"] = round(time.time() - t0, 1)
        if entry["status"] not in ("done", "error"):
            entry["status"] = "error" if entry["error"] else "done"

    c = job.counts()
    job.status = "done"
    job.finished = time.time()
    job.images = []
    summary = "批次结束：成功 %d，%s %d，失败 %d%s" % (
        c["done"],
        "已取消" if job.cancel.is_set() else "跳过",
        c["skipped"] + c["cancelled"],
        c["error"],
        "（剩余 %d 艘未渲染）" % c["cancelled"] if job.cancel.is_set() else "")
    job.say(summary)
    failed = [e["ship"]["key"] for e in job.ships if e["status"] == "error"]
    if failed:
        job.say("失败的船：" + ", ".join(failed))


def run_ship(job, entry):
    """Export -> sanitize -> textures -> render for one ship of the batch."""
    ship = entry["ship"]
    p = job.params
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in ship["key"])
    raw_glb = os.path.join(GLB_DIR, safe + ".raw.glb")
    clean_glb = os.path.join(GLB_DIR, safe + ".glb")
    outdir = out_dir_for(ship)
    os.makedirs(outdir, exist_ok=True)
    views = p.get("views", ["hero"])
    prefix = file_prefix_for(ship)
    # only the directory of --out matters to render_ship.py; the real filename is
    # <prefix>_<view>_<W>x<H>.png, which --prefix makes it produce
    hero = os.path.join(outdir, f"{prefix}_{views[0]}_{p.get('res', '')}.png")

    def stage(name):
        # ``job.status`` drives the batch indicator, ``entry["status"]`` the
        # per-ship row in the list on the page
        job.status = name
        entry["status"] = "running"
        entry["note"] = STAGE_ZH.get(name, name)

    try:
        # 1 ---- export
        stage("exporting")
        job.say(f"导出模型 {ship['key']} (贴图上限 {p.get('tex', 4096)}px)…")
        exp = subprocess.run(
            [EXPORTER, "-W", GAME_DIR, "-s", ship["key"], "-o", raw_glb, "-Z", str(p.get("tex", 4096))],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if not os.path.exists(raw_glb):
            tail = "\n".join((exp.stdout or "").splitlines()[-5:])
            raise RuntimeError(f"模型导出失败: {tail or exp.stderr}")

        # 2 ---- sanitize
        stage("sanitizing")
        job.say("清理空 mesh…")
        san = subprocess.run(
            [PYTHON, SANITIZE_PY, raw_glb, clean_glb],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if san.returncode != 0 or not os.path.exists(clean_glb):
            raise RuntimeError(f"清理失败: {san.stderr or san.stdout}")

        # 2b ---- game PBS maps (_a/_mg/_n/_ao); without them the ship renders
        # with flat albedo only and loses the metallic/gloss detail
        stage("textures")
        job.say("提取游戏 PBS 贴图（_a/_mg/_n/_ao）…")
        ship_dir = TEX_DIR
        tex = subprocess.run(
            [PYTHON, EXTRACT_TEX_PY, "--game-dir", GAME_DIR, "--ship", ship["key"],
             "--nation", ship["nation"], "--type", ship["type"], "--out", ship_dir],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        tex_args = []
        if "EXTRACT_OK" in (tex.stdout or ""):
            tex_args = ["--textures", ship_dir]
            job.say("已载入游戏贴图（金属度/光泽、法线、AO）")
        else:
            job.say("贴图不可用，退回平面着色：" + (tex.stderr or "").strip()[-200:])

        # 3 ---- render
        stage("rendering")
        job.say(f"渲染 {p.get('res')} × {len(views)} 视角 (samples={p.get('samples')})…")
        cmd = [
            BLENDER, "-b", "--factory-startup", "-P", RENDER_PY, "--",
            "--glb", clean_glb,
            "--out", hero,
            "--res", p["res"],
            "--samples", str(p["samples"]),
            "--views", ",".join(views),
            "--prefix", prefix,
            "--preset", p.get("preset", "day"),
        ] + tex_args
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
        # render_ship.py writes <prefix>_<view>_<W>x<H>.png into the flat
        # per-nation folder; take whatever this run produced.
        rel_base = "/renders/" + "/".join(
            os.path.relpath(outdir, OUT_ROOT).split(os.sep)
        )
        res_tag = p.get("res", "")
        made = []
        for v in views:
            cand = os.path.join(outdir, f"{prefix}_{v}_{res_tag}.png")
            if not os.path.exists(cand):
                # fall back to the newest matching file (older naming schemes
                # and re-renders at another resolution)
                hits = glob.glob(os.path.join(
                    outdir, glob.escape(prefix) + "_" + glob.escape(v) + "_*.png"))
                cand = max(hits, key=os.path.getmtime) if hits else cand
            if os.path.exists(cand):
                name = os.path.basename(cand)
                made.append({
                    "view": v,
                    "name": name,
                    "url": rel_base + "/" + name,
                    "bytes": os.path.getsize(cand),
                })
        entry["images"] = made
        job.images = made
        entry["status"] = "done"
        entry["error"] = None
        entry["note"] = f"{len(made)} 张"
        job.say(f"完成，共 {len(made)} 张")

    except Exception as e:
        entry["status"] = "error"
        entry["error"] = str(e)
        entry["note"] = "失败"
        job.error = str(e)
        job.say("错误: " + str(e))
    finally:
        for f in (raw_glb, clean_glb):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass


# ---------------------------------------------------------------- job queue
# One worker thread: batches run strictly in order and two Blender/exporter
# processes never fight over the GPU or the same files in glb/.
_work = queue.Queue()


def _worker():
    while True:
        job = _work.get()
        try:
            run_batch(job)
        except Exception as e:          # never let the worker die
            job.status = "error"
            job.error = str(e)
            job.finished = time.time()
            job.say("批次异常结束: " + str(e))
        finally:
            _work.task_done()


threading.Thread(target=_worker, name="render-worker", daemon=True).start()


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
/* batch progress */
.batch{display:none;margin-bottom:12px}
.batch.on{display:block}
.bar{height:7px;background:#0b0f14;border:1px solid var(--line);border-radius:999px;overflow:hidden}
.bar > i{display:block;height:100%;width:0;background:var(--accent);transition:width .3s}
.batch .sum{font-size:11.5px;color:var(--mute);margin-top:6px;display:flex;
            justify-content:space-between;gap:10px}
ol.queue{list-style:none;margin:0 0 4px;padding:0;max-height:236px;overflow:auto;
         border:1px solid var(--line);border-radius:8px}
.queue li{display:grid;grid-template-columns:20px 1fr auto auto;gap:8px;align-items:center;
          padding:5px 9px;font-size:12px;border-bottom:1px solid #1d2530}
.queue li:last-child{border-bottom:none}
.queue li .n{color:#5b6673;text-align:right;font-variant-numeric:tabular-nums}
.queue li .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.queue li .nm em{font-style:normal;color:var(--mute);margin-left:6px;font-size:11px}
.queue li .st{font-size:11px;color:var(--mute);white-space:nowrap}
.queue li .sec{font-size:11px;color:#5b6673;white-space:nowrap;
               font-variant-numeric:tabular-nums;min-width:46px;text-align:right}
.queue li.running{background:rgba(210,153,34,.10)}
.queue li.running .st{color:var(--warn)}
.queue li.done .st{color:var(--ok)}
.queue li.error{background:rgba(248,81,73,.08)}
.queue li.error .st{color:var(--err)}
.queue li.skipped .st,.queue li.cancelled .st{color:#5b6673}
.opt{display:flex;align-items:center;gap:7px;margin-top:12px;font-size:12.5px;
     color:var(--mute);cursor:pointer;user-select:none}
.opt input{width:auto;margin:0}
.btn.ghost{background:#2b3644;color:var(--fg);margin-top:9px}
.btn.ghost:hover{filter:brightness(1.25)}
# 默认全选、可按 Ctrl / Shift 加减选；单击是加/减选，不会把其余选择清空。
# 勾「全部生成」后，提交的就是整张筛选列表，按列表顺序渲染。
#ship{user-select:none}
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
      <select id="ship" size="9" multiple style="height:auto"></select>

      <label class="opt"><input type="checkbox" id="allsel">
        全选本列表（共 <span id="alln">0</span> 艘）· 单击某一行加/减选，Ctrl / Shift 连选</label>

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
      <button class="btn ghost" id="cancel" style="display:none">取消任务</button>
    </div>

    <div class="card">
      <div class="status">
        <span class="dot" id="dot"></span>
        <span id="stat">就绪 — 选择战舰后点击「开始生成」</span>
      </div>
      <div class="batch" id="batch">
        <div class="bar"><i id="barfill"></i></div>
        <div class="sum"><span id="batchsum"></span><span id="batcheta"></span></div>
      </div>
      <ol class="queue" id="queue" style="display:none"></ol>
      <pre class="log" id="log">等待任务…</pre>
      <div class="shots" id="shots"></div>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
let CAT = null, SHIPS = [], CUR = null, POLL = null, VIEWS = [];
let SEL = [], JOBID = null;

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
  resumeRunning();
}

// if a batch was already running (page refreshed, or another tab started it),
// pick the polling back up instead of leaving it invisible
async function resumeRunning(){
  try {
    const st = await (await fetch('/api/jobs')).json();
    const j = (st.jobs || []).find(x => x.status !== 'done' && x.status !== 'error');
    if(!j) return;
    $('#go').disabled = true;
    $('#cancel').style.display = '';
    $('#batch').classList.add('on');
    poll(j.id);
  } catch(e){}
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

function selectedShips(){
  // every ticked row, in list (= catalogue) order. Selection state is read from
  // each <option>; the <select> element's own value/selectedIndex only reflects
  // the last option and must not be used here.
  const box = $('#ship');
  return [...box.options].filter(o => o.selected).map(o => o.value);
}

function refresh(){
  const list = filtered();
  const sel = $('#ship');
  sel.innerHTML = '';
  for(const s of list){
    const o = document.createElement('option');
    o.value = s.key;
    o.textContent = `${s.zh} ${s.en}  ·  ${s.tierZh} ${s.typeZh}  ·  ${s.nationZh} ${s.nation}`;
    // 默认全选整张筛选列表
    o.selected = true;
    sel.appendChild(o);
  }
  sel.multiple = true;
  // 列表默认全选，单击是加/减选（不靠 Ctrl 也能切换）；
  // Ctrl / Shift 仍是原生的「追加 / 范围选择」。
  // 注意：选中状态一律以每个 option.selected 为准，不看 <select> 的
  // value / selectedIndex（后者只代表"最后一项"）。
  sel.onmousedown = e => {
    if(e.ctrlKey || e.metaKey || e.shiftKey) return;
    [...sel.options].forEach(o => { o.selected = false; });
  };
  syncAllBox();
  $('#alln').textContent = list.length;
  CUR = list[0] || null;
  SEL = list.slice();
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

  // batch size = ticked ships (each ship costs the per-ship estimate)
  const keys = selectedShips();
  const n = keys.length;
  SEL = keys.map(k => SHIPS.find(s => s.key === k)).filter(Boolean);
  // 预览信息跟着"真正会提交的清单"走：批量时展示第一艘 = 渲染顺序的第一艘
  CUR = SEL[0] || null;

  const total = base * Math.max(n, 1);
  const tt = total < 60 ? `${Math.round(total)} 秒`
           : total < 3600 ? `${(total/60).toFixed(1)} 分钟`
           : `${(total/3600).toFixed(1)} 小时`;
  if(!CUR){
    $('#est').textContent = '未选择战舰';
  } else if(n > 1){
    $('#est').textContent =
      `将按顺序生成 ${n} 艘 · 每艘约 ${s} · 合计约 ${tt}`;
  } else {
    $('#est').textContent =
      `已选：${CUR.nationZh} ${CUR.nation} / ${CUR.typeZh} / ${CUR.tierZh} · 预计约 ${tt}`;
  }
}

$('#nation').onchange = $('#type').onchange = $('#tier').onchange = refresh;
$('#ship').onchange = () => { syncAllBox(); estimate(); };
$('#ship').oninput = $('#ship').onchange;
// keep the "全部生成" box in step without assuming the browser copied an
// option's selected state onto the <select> element itself
function syncAllBox(){
  const box = $('#ship');
  const n = box.options.length;
  const on = [...box.options].filter(o => o.selected).length;
  $('#allsel').checked = n > 0 && on === n;
  $('#allsel').indeterminate = on > 0 && on < n;
  $('#count').textContent = n ? `（共 ${n} 艘，已选 ${on} 艘）` : '（无匹配战舰）';
}
$('#allsel').onchange = e => {
  const on = e.target.checked;
  const box = $('#ship');
  [...box.options].forEach(o => { o.selected = on; });
  if(on && box.options.length) box.options[box.options.length - 1].selected = true;
  estimate();
};
$('#res').onchange = estimate;
$('#samples').onchange = estimate;

$('#go').onclick = async () => {
  const keys = selectedShips();
  if(!keys.length) return alert('请先在上面的列表里选择战舰（可按住 Ctrl / Shift 多选，或勾选「全部生成」）');
  const views = [...document.querySelectorAll('.chip[data-v].on')].map(c => c.dataset.v);
  if(!views.length) return alert('请至少选择一个视角');
  $('#go').disabled = true;
  $('#shots').innerHTML = '';
  $('#queue').innerHTML = '';
  $('#queue').style.display = '';
  $('#batch').classList.add('on');
  $('#log').textContent = '提交中…';
  setStatus('busy', `提交 ${keys.length} 艘…`);

  let r, j;
  try {
    r = await fetch('/api/render', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        keys,
        res: $('#res').value,
        samples: +$('#samples').value,
        views,
        preset: $('#preset').value,
        tex: 4096
      })
    });
    j = await r.json();
  } catch(err){
    setStatus('err', '提交失败：' + err); $('#go').disabled = false; return;
  }
  if(j.error){ setStatus('err', j.error); $('#go').disabled = false; return; }
  $('#cancel').style.display = '';
  poll(j.id);
};

$('#cancel').onclick = async () => {
  if(!JOBID) return;
  $('#cancel').disabled = true;
  try { await fetch('/api/cancel/' + JOBID, {method: 'POST'}); } catch(e){}
  $('#cancel').disabled = false;
};

function setStatus(cls, text){
  $('#dot').className = 'dot ' + cls;
  $('#stat').textContent = text;
}

const STATUS_ZH = {
  queued: '排队中', pending: '等待中', running: '进行中',
  exporting: '导出模型', sanitizing: '清理网格', textures: '提取贴图',
  rendering: '渲染中', done: '完成', error: '出错',
  skipped: '已跳过', cancelled: '已取消'
};

const SHIP_ZH = {
  pending: '等待中', running: '进行中', done: '完成', error: '出错',
  skipped: '已跳过', cancelled: '已取消'
};

function fmtSec(s){
  if(s == null) return '';
  return s < 60 ? `${Math.round(s)}s` : `${(s/60).toFixed(1)}m`;
}

function renderQueue(j){
  const ol = $('#queue');
  ol.style.display = '';
  // rebuild only when the batch (or its state) changed shape; cheap enough
  const sig = j.ships.map(s => s.status + (s.elapsed || '')).join('|');
  if(ol.dataset.sig === sig) return;
  ol.dataset.sig = sig;
  ol.innerHTML = '';
  j.ships.forEach((e, i) => {
    const s = e.ship;
    const li = document.createElement('li');
    li.className = e.status;
    const st = e.status === 'running'
      ? (STATUS_ZH[j.status] || '进行中')
      : (SHIP_ZH[e.status] || e.status);
    li.innerHTML =
      `<span class="n">${i+1}</span>
       <span class="nm">${s.zh || s.key} <em>${s.key} · ${s.tierZh} ${s.typeZh}</em></span>
       <span class="st">${st}</span>
       <span class="sec">${fmtSec(e.elapsed)}</span>`;
    if(e.error) li.title = e.error;
    ol.appendChild(li);
  });
  const cur = ol.querySelector('li.running');
  if(cur) cur.scrollIntoView({block:'nearest'});
}

function renderBatch(j){
  $('#batch').classList.add('on');
  const c = j.counts || {};
  const total = j.total || 1;
  const fin = (c.done||0) + (c.error||0) + (c.skipped||0) + (c.cancelled||0);
  $('#barfill').style.width = `${Math.round(fin / total * 100)}%`;
  $('#batchsum').textContent =
    `${fin} / ${total} · 完成 ${c.done||0} · 跳过 ${(c.skipped||0)+(c.cancelled||0)} · 失败 ${c.error||0}`;
  // rough remaining time from the ships finished so far
  const doneEls = j.ships.filter(s => s.elapsed);
  const avg = doneEls.length
    ? doneEls.reduce((a, s) => a + s.elapsed, 0) / doneEls.length : null;
  const left = total - fin;
  $('#batcheta') .textContent = (avg && left > 0 && j.status !== 'done')
    ? `剩余约 ${fmtSec(avg * left)}（每艘均 ${fmtSec(avg)}）` : '';
}

function applyJob(j){
  $('#log').textContent = (j.log || []).join('\n') || '…';
  $('#log').scrollTop = $('#log').scrollHeight;
  if(j.total) renderBatch(j);
  if(j.ships) renderQueue(j);
  if(j.images && j.images.length) renderShots(j.images, j.current);

  const label = STATUS_ZH[j.status] || j.status;
  if(j.status === 'done'){
    const c = j.counts || {};
    setStatus((c.error ? 'err' : 'done'),
      `批次完成 · 用时 ${fmtSec(j.elapsed)} · 完成 ${c.done||0} · 跳过 ${(c.skipped||0)+(c.cancelled||0)} · 失败 ${c.error||0}`);
  } else if(j.status === 'error'){
    setStatus('err', '失败：' + (j.error || '未知错误'));
  } else {
    const cur = j.current;
    setStatus('busy',
      `${label} · ${j.index >= 0 ? j.index + 1 : 0} / ${j.total} · ${cur ? (cur.zh || cur.key) : ''}`);
  }
}

function poll(id){
  JOBID = id;
  clearInterval(POLL);
  const tick = async () => {
    let j;
    try { j = await (await fetch('/api/job/' + id)).json(); }
    catch(e){ return; }              // transient, try again on the next tick
    if(!j || j.error) return;
    applyJob(j);
    if(j.status === 'done' || j.status === 'error'){
      clearInterval(POLL); POLL = null;
      $('#go').disabled = false;
      $('#cancel').style.display = 'none';
    }
  };
  tick();
  POLL = setInterval(tick, 2000);
}

function renderShots(imgs, ship){
  const box = $('#shots');
  const have = [...box.children].map(c => c.dataset.v);
  for(const im of imgs){
    if(have.includes(im.view)) continue;
    const v = VIEWS.find(v => v.key === im.view);
    const d = document.createElement('div');
    d.className = 'shot'; d.dataset.v = im.view;
    const who = ship ? `${ship.zh || ship.key} · ` : '';
    d.innerHTML =
      `<a href="${im.url}" target="_blank"><img src="${im.url}" loading="lazy"></a>
       <div class="cap">
         <span>${who}${v ? v.zh + '（' + im.view + '）' : im.view}</span>
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

        # cheap "is this already rendered at the current settings" probe, so the
        # page can show how many of the selected ships still need a render
        if path == "/api/done":
            q = parse_qs(urlparse(self.path).query)
            res = q.get("res", [""])[0]
            view = q.get("view", ["hero"])[0]
            keys = [k for k in q.get("key", []) if k]
            try:
                ships = load_ships()
            except Exception as e:
                self._json({"error": str(e)}, 500)
                return
            by_key = {s["key"]: s for s in ships}
            done = []
            for k in keys:
                s = by_key.get(k)
                if not s:
                    continue
                hero = os.path.join(out_dir_for(s), f"{file_prefix_for(s)}_{view}_{res}.png")
                if os.path.exists(hero):
                    done.append(k)
            self._json({"done": done, "total": len(keys)})
            return

        # lightweight list of jobs (no per-ship payload) — used by the page to
        # re-attach to a batch that is still running after a refresh
        if path == "/api/jobs":
            jobs = [{
                "id": j.id,
                "status": j.status,
                "total": j.total,
                "index": j.index,
                "started": j.started,
                "finished": j.finished,
            } for j in list(_jobs.values())]
            jobs.sort(key=lambda x: x["started"], reverse=True)
            self._json({"jobs": jobs[:20]})
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
        route = urlparse(self.path).path

        # cancel a running batch: the current ship finishes, the rest are marked
        if route.startswith("/api/cancel/"):
            jid = route.rsplit("/", 1)[-1]
            job = _jobs.get(jid)
            if not job:
                self._json({"error": "unknown job"}, 404)
                return
            job.cancel.set()
            job.say("收到取消请求，当前这艘完成后停止")
            self._json({"ok": True})
            return

        if route != "/api/render":
            self._send(404, "not found")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._json({"error": f"bad request: {e}"}, 400)
            return

        # Ship selection. New clients send every selected ship:
        #   {"keys": [...]}            explicit list, order preserved
        #   {"nation","type","tier"}   filter ("" = any), order as in the catalogue
        # Old clients send a single {"key": "..."}.
        ships = load_ships()
        requested = [k for k in (req.get("keys") or []) if k]
        if not requested and req.get("key"):
            requested = [req["key"]]

        if requested:
            by_key = {s["key"]: s for s in ships}
            unknown = [k for k in requested if k not in by_key]
            if unknown:
                self._json({"error": f"unknown ship(s): {', '.join(unknown[:5])}"}, 400)
                return
            selected = [by_key[k] for k in requested]
        else:
            nation = req.get("nation") or ""
            typ = req.get("type") or ""
            tier = req.get("tier") or ""
            selected = [s for s in ships
                        if (not nation or s["nation"] == nation)
                        and (not typ or s["type"] == typ)
                        and (not tier or s["tier"] == tier)]
        if not selected:
            self._json({"error": "没有选中任何战舰"}, 400)
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
            # true = leave ships that already have their hero view at this
            # resolution alone (matched by file name prefix)
            "skip_existing": bool(req.get("skip_existing", False)),
        }

        previous = [j for j in _jobs.values() if j.status not in ("done", "error")]
        if previous:
            self._json({"error": "已有任务在跑（任务 %s 共 %d 艘）。等它结束或先点「取消」再提交。"
                                  % (previous[0].id, previous[0].total)}, 409)
            return

        jid = uuid.uuid4().hex[:12]
        job = Job(jid, selected, params)
        _jobs[jid] = job
        # record the exact order so a "only one ship got rendered" report can
        # be checked against what the browser actually submitted
        print("[job %s] %d 艘: %s" % (
            jid, len(selected), ", ".join(s["key"] for s in selected)), flush=True)
        _work.put(job)
        self._json({"id": jid, "total": len(selected),
                    "keys": [s["key"] for s in selected]})


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
