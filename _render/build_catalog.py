"""
Build the ship catalogue consumed by the web UI.

Merges three sources:
  1. wows-list-ships.exe      -> ship key + nation/type/tier
  2. game gettext catalogue   -> Chinese display names (IDS_<baseId>)
  3. static label maps        -> Chinese nation / class / tier labels

Writes ships.json next to this file.

Usage: python build_catalog.py [--lang zh]
"""

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WARSHIPS = os.path.dirname(HERE)
TOOLS = os.path.join(WARSHIPS, "_tools")
LIST_SHIPS = os.path.join(TOOLS, "model-exporter", "wows-list-ships.exe")
# 游戏安装目录：优先 WOWS_GAME_DIR 环境变量，否则用内置默认值。
# 也可用 --game /path/to/World of Warships 覆盖。
GAME_DIR = os.environ.get("WOWS_GAME_DIR") or r"D:\SteamLibrary\steamapps\common\World of Warships"

# ---- Chinese labels (in-game terminology) --------------------------------
NATION_ZH = {
    "Commonwealth": "英联邦",
    "Europe": "欧洲",
    "France": "F系",
    "Germany": "D系",
    "Italy": "I系",
    "Japan": "R系",
    "Netherlands": "荷兰",
    "Pan_America": "泛美",
    "Pan-America": "泛美",
    "Pan_Asia": "泛亚",
    "Pan-Asia": "泛亚",
    "Spain": "西班牙",
    "United_Kingdom": "Y系",
    "USA": "M系",
    "Russia": "S系",
    "Events": "活动舰",
}

TYPE_ZH = {
    "Battleship": "战列舰",
    "Cruiser": "巡洋舰",
    "Destroyer": "驱逐舰",
    "AirCarrier": "航空母舰",
    "Submarine": "潜艇",
    "Auxiliary": "辅助舰",
}

TIER_ZH = {
    "1": "I级", "2": "II级", "3": "III级", "4": "IV级", "5": "V级",
    "6": "VI级", "7": "VII级", "8": "VIII级", "9": "IX级",
    "10": "X级", "11": "★级",
}

# render view presets, with Chinese labels
VIEWS = [
    {"key": "hero",  "zh": "主视角",   "desc": "三分之四艏侧"},
    {"key": "side",  "zh": "侧舷",     "desc": "正侧面试"},
    {"key": "stern", "zh": "艉侧",     "desc": "三分之四艉侧"},
    {"key": "bow",   "zh": "艏部",     "desc": "正艏低角"},
    {"key": "top",   "zh": "俯视",     "desc": "高角看甲板"},
    {"key": "low",   "zh": "低角",     "desc": "极低角"},
]

RESOLUTIONS = [
    {"value": "1920x1080", "zh": "1920 × 1080（全高清）"},
    {"value": "2560x1440", "zh": "2560 × 1440（2K）"},
    {"value": "3840x2160", "zh": "3840 × 2160（4K）"},
    {"value": "7680x4320", "zh": "7680 × 4320（8K）"},
    {"value": "15360x8640", "zh": "15360 × 8640（16K · 上限）"},
]

PRESETS = [
    {"value": "day", "zh": "白昼"},
]


def read_mo(path):
    import struct
    with open(path, "rb") as f:
        data = f.read()
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic == 0x950412DE:
        endian = "<"
    elif magic == 0xDE120495:
        endian = ">"
    else:
        raise ValueError("not a .mo file")
    _, num, off_orig, off_trans = struct.unpack_from(endian + "IIII", data, 4)

    def ent(table, i):
        rl, ro = struct.unpack_from(endian + "II", data, table + i * 8)
        return data[ro: ro + rl]

    out = {}
    for i in range(num):
        try:
            k = ent(off_orig, i).decode("utf-8")
            v = ent(off_trans, i).decode("utf-8")
        except UnicodeDecodeError:
            continue
        if "\x04" in k:
            k = k.split("\x04", 1)[1]
        out[k] = v
    return out


def find_mo(game_dir, lang):
    bin_dir = os.path.join(game_dir, "bin")
    builds = sorted(
        (int(d), os.path.join(bin_dir, d))
        for d in os.listdir(bin_dir)
        if d.isdigit() and os.path.isdir(os.path.join(bin_dir, d))
    )
    for _, bdir in reversed(builds):
        mo = os.path.join(bdir, "res", "texts", lang, "LC_MESSAGES", "global.mo")
        if os.path.isfile(mo):
            return mo
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--game", default=GAME_DIR)
    a = ap.parse_args()

    # ---- 1. ship list
    print("reading ship list ...")
    out = subprocess.run(
        [LIST_SHIPS, "-W", a.game],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout

    raws = []
    for line in out.splitlines():
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5 or parts[0] == "Key":
            continue
        key, index, nation, typ, tier = parts[:5]
        if not tier.strip():
            continue
        raws.append({
            "key": key.strip(),
            "nation": nation.strip(),
            "type": typ.strip(),
            "tier": tier.strip(),
        })
    print("  ships:", len(raws))

    # ---- 2. localization
    mo = find_mo(a.game, a.lang)
    names = {}
    if mo:
        print("reading", mo)
        table = read_mo(mo)
        for k, v in table.items():
            if k.startswith("IDS_"):
                names[k[4:]] = v
        print("  name entries:", len(names))
    else:
        print("  [warn] no .mo found, names will be empty")

    # ---- 3. build
    ships = []
    missing = 0
    for s in raws:
        base = s["key"].split("_")[0]
        zh = names.get(base, "")
        if not zh:
            missing += 1
            zh = s["key"]                      # fall back to the raw key
        # English display name: key minus its hull-number prefix, e.g.
        #   PGSB108_Bismarck -> Bismarck ; PJSB007_Kongo_1942 -> Kongo 1942
        parts = s["key"].split("_")
        en = " ".join(parts[1:]) if len(parts) > 1 else s["key"]
        ships.append({
            "key": s["key"],
            "zh": zh,
            "en": en,
            "nation": s["nation"],
            "nationZh": NATION_ZH.get(s["nation"], s["nation"]),
            "type": s["type"],
            "typeZh": TYPE_ZH.get(s["type"], s["type"]),
            "tier": s["tier"],
            "tierZh": TIER_ZH.get(s["tier"], "T" + s["tier"]),
        })
    print("  ships without a Chinese name:", missing)

    ships.sort(key=lambda x: (
        x["nationZh"], x["typeZh"],
        int(x["tier"]) if x["tier"].isdigit() else 99,
        x["key"],
    ))

    payload = {
        "ships": ships,
        "views": VIEWS,
        "resolutions": RESOLUTIONS,
        "presets": PRESETS,
        "nations": sorted({s["nation"] for s in ships}),
        "types": sorted({s["type"] for s in ships}),
        "tiers": sorted({s["tier"] for s in ships}, key=lambda t: int(t) if t.isdigit() else 99),
    }

    dest = os.path.join(HERE, "ships.json")
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print("written:", dest, "(%d ships)" % len(ships))


if __name__ == "__main__":
    main()
