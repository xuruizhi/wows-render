"""
Extract Chinese ship names from the game's gettext catalogue.

The game ships gettext `.mo` files under
  <game>\bin\<build>\res\texts\<lang>\LC_MESSAGES\global.mo

Ship display names live under msgids matching an IDS_ prefix, e.g.
  IDS_PGSB108_Bismarck  ->  "俾斯麦"
Ship class/type names are plain keys like IDS_SHIPTYPE_BATTLESHIP.

Output: shipnames_<lang>.json  { "<ship key>": "<localized name>", ... }

Usage: python extract_names.py [--lang zh] [--out shipnames.json]
"""

import argparse
import json
import os
import struct
import sys

# 游戏安装目录：优先 WOWS_GAME_DIR 环境变量，否则用内置默认值。
# 也可用 --game /path/to/World of Warships 覆盖。
DEFAULT_GAME = os.environ.get("WOWS_GAME_DIR") or r"D:\SteamLibrary\steamapps\common\World of Warships"


def read_mo(path):
    """Parse a GNU gettext .mo file into {msgid: msgstr}."""
    with open(path, "rb") as f:
        data = f.read()

    magic = struct.unpack_from("<I", data, 0)[0]
    if magic == 0x950412DE:
        endian = "<"
    elif magic == 0xDE120495:
        endian = ">"
    else:
        raise ValueError("not a .mo file: %s" % path)

    version, num_strings, off_orig, off_trans = struct.unpack_from(endian + "IIII", data, 4)

    def read_entry(table_off, idx):
        rel_len, rel_off = struct.unpack_from(endian + "II", data, table_off + idx * 8)
        return data[rel_off: rel_off + rel_len]

    out = {}
    for i in range(num_strings):
        raw_key = read_entry(off_orig, i)
        raw_val = read_entry(off_trans, i)
        try:
            key = raw_key.decode("utf-8")
            val = raw_val.decode("utf-8")
        except UnicodeDecodeError:
            continue
        # msgids may be context-qualified ("ctx\x04id")
        if "\x04" in key:
            key = key.split("\x04", 1)[1]
        out[key] = val
    return out


def find_mo(game_dir, lang):
    """Locate global.mo for a language under the newest build dir."""
    bin_dir = os.path.join(game_dir, "bin")
    builds = []
    for d in os.listdir(bin_dir):
        p = os.path.join(bin_dir, d)
        if os.path.isdir(p) and d.isdigit():
            builds.append((int(d), p))
    builds.sort(reverse=True)  # newest build first

    for _, bdir in builds:
        mo = os.path.join(bdir, "res", "texts", lang, "LC_MESSAGES", "global.mo")
        if os.path.isfile(mo):
            return mo
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default=DEFAULT_GAME)
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    mo_path = find_mo(a.game, a.lang)
    if not mo_path:
        sys.exit("global.mo not found for lang=%s under %s" % (a.lang, a.game))
    print("reading:", mo_path)

    table = read_mo(mo_path)
    print("total msgids:", len(table))

    # Ship names in GameParams keys look like PGSB108_Bismarck / RJSB018_Amagi_1942.
    # The catalogue keys them as IDS_<key> (sometimes with different case).
    ships = {}
    for k, v in table.items():
        if not k.startswith("IDS_"):
            continue
        rest = k[4:]
        if rest.startswith("PG") or rest.startswith("RJS") or rest.startswith("PJS"):
            ships[rest] = v

    out_path = a.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "shipnames_%s.json" % a.lang)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(ships, f, ensure_ascii=False, indent=1, sort_keys=True)

    print("extracted ship names:", len(ships))
    print("written:", out_path)

    for k in list(ships)[:8]:
        print("  %s -> %s" % (k, ships[k]))


if __name__ == "__main__":
    main()
