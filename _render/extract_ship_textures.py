"""Extract the PBR texture set for a WoWs ship from the game packages.

Why this exists
---------------
The game's PBS ship shader (shaders/materials/pbs/ship_material) samples:

  diffuseMap          <ship>_<part>_a    albedo
  normalMap           <ship>_<part>_n    tangent-space normal
  metallicGlossMap    <ship>_<part>_mg   packed: R,G = metallic, B = gloss
  ambientOcclusionMap <ship>_<part>_ao   ambient occlusion

`wows-gltf-exporter` only reads `_a`, which is why renders looked flat and
plastic. This script pulls the companion maps out of the game as PNG.

DDS storage detail
------------------
WoWs stores each mip level as its own complete DDS file: `<name>.dd0` holds the
largest mip, `.dd1`/`.dd2` the next ones down, `<name>.dds` the small tail. The
full-resolution image is therefore `.dd0`, which Pillow opens directly.

Usage
-----
  python extract_ship_textures.py --ship GSB001_Bismarck_1941 \\
      --nation germany --type battleship --out textures/

  # ship key instead of resource dir (exporter naming), nation/type required
  python extract_ship_textures.py --ship PGSB108_Bismarck \\
      --nation germany --type battleship --out textures/
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEPACK = os.path.join(os.path.dirname(HERE), "_tools", "wows-depack", "wows-depack-cli.exe")

PART_MAPS = ("a", "mg", "n", "ao")

# ship lists say "Battleship"/"Cruiser"; the resource tree is lowercase
TYPE_DIRS = {
    "battleship": "battleship", "cruiser": "cruiser", "destroyer": "destroyer",
    "aircarrier": "aircarrier", "aircraftcarrier": "aircarrier",
    "submarine": "submarine",
}
# ship-list nation names -> content/gameplay directory names
NATION_DIRS = {
    "germany": "germany", "usa": "usa", "japan": "japan", "russia": "russia",
    "united_kingdom": "britain", "uk": "britain", "france": "france",
    "italy": "italy", "pan_asia": "panasia", "panasia": "panasia",
    "pan_america": "panamerica", "panamerica": "panamerica",
    "europe": "europe", "commonwealth": "commonwealth", "netherlands": "netherlands",
    "spain": "spain", "poland": "poland",
}


def normalize_type(t):
    key = re.sub(r"[^a-z]", "", (t or "").lower())
    return TYPE_DIRS.get(key)


def normalize_nation(n):
    return NATION_DIRS.get((n or "").lower())


def find_index_dir(game_dir):
    """Pick the newest build's idx directory (highest numeric folder name)."""
    best = None
    bin_dir = os.path.join(game_dir, "bin")
    for name in os.listdir(bin_dir):
        idx = os.path.join(bin_dir, name, "idx")
        if os.path.isdir(idx) and os.path.exists(os.path.join(idx, "basecontent.idx")):
            if name.isdigit() and (best is None or int(name) > int(best[0])):
                best = (name, idx)
    return best[1] if best else None


def depack_extract(game_dir, idx_dir, res_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    cmd = [DEPACK, "-W", game_dir, "-I", idx_dir, "-e", res_path, "-O", out_dir]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return (r.stderr or r.stdout or "").strip()


def find_texture_dir(root, ship_file):
    for dirpath, _dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) != "textures":
            continue
        if any(fn.startswith(ship_file + "_") and fn.endswith(".dds")
               for fn in filenames):
            return os.path.abspath(dirpath)
    return None


def list_parts(text_dir, ship_file):
    """Texture "parts" present for this ship.

    Two layouts exist:
      <ship>_hull_a / <ship>_DeckHouse_a   (Bismarck: per-part sets)
      <ship>_a                             (Emden: one set for the whole ship)
    Return the part suffix, with "" meaning the no-part layout.
    """
    pat = re.compile(r"^%s_(.+)_(%s)\.(dds|dd\d)$"
                     % (re.escape(ship_file), "|".join(PART_MAPS)))
    parts = set()
    for fn in os.listdir(text_dir):
        m = pat.match(fn)
        if m:
            parts.add(m.group(1))
    return sorted(parts)


def stem_for(ship_file, part):
    return "%s_%s" % (ship_file, part) if part else ship_file


def convert_dds(base_no_ext, out_png):
    from PIL import Image
    src = None
    for ext in (".dd0", ".dd1", ".dd2", ".dds"):
        cand = base_no_ext + ext
        if os.path.exists(cand):
            src = cand
            break
    if not src:
        return None
    im = Image.open(src).convert("RGBA")
    im.save(out_png)
    return im.size


def resolve_resource_dir(game_dir, ship_key, nation, type_name):
    """Ask the exporter which resource directory a ship key maps to.

    The trailing hull number cannot be derived from the ship key (PJSB007 ->
    JSB007 is coincidence; PGSC502 -> GSC020 is not), so we read the model path
    the exporter reports. Falls back to the key itself when the ship key is
    already a resource-dir name.
    """
    if re.match(r"^G[A-Z]{2}\d{3}_", ship_key):
        return ship_key
    exporter = os.path.join(os.path.dirname(HERE), "_tools", "model-exporter",
                            "wows-gltf-exporter.exe")
    if not os.path.exists(exporter):
        return None
    tmp = os.path.join(HERE, "_probe_out", "_resdir_tmp.glb")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    r = subprocess.run([exporter, "-W", game_dir, "-s", ship_key, "-o", tmp,
                        "-Z", "64", "-v"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    text = (r.stderr or "") + (r.stdout or "")
    m = re.search(r"hull model:\s*content/gameplay/([^/]+)/ship/([^/]+)/([^/]+)/",
                  text)
    if os.path.exists(tmp):
        os.remove(tmp)
    if not m:
        return None
    return m.group(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-dir",
                    default=os.environ.get("WOWS_GAME_DIR",
                                           r"D:\SteamLibrary\steamapps\common\World of Warships"))
    ap.add_argument("--ship", required=True,
                    help="resource dir (GSB001_Bismarck_1941) or ship key (PGSB108_Bismarck)")
    ap.add_argument("--nation", default="")
    ap.add_argument("--type", default="",
                    help="battleship / cruiser / destroyer / aircarrier / submarine")
    ap.add_argument("--out", required=True)
    ap.add_argument("--keep-dds", action="store_true",
                    help="also copy the raw DDS parts next to the PNGs")
    ap.add_argument("--camo", action="store_true",
                    help="also extract the ship's camouflage masks "
                         "(<ship>_<part>_camo_<NN>.png)")
    args = ap.parse_args()

    idx_dir = find_index_dir(args.game_dir)
    if not idx_dir:
        sys.exit("cannot find an idx directory under %s/bin" % args.game_dir)

    ship = resolve_resource_dir(args.game_dir, args.ship, args.nation, args.type)
    if not ship:
        sys.exit("cannot resolve a resource dir for %r" % args.ship)
    nation_dir = normalize_nation(args.nation)
    type_dir = normalize_type(args.type)
    if not (nation_dir and type_dir):
        sys.exit("unknown nation/type: %r / %r" % (args.nation, args.type))
    print("idx:", idx_dir)
    print("ship:", ship, "|", nation_dir, type_dir)

    work = os.path.join(args.out, "_extract")
    shutil.rmtree(work, ignore_errors=True)
    res_path = "content/gameplay/%s/ship/%s" % (nation_dir, type_dir)
    log = depack_extract(args.game_dir, idx_dir, res_path, work)
    text_dir = find_texture_dir(work, ship)
    if not text_dir:
        sys.exit("no textures for %s under %s\n%s" % (ship, work, log))
    print("textures:", text_dir)
    parts = list_parts(text_dir, ship)
    # no per-part suffix: the ship has a single texture set directly named
    # <ship>_a / _mg / _n / _ao
    if not parts and any(os.path.exists(os.path.join(text_dir, "%s_%s.dds" % (ship, m)))
                         or os.path.exists(os.path.join(text_dir, "%s_%s.dd0" % (ship, m)))
                         for m in PART_MAPS):
        parts = [""]
    print("parts:", parts or ["<whole ship>"])
    if not parts:
        sys.exit("no <ship>[_<part>]_{a,mg,n,ao} files found")

    os.makedirs(args.out, exist_ok=True)
    made = []
    for part in parts:
        stem = stem_for(ship, part)
        label = part or "ship"
        for m in PART_MAPS:
            base = os.path.join(text_dir, "%s_%s" % (stem, m))
            if not any(os.path.exists(base + e) for e in (".dds", ".dd0")):
                print("  %-10s missing %s" % (label, m))
                continue
            png = os.path.join(args.out, "%s_%s.png" % (stem, m))
            size = convert_dds(base, png)
            if not size:
                continue
            made.append(png)
            print("  %-10s %-3s -> %s %dx%d"
                  % (label, m, os.path.basename(png), size[0], size[1]))
            if args.keep_dds:
                for e in (".dds", ".dd0", ".dd1", ".dd2"):
                    if os.path.exists(base + e):
                        shutil.copy(base + e, args.out)

    if not made:
        sys.exit("nothing extracted")

    # ---- camouflage masks live in a shared tree, not next to the ship ----
    # <ship>_<part>_camo_<NN>.png ; camoColorMask channels index painted regions
    if args.camo:
        camo_out = os.path.join(args.out, "_camo")
        shutil.rmtree(camo_out, ignore_errors=True)
        depack_extract(args.game_dir, idx_dir,
                       "content/gameplay/common/camouflage", camo_out)
        stems = set()
        for part in parts:
            stems.add(stem_for(ship, part))
        camo_made = 0
        for dirpath, _d, files in os.walk(camo_out):
            for fn in files:
                low = fn.lower()
                if "_camo" not in low or not low.endswith((".dds", ".dd0",
                                                           ".dd1", ".dd2")):
                    continue
                base_name = re.sub(r"\.(dds|dd\d)$", "", fn)
                if not any(base_name.startswith(s + "_camo") for s in stems):
                    continue
                src = os.path.join(dirpath, base_name)
                png = os.path.join(args.out, base_name + ".png")
                if os.path.exists(png):
                    continue
                size = convert_dds(src, png)
                if size:
                    camo_made += 1
                    print("  camo       %-3s -> %s %dx%d"
                          % ("", os.path.basename(png), size[0], size[1]))
        print("CAMO_OK", camo_made)

    if not args.keep_dds:
        shutil.rmtree(work, ignore_errors=True)
    print("EXTRACT_OK", len(made), "files")


if __name__ == "__main__":
    main()
