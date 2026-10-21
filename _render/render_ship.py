"""
WoWs ship GLB -> photorealistic render (Blender 4.5 / Cycles GPU).

Usage:
  blender -b --factory-startup -P render_ship.py -- --glb <in.glb> --out <out.png>
          [--res 3840x2160] [--samples 128] [--views hero,side,bow]
          [--engine gpu|cpu] [--transparent] [--preset day]

Design notes:
  * WoWs geometry is Z-up in the game; Blender's glTF importer applies the
    Y-up conversion, so after import the long horizontal axis is Y.
  * Auto-frames the camera from the real bounding box so it works on any ship.
  * Needs no external assets: procedural sky + ocean shader.
"""

import bpy
import sys
import os
import re
import glob
import math
import argparse
from mathutils import Vector, Euler
from bpy_extras.object_utils import world_to_camera_view

argv = sys.argv
argv = argv[argv.index("--") + 1:] if "--" in argv else []

ap = argparse.ArgumentParser()
ap.add_argument("--glb", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--res", default="3840x2160")
ap.add_argument("--samples", type=int, default=128)
ap.add_argument("--views", default="hero")
ap.add_argument("--engine", default="gpu")
ap.add_argument("--transparent", action="store_true")
ap.add_argument("--preset", default="day")
ap.add_argument("--exposure", type=float, default=0.0,
                help="extra exposure on top of the preset (PBS maps darken the "
                     "ship, +0.45 restores parity with flat-albedo renders)")
ap.add_argument("--metal", type=float, default=1.0,
                help="metal roughness fallback multiplier (rails only)")
ap.add_argument("--roughness", type=float, default=1.0,
                help="roughness fallback multiplier for map-less materials")
ap.add_argument("--textures", default="",
                help="dir with <ship>_<part>_{a,mg,n,ao}.png from extract_ship_textures.py")
ap.add_argument("--roughness-min", type=float, default=0.06,
                help="roughness at gloss=1 (fully polished)")
ap.add_argument("--roughness-max", type=float, default=0.92,
                help="roughness at gloss=0 (fully matte)")
ap.add_argument("--ao-strength", type=float, default=0.35,
                help="how far AO darkens at its lowest; AO is a light-occlusion "
                     "term, not a paint decal")
ap.add_argument("--ao-pivot", type=float, default=0.72,
                help="AO value treated as neutral (the maps average ~0.715)")
ap.add_argument("--ao-brighten", type=float, default=0.0,
                help="how far AO lifts above neutral at its highest")
# g_pbsExtras global modulators the game applies to every ship material
ap.add_argument("--wetness", type=float, default=0.0,
                help="overallWetness: 0..1, lowers roughness as a wet film would "
                     "(the game raises this in rain/at sea)")
ap.add_argument("--specular-mult", type=float, default=1.0,
                help="specularMult: global scale on specular reflection")
# camouflage: camoColorMask channels index painted regions, tinted per palette
ap.add_argument("--camo", default="none",
                help="camo scheme suffix to apply (e.g. 01); needs the "
                     "<stem>_camo_<NN>.png mask extracted into --textures")
ap.add_argument("--camo-repeats", type=float, default=1.0,
                help="camoRepeats: UV tiling of the camo mask")
ap.add_argument("--camo-colors", type=float, nargs=9,
                default=None,
                help="camoColor palette: 3 x RGB for mask channels R,G,B "
                     "(the game's palette lives in GameParams.data)")
ap.add_argument("--fill-light", action="store_true",
                help="add the old area fill lamp (brighter, but the game has no "
                     "fill light; off by default now)")
ap.add_argument("--no-normal-map", action="store_true",
                help="debug: skip the _n map")
ap.add_argument("--no-normal-flip", action="store_true",
                help="debug: do not flip normal green (DirectX -> Blender)")
ap.add_argument("--normal-mode", default="bump", choices=["bump", "rg"],
                help="how to use the _n map: bump treats B as a height field "
                     "(default, matches the game's clean plating); rg rebuilds a "
                     "tangent normal from R/G (legacy, produces corrugation)")
ap.add_argument("--normal-strength", type=float, default=0.10,
                help="bump strength for --normal-mode bump; higher values make "
                     "the plating look pitted")
ap.add_argument("--normal-distance", type=float, default=0.02,
                help="bump distance for --normal-mode bump")
ap.add_argument("--no-metallic-map", action="store_true",
                help="debug: skip metallic from the _mg map")
# Sun vs environment balance.
# Measured against an in-game screenshot (ship-only pixels: mean 134.5, p50 146):
# the old sun 1.0 / env 1.45 split under-lit the hull (mean 72.9) while flooding
# the sky (196.4), i.e. everything looked washed out and flat. The game is the
# opposite: a strong directional sun with a dimmer sky. sun 3.0 / env 0.35 brings
# ship mean to 137.8 and p50 to 141.6, matching the reference.
ap.add_argument("--sun-boost", type=float, default=3.0,
                help="multiplier on sun energy; the game's key light is strong")
ap.add_argument("--detail", type=float, default=0.0,
                help="procedural detail-normal strength standing in for the "
                     "game's detailMap; off by default because at whole-ship "
                     "distance it reads as pitting rather than plate detail")
ap.add_argument("--detail-scale", type=float, default=180.0,
                help="tiling scale of the procedural detail normal")
ap.add_argument("--env-boost", type=float, default=0.35,
                help="environment (PMREM stand-in) strength multiplier; kept low "
                     "for the same reason, so the sky stays behind the ship")
ap.add_argument("--haze-strength", type=float, default=0.20,
                help="atmospheric haze mix cap; high values desaturate the hull")
# GT tonemap approximation of g_tonemapParams (see docs/game-rendering-pipeline.md).
# On by default: it is the game's curve, so the default look should use it.
# --no-gt-tonemap falls back to a plain AgX render.
ap.add_argument("--gt-tonemap", dest="gt_tonemap", action="store_true", default=True,
                help="apply the game's GT curve (default)")
ap.add_argument("--no-gt-tonemap", dest="gt_tonemap", action="store_false",
                help="disable the GT curve and render with the view transform only")
ap.add_argument("--gt-black", type=float, default=0.002,
                help="GT gtBlack: input level clipped to zero")
ap.add_argument("--gt-linear-start", type=float, default=0.08,
                help="GT gtLinearSectionStart: identity below this level")
ap.add_argument("--gt-linear-len", type=float, default=0.52,
                help="GT gtLinearSectionLength")
ap.add_argument("--gt-contrast", type=float, default=1.18,
                help="GT gtContrast: slope of the linear section")
ap.add_argument("--gt-exposure", type=float, default=0.55,
                help="GT exposure: multiplies the HDR frame before the LUT so the "
                     "bright sky lands inside the curve's 0..1 domain")
ap.add_argument("--mist-scale", type=float, default=0.62,
                help="scales the mist colour under --gt-tonemap so the sky keeps "
                     "gradient instead of clipping to white")
ap.add_argument("--view-transform", default="AgX",
                choices=["AgX", "Standard", "Filmic", "Filmic Log", "Khronos PBR Neutral"])
# auto exposure: stands in for the game's wg_average_luminance metering pass
ap.add_argument("--auto-exposure", dest="auto_exposure", action="store_true",
                default=True,
                help="meter each view with a low-sample probe render and correct "
                     "exposure (default, mirrors the game)")
ap.add_argument("--no-auto-exposure", dest="auto_exposure", action="store_false",
                help="use the fixed preset exposure only")
ap.add_argument("--auto-key", type=float, default=0.18,
                help="target median luminance the metering aims for")
ap.add_argument("--auto-strength", type=float, default=0.35,
                help="how much of the computed shift to apply (0..1); the game "
                     "adapts gradually rather than snapping to the target")
ap.add_argument("--auto-max", type=float, default=0.8,
                help="clamp on the per-view exposure correction, in stops")
args = ap.parse_args(argv)

RES_X, RES_Y = [int(v) for v in args.res.lower().split("x")]

# --------------------------------------------------------------- light presets
# (sun_elevation_deg, sun_azimuth_deg, sun_energy, sky_strength, fill_energy_scale, exposure)
# Only one look is offered: bright, clean daylight. The ship should read as
# light grey against a pale sky, not as a dark silhouette.
#
# Exposure note: once the game's PBS maps are wired in (--textures), the normal
# map's micro-shading darkens the ship by roughly 7 levels, much like the game
# compensates with extra ship-side indirect light (indirectMultShips). PBS_EXPOSURE
# below is added automatically whenever maps are in use.
PBS_EXPOSURE = 0.45
PRESETS = {
    "day": (35.0, 125.0, 5.6, 1.70, 0.70, 0.60),  # high sun, clean blue
}
SUN_EL, SUN_AZ, SUN_E, SKY_S, FILL_S, EXPO = PRESETS.get(args.preset, PRESETS["day"])

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene

# --------------------------------------------------------------- import
bpy.ops.import_scene.gltf(filepath=args.glb)
meshes = [o for o in scene.objects if o.type == "MESH"]
if not meshes:
    print("RENDER_ERROR: no meshes")
    sys.exit(3)

# --------------------------------------------------------------- materials
# The exporter writes only the albedo: every material lands on
# metallic=0 / roughness=0.8, which reads flat and plastic. The game instead
# feeds its PBS ship shader (shaders/materials/pbs/ship_material) four maps:
#
#   diffuseMap          <ship>_<part>_a    albedo
#   normalMap           <ship>_<part>_n    tangent-space normal
#   metallicGlossMap    <ship>_<part>_mg   R,G = metallic   B = gloss
#   ambientOcclusionMap <ship>_<part>_ao   ambient occlusion
#
# so we load those companions (see extract_ship_textures.py) and drive the
# Principled BSDF from them. Channel roles for _mg were established against
# the albedo: painted areas read B≈0.38 while rust/wood read B≈0.00, i.e. B is
# gloss and R,G carry metallic.
TEX_DIR = os.path.abspath(args.textures) if args.textures else ""
GLOSS_FROM_B = True                # _mg blue channel == glossiness


def load_img(path, colorspace):
    img = bpy.data.images.load(path, check_existing=True)
    img.colorspace_settings.name = colorspace
    return img


def find_part_textures(mat_name):
    """Map a Blender material name to a texture stem present in TEX_DIR.

    Three on-disk layouts are seen across ships:
      <ship>_hull_a / <ship>_DeckHouse_a   per-part, lowercase (Bismarck)
      <ship>_Hull_a / <ship>_Deck_House_a  per-part, capitalised/underscored (Kongo)
      <ship>_a                             one set for the whole ship (Emden)
    Match case-insensitively on a normalised part key.
    """
    if not TEX_DIR or not os.path.isdir(TEX_DIR):
        return None
    stem = mat_name.rsplit(".", 1)[0]

    def norm(s):
        return re.sub(r"[^a-z]", "", s.lower())

    # index every *_a.png in the directory by its normalised stem
    if not hasattr(find_part_textures, "_index"):
        idx = {}
        for fn in os.listdir(TEX_DIR):
            if fn.endswith("_a.png"):
                idx[norm(fn[:-6])] = fn[:-6]     # keep the real on-disk casing
        find_part_textures._index = idx
    index = find_part_textures._index

    for part in ("hull", "deckhouse"):
        nstem = norm(stem)
        # "<ship>_hull" -> "<ship>"; otherwise the material is the bare ship name
        owner = stem[:-(len(part) + 1)] if nstem.endswith("_" + part) else stem
        key = norm("%s_%s" % (owner, part))
        if key in index:
            return index[key]
    if norm(stem) in index:
        return index[norm(stem)]
    return None


def build_pbs_material(mat, stem):
    """Rebuild a material the way the game's PBS shader reads its maps."""
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if not bsdf:
        return False
    base = os.path.join(TEX_DIR, stem)

    # --- albedo (sRGB) keeps whatever the exporter already wired up
    uv = nt.nodes.new("ShaderNodeUVMap")          # glTF imports UV0 as this map
    uv.uv_map = "UVMap"

    # --- normal map: R,G hold tangent xy; B is NOT a usable z (it correlates
    # negatively with the true normal), so z is rebuilt from x,y. This is the
    # two-channel normal format WoWs ships for hulls.
    #
    # The game also samples a detailMap ("Detail normal(rg), albedo(a)"), but no
    # ship in the install ships a _d texture -- only a/mg/n/ao exist -- so the
    # detail layer is generated procedurally here with the same slot semantics
    # (rg = fine normal detail) to get the close-up plate/weld micro-structure.
    n_path = base + "_n.png"
    use_normal = os.path.exists(n_path) and not args.no_normal_map
    if use_normal:
        n_tex = nt.nodes.new("ShaderNodeTexImage")
        n_tex.image = load_img(n_path, "Non-Color")
        n_tex.interpolation = "Smart"
        nt.links.new(uv.outputs["UV"], n_tex.inputs["Vector"])
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        nt.links.new(n_tex.outputs["Color"], sep.inputs["Color"])

        # x,y in [0,1] -> [-1,1]; WoWs uses the DirectX convention (green down)
        def signed(ch):
            m = nt.nodes.new("ShaderNodeMath")
            m.operation = "MULTIPLY_ADD"
            m.inputs[1].default_value = 2.0
            m.inputs[2].default_value = -1.0
            nt.links.new(ch, m.inputs[0])
            return m.outputs["Value"]

        sx = signed(sep.outputs["Red"])
        sy = signed(sep.outputs["Green"])
        if not args.no_normal_flip:
            flip = nt.nodes.new("ShaderNodeMath")
            flip.operation = "MULTIPLY"
            flip.inputs[1].default_value = -1.0
            nt.links.new(sy, flip.inputs[0])
            sy = flip.outputs["Value"]

        if args.normal_mode == "bump":
            # Measured on the hull map: R std 0.025, G std 0.042, but B std 0.132.
            # The surface detail lives in B, not in the barely-varying R/G, so
            # treat B as a height field and derive normals from its slope.
            # Rebuilding z from R/G instead produced blotchy "corrugated" plating
            # that the game's clean steel does not have.
            bump = nt.nodes.new("ShaderNodeBump")
            bump.inputs["Strength"].default_value = float(args.normal_strength)
            bump.inputs["Distance"].default_value = float(args.normal_distance)
            nt.links.new(sep.outputs["Blue"], bump.inputs["Height"])
            normal_out = bump.outputs["Normal"]
        else:
            # legacy path: rebuild z = sqrt(1 - x^2 - y^2) from R/G pairs
            def sq(v):
                m = nt.nodes.new("ShaderNodeMath")
                m.operation = "MULTIPLY"
                nt.links.new(v, m.inputs[0])
                nt.links.new(v, m.inputs[1])
                return m.outputs["Value"]

            acc = nt.nodes.new("ShaderNodeMath")
            acc.operation = "ADD"
            nt.links.new(sq(sx), acc.inputs[0])
            nt.links.new(sq(sy), acc.inputs[1])
            inv = nt.nodes.new("ShaderNodeMath")
            inv.operation = "SUBTRACT"
            inv.inputs[0].default_value = 1.0
            nt.links.new(acc.outputs["Value"], inv.inputs[1])
            zc = nt.nodes.new("ShaderNodeMath")
            zc.operation = "SQRT"
            nt.links.new(inv.outputs["Value"], zc.inputs[0])

            # pack back to the 0..1 encoding Blender's Normal Map node expects
            comb = nt.nodes.new("ShaderNodeCombineColor")
            for sock, val in (("Red", sx), ("Green", sy),
                              ("Blue", zc.outputs["Value"])):
                m = nt.nodes.new("ShaderNodeMath")
                m.operation = "MULTIPLY_ADD"
                m.inputs[1].default_value = 0.5
                m.inputs[2].default_value = 0.5
                nt.links.new(val, m.inputs[0])
                nt.links.new(m.outputs["Value"], comb.inputs[sock])

            n_map = nt.nodes.new("ShaderNodeNormalMap")
            n_map.space = "TANGENT"
            n_map.uv_map = "UVMap"
            nt.links.new(comb.outputs["Color"], n_map.inputs["Color"])
            normal_out = n_map.outputs["Normal"]
    else:
        normal_out = None

    # --- procedural stand-in for the game's detailMap (fine rg normal detail).
    # Plate seams / weld lines read at close range, so a small tiling noise
    # driven bump is layered under the baked normal instead of doing nothing.
    if args.detail > 0.0:
        d_uv = nt.nodes.new("ShaderNodeTexCoord")
        d_noise = nt.nodes.new("ShaderNodeTexNoise")
        d_noise.inputs["Scale"].default_value = float(args.detail_scale)
        d_noise.inputs["Detail"].default_value = 4.0
        d_noise.inputs["Roughness"].default_value = 0.55
        nt.links.new(d_uv.outputs["UV"], d_noise.inputs["Vector"])
        d_bump = nt.nodes.new("ShaderNodeBump")
        d_bump.inputs["Strength"].default_value = float(args.detail)
        d_bump.inputs["Distance"].default_value = 0.02
        nt.links.new(d_noise.outputs["Fac"], d_bump.inputs["Height"])
        if normal_out:
            nt.links.new(normal_out, d_bump.inputs["Normal"])
        nt.links.new(d_bump.outputs["Normal"], bsdf.inputs["Normal"])
    elif normal_out:
        nt.links.new(normal_out, bsdf.inputs["Normal"])

    # --- metallic (R) and gloss/roughness (B) from the packed _mg map
    mg_path = base + "_mg.png"
    if os.path.exists(mg_path):
        mg_tex = nt.nodes.new("ShaderNodeTexImage")
        mg_tex.image = load_img(mg_path, "Non-Color")
        mg_tex.interpolation = "Smart"
        nt.links.new(uv.outputs["UV"], mg_tex.inputs["Vector"])
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        nt.links.new(mg_tex.outputs["Color"], sep.inputs["Color"])

        # metallic = R (falls back to the average of R,G which agree closely)
        if not args.no_metallic_map:
            nt.links.new(sep.outputs["Red"], bsdf.inputs["Metallic"])

        # roughness = 1 - gloss
        if GLOSS_FROM_B:
            gloss = sep.outputs["Blue"]
        else:
            gloss = sep.outputs["Green"]
        inv = nt.nodes.new("ShaderNodeMath")
        inv.operation = "SUBTRACT"
        inv.inputs[0].default_value = 1.0
        nt.links.new(gloss, inv.inputs[1])

        # overallWetness (g_pbsExtras): wet paint films over smooth surfaces, so
        # the game reduces roughness by the wetness amount before shading.
        if args.wetness > 0.0:
            wet = nt.nodes.new("ShaderNodeMath")
            wet.operation = "MULTIPLY"
            wet.use_clamp = True
            wet.inputs[1].default_value = 1.0 - float(args.wetness)
            nt.links.new(inv.outputs["Value"], wet.inputs[0])
            rough_src = wet.outputs["Value"]
        else:
            rough_src = inv.outputs["Value"]

        # keep paint from becoming a mirror: clamp the roughest end a little
        remap = nt.nodes.new("ShaderNodeMapRange")
        remap.inputs["From Min"].default_value = 0.0
        remap.inputs["From Max"].default_value = 1.0
        remap.inputs["To Min"].default_value = float(args.roughness_min)
        remap.inputs["To Max"].default_value = float(args.roughness_max)
        nt.links.new(rough_src, remap.inputs["Value"])
        nt.links.new(remap.outputs["Result"], bsdf.inputs["Roughness"])

    # --- specularMult (g_pbsExtras): global scale on specular reflection.
    # Applied as a plain multiplier on the socket's constant value.
    if abs(args.specular_mult - 1.0) > 1e-3:
        for sock in ("Specular IOR Level", "Specular"):
            if sock in bsdf.inputs and not bsdf.inputs[sock].is_linked:
                base_val = bsdf.inputs[sock].default_value
                old = nt.nodes.new("ShaderNodeValue")
                old.outputs[0].default_value = float(base_val)
                s = nt.nodes.new("ShaderNodeMath")
                s.operation = "MULTIPLY"
                s.use_clamp = True
                s.inputs[1].default_value = float(args.specular_mult)
                nt.links.new(old.outputs[0], s.inputs[0])
                nt.links.new(s.outputs["Value"], bsdf.inputs[sock])
                break

    # --- camouflage (ship_camo_material): camoColorMask is NOT a paint image.
    # Channels R/G/B index up to three painted regions; each region is tinted
    # from a palette (camoColor) and blended over the base albedo. Palette
    # values live in GameParams.data, which the exporter does not expose, so the
    # colours come from --camo-colors (default: a neutral naval camo grey set).
    if args.camo and args.camo != "none":
        # textures are stored as <stem>_camo_<NN>.png next to the base maps
        camo_path = os.path.join(TEX_DIR, "%s_camo_%s.png" % (stem, args.camo))
        if not os.path.exists(camo_path):
            hits = sorted(glob.glob(os.path.join(
                TEX_DIR, glob.escape(stem) + "_camo_*.png")))
            camo_path = hits[0] if hits else ""
        if camo_path:
            c_tex = nt.nodes.new("ShaderNodeTexImage")
            c_tex.image = load_img(camo_path, "Non-Color")
            c_tex.interpolation = "Smart"
            c_uv = nt.nodes.new("ShaderNodeVectorMath")
            c_uv.operation = "MULTIPLY"
            c_uv.inputs[1].default_value = (float(args.camo_repeats),
                                            float(args.camo_repeats), 1.0)
            nt.links.new(uv.outputs["UV"], c_uv.inputs[0])
            nt.links.new(c_uv.outputs["Vector"], c_tex.inputs["Vector"])

            base_in = bsdf.inputs["Base Color"]
            under = base_in.links[0].from_socket if base_in.is_linked else None
            cur = under
            # 3 x RGB; overridable because the real palette is in GameParams.data
            flat = args.camo_colors or [0.30, 0.33, 0.36,
                                        0.16, 0.18, 0.20,
                                        0.55, 0.56, 0.58]
            palette = [tuple(flat[i:i + 3]) for i in range(0, 9, 3)]
            for ch, col in zip(("Red", "Green", "Blue"), palette):
                if max(col) <= 0.0:
                    continue
                sep = nt.nodes.new("ShaderNodeSeparateColor")
                nt.links.new(c_tex.outputs["Color"], sep.inputs["Color"])
                mix = nt.nodes.new("ShaderNodeMix")
                mix.data_type = "RGBA"
                mix.blend_type = "MIX"
                mix.inputs["B"].default_value = (col[0], col[1], col[2], 1.0)
                nt.links.new(sep.outputs[ch], mix.inputs["Factor"])
                if cur:
                    nt.links.new(cur, mix.inputs["A"])
                else:
                    mix.inputs["A"].default_value = tuple(base_in.default_value)
                cur = mix.outputs["Result"]
            if cur:
                nt.links.new(cur, base_in)
            print("MAT_INFO:   camo %s on %s" % (os.path.basename(camo_path), stem))

    # --- ambient occlusion
    # The game's AO attenuates INDIRECT light (aoPower in g_pbsExtras); it is not
    # a paint decal. Multiplying it straight into Base Color made the whole hull
    # permanently darker -- measured: albedo brightest 10% is sRGB 178 while the
    # AO map averages 0.715, so the ship lost ~28% brightness everywhere and read
    # dull grey next to the game. Instead we remap AO around 1.0 so only the
    # genuinely occluded creases darken, and keep the plating near its albedo.
    ao_path = base + "_ao.png"
    if os.path.exists(ao_path) and args.ao_strength > 0.0:
        ao_tex = nt.nodes.new("ShaderNodeTexImage")
        ao_tex.image = load_img(ao_path, "Non-Color")
        ao_tex.interpolation = "Smart"
        nt.links.new(uv.outputs["UV"], ao_tex.inputs["Vector"])

        # Remap AO around a neutral pivot: values at/below `pivot` map to
        # [1-strength, 1], values above lift toward 1+brighten. This keeps the
        # average occlusion from dimming the whole hull.
        pivot = max(0.05, min(float(args.ao_pivot), 0.98))
        lo = nt.nodes.new("ShaderNodeMapRange")
        lo.clamp = True
        lo.inputs["From Min"].default_value = 0.0
        lo.inputs["From Max"].default_value = pivot
        lo.inputs["To Min"].default_value = 1.0 - float(args.ao_strength)
        lo.inputs["To Max"].default_value = 1.0
        nt.links.new(ao_tex.outputs["Color"], lo.inputs["Value"])

        hi = nt.nodes.new("ShaderNodeMapRange")
        hi.clamp = True
        hi.inputs["From Min"].default_value = pivot
        hi.inputs["From Max"].default_value = 1.0
        hi.inputs["To Min"].default_value = 1.0
        hi.inputs["To Max"].default_value = 1.0 + float(args.ao_brighten)
        nt.links.new(ao_tex.outputs["Color"], hi.inputs["Value"])

        # pick the side with a simple AO<pivot test
        lt = nt.nodes.new("ShaderNodeMath")
        lt.operation = "LESS_THAN"
        lt.inputs[1].default_value = pivot
        nt.links.new(ao_tex.outputs["Color"], lt.inputs[0])

        pick = nt.nodes.new("ShaderNodeMix")
        pick.data_type = "FLOAT"
        nt.links.new(lt.outputs["Value"], pick.inputs["Factor"])
        nt.links.new(lo.outputs["Result"], pick.inputs["A"])
        nt.links.new(hi.outputs["Result"], pick.inputs["B"])
        ao_value = pick.outputs["Result"]

        base_in = bsdf.inputs["Base Color"]
        src = base_in.links[0].from_socket if base_in.is_linked else None
        ao_mix = nt.nodes.new("ShaderNodeMix")
        ao_mix.data_type = "RGBA"
        ao_mix.blend_type = "MULTIPLY"
        ao_mix.inputs["Factor"].default_value = 1.0
        if src:
            nt.links.new(src, ao_mix.inputs["A"])
        else:
            ao_mix.inputs["A"].default_value = tuple(base_in.default_value)
        nt.links.new(ao_value, ao_mix.inputs["B"])
        nt.links.new(ao_mix.outputs["Result"], base_in)
        # note the pivot in the log so tuning stays traceable
        print("MAT_INFO:   AO pivot=%.2f strength=%.2f"
              % (pivot, args.ao_strength))
    return True


applied = {"pbs": 0, "fallback": 0}
for mat in list(bpy.data.materials):
    if not mat.use_nodes:
        continue
    stem = find_part_textures(mat.name)
    if stem:
        if build_pbs_material(mat, stem):
            applied["pbs"] += 1
            print("MAT_INFO: %-38s PBS maps from %s" % (mat.name[:38], stem))
            continue
    applied["fallback"] += 1
print("MAT_INFO: applied", applied)
if TEX_DIR and not applied["pbs"]:
    print("MAT_WARN: no PBS textures matched in %s" % TEX_DIR)

# Railings are modelled as thin wires with a flat grey material and no maps of
# their own; keep them as a dark, slightly glossy metal so they read as steel.
grey = bpy.data.materials.get("__default_grey")
if grey and grey.use_nodes:
    gb = next((n for n in grey.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if gb:
        gb.inputs["Base Color"].default_value = (0.055, 0.058, 0.062, 1.0)
        gb.inputs["Metallic"].default_value = min(1.0, 0.85 * args.metal)
        gb.inputs["Roughness"].default_value = max(0.02, 0.30 * args.roughness)
        print("MAT_INFO: __default_grey -> dark metal (rails/rigging)")

# --------------------------------------------------------------- bounds
mins = Vector((1e18,) * 3)
maxs = Vector((-1e18,) * 3)
for ob in meshes:
    for corner in ob.bound_box:
        w = ob.matrix_world @ Vector(corner)
        for i in range(3):
            mins[i] = min(mins[i], w[i])
            maxs[i] = max(maxs[i], w[i])
center = (mins + maxs) / 2.0
size = maxs - mins
length = max(size.x, size.y, size.z)
half_beam = min(size.x, size.y) / 2.0
print("BBOX size=%s length=%.2f" % (tuple(round(v, 2) for v in size), length))

# waterline ~ the bottom of the hull. The exporter puts origin near waterline,
# so use min Z as keel and lift slightly.
water_z = mins.z

# --------------------------------------------------------------- world / sky
world = bpy.data.worlds.new("World")
scene.world = world
world.use_nodes = True
wnt = world.node_tree
wnt.nodes.clear()

sky = wnt.nodes.new("ShaderNodeTexSky")
sky.sky_type = "NISHITA"
sky.sun_elevation = math.radians(SUN_EL)
sky.sun_rotation = math.radians(SUN_AZ)
sky.altitude = 100.0
sky.air_density = 1.0
sky.dust_density = 1.5
sky.ozone_density = 1.0

bg = wnt.nodes.new("ShaderNodeBackground")
# Environment strength stands in for the game's PMREM probe. With the fill lamp
# gone, this is what keeps the shadow side readable (the game also scales ship
# indirect light separately via indirectMultShips).
bg.inputs["Strength"].default_value = SKY_S * args.env_boost
wout = wnt.nodes.new("ShaderNodeOutputWorld")
ramp = wnt.nodes.new("ShaderNodeValToRGB")
ramp.color_ramp.elements[0].position = 0.0
ramp.color_ramp.elements[0].color = (0.13, 0.26, 0.46, 1.0)
ramp.color_ramp.elements[1].position = 0.55
ramp.color_ramp.elements[1].color = (0.55, 0.74, 0.96, 1.0)
sep = wnt.nodes.new("ShaderNodeSeparateColor")
wnt.links.new(sky.outputs["Color"], sep.inputs["Color"])
wnt.links.new(sep.outputs["Red"], ramp.inputs["Fac"])
wnt.links.new(ramp.outputs["Color"], bg.inputs["Color"])
wnt.links.new(bg.outputs["Background"], wout.inputs["Surface"])

# --------------------------------------------------------------- sun
sun_data = bpy.data.lights.new("Sun", type="SUN")
sun_data.energy = SUN_E * args.sun_boost
sun_data.angle = math.radians(0.8)
sun_data.color = (1.0, 0.95, 0.87)
sun = bpy.data.objects.new("Sun", sun_data)
scene.collection.objects.link(sun)
# aim the sun: elevation from horizon
sun_el = math.radians(SUN_EL)
sun_az = math.radians(SUN_AZ)


def aim_sun(cam_azimuth_deg, offset_deg=-52.0):
    """Place the sun off to one side of the camera so visible faces are lit."""
    el = math.radians(SUN_EL)
    az = math.radians(cam_azimuth_deg + offset_deg)
    sun.rotation_euler = Euler((math.pi / 2 - el, 0.0, az), "XYZ")


aim_sun(SUN_AZ)

# --------------------------------------------------------------- ocean
water_mat = bpy.data.materials.new("Ocean")
water_mat.use_nodes = True
nt = water_mat.node_tree
nt.nodes.clear()

bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
bsdf.inputs["Base Color"].default_value = (0.008, 0.032, 0.058, 1.0)
bsdf.inputs["Roughness"].default_value = 0.06
bsdf.inputs["IOR"].default_value = 1.333
if "Metallic" in bsdf.inputs:
    bsdf.inputs["Metallic"].default_value = 0.0

texco = nt.nodes.new("ShaderNodeTexCoord")
# large-scale swell
n1 = nt.nodes.new("ShaderNodeTexNoise")
n1.inputs["Scale"].default_value = 3.0
n1.inputs["Detail"].default_value = 8.0
n1.inputs["Roughness"].default_value = 0.6
# fine chop
n2 = nt.nodes.new("ShaderNodeTexNoise")
n2.inputs["Scale"].default_value = 55.0
n2.inputs["Detail"].default_value = 6.0
n2.inputs["Roughness"].default_value = 0.5

mix = nt.nodes.new("ShaderNodeMixRGB")
mix.blend_type = "ADD"
mix.inputs["Fac"].default_value = 0.35

bump = nt.nodes.new("ShaderNodeBump")
bump.inputs["Strength"].default_value = 0.22
bump.inputs["Distance"].default_value = 0.05

out_n = nt.nodes.new("ShaderNodeOutputMaterial")
nt.links.new(texco.outputs["Object"], n1.inputs["Vector"])
nt.links.new(texco.outputs["Object"], n2.inputs["Vector"])
nt.links.new(n1.outputs["Fac"], mix.inputs["Color1"])
nt.links.new(n2.outputs["Fac"], mix.inputs["Color2"])
nt.links.new(mix.outputs["Color"], bump.inputs["Height"])
nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
nt.links.new(bsdf.outputs["BSDF"], out_n.inputs["Surface"])

# ocean sized so its edge is far beyond the horizon in frame
plane_size = length * 4000.0
bpy.ops.mesh.primitive_plane_add(size=plane_size,
                                 location=(center.x, center.y, water_z + 0.02))
ocean = bpy.context.active_object
ocean.name = "Ocean"
ocean.data.materials.append(water_mat)

# --------------------------------------------------------------- camera
cam_data = bpy.data.cameras.new("Cam")
cam_data.lens = 70.0
cam_data.sensor_width = 36.0
cam_data.clip_start = 0.5
cam_data.clip_end = plane_size * 2
cam = bpy.data.objects.new("Cam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam

target = bpy.data.objects.new("Target", None)
# aim a little above the waterline, at the ship's visual centroid
target.location = (center.x, center.y, water_z + size.z * 0.55)
scene.collection.objects.link(target)

con = cam.constraints.new("TRACK_TO")
con.target = target
con.track_axis = "TRACK_NEGATIVE_Z"
con.up_axis = "UP_Y"


def place_camera(azimuth_deg, elevation_deg, distance):
    az = math.radians(azimuth_deg)
    el = math.radians(elevation_deg)
    cam.location = (
        center.x + distance * math.cos(el) * math.sin(az),
        center.y - distance * math.cos(el) * math.cos(az),
        target.location.z + distance * math.sin(el),
    )
    bpy.context.view_layer.update()


# --------------------------------------------------------------- render cfg
scene.render.resolution_x = RES_X
scene.render.resolution_y = RES_Y
scene.render.resolution_percentage = 100
scene.render.film_transparent = args.transparent
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA" if args.transparent else "RGB"
scene.render.image_settings.compression = 15

scene.render.engine = "CYCLES"
cycles = scene.cycles
cycles.samples = args.samples
cycles.use_denoising = True
cycles.max_bounces = 6
cycles.diffuse_bounces = 3
cycles.glossy_bounces = 3
cycles.transmission_bounces = 2
cycles.transparent_max_bounces = 4
cycles.use_adaptive_sampling = True
cycles.adaptive_threshold = 0.02
cycles.device = "CPU"

# --- atmospheric haze via Mist pass + compositor mix ---
# The mist starts well behind the ship (start=length*0.8 puts the ship almost
# entirely in front of it) and is capped low: haze in the game tints distant
# objects only. A heavy mix here desaturated the hull -- measured saturation
# 0.097 vs the game's 0.134 -- washing the paint toward flat grey.
scene.use_nodes = True
scene.view_layers[0].use_pass_mist = True
scene.world.mist_settings.start = length * 1.6
scene.world.mist_settings.depth = length * 22.0
scene.world.mist_settings.falloff = "QUADRATIC"

cn = scene.node_tree
cn.nodes.clear()
rl = cn.nodes.new("CompositorNodeRLayers")
haze = cn.nodes.new("CompositorNodeMath")
haze.operation = "MULTIPLY"
haze.inputs[1].default_value = float(args.haze_strength)
mixc = cn.nodes.new("CompositorNodeMixRGB")
mixc.blend_type = "MIX"
mixc.inputs[2].default_value = (0.58, 0.70, 0.85, 1.0)
comp = cn.nodes.new("CompositorNodeComposite")
cn.links.new(rl.outputs["Image"], mixc.inputs[1])
cn.links.new(rl.outputs["Mist"], haze.inputs[0])
cn.links.new(haze.outputs["Value"], mixc.inputs[0])


def gt_tonemap_curve(black, linear_start, linear_len, contrast):
    """Points approximating the game's GT (Guerrilla Tonemap) curve.

    The shader exposes g_tonemapParams as
        exposure / gtContrast / gtLinearSectionStart / gtLinearSectionLength / gtBlack
    i.e. a black point, a straight section that preserves midtone contrast, and
    a shoulder that rolls highlights off.

    IMPORTANT -- measured behaviour: in this compositor the CurveRGB node runs
    on the ALREADY DISPLAY-MAPPED values (an identity curve reproduces the
    no-curve render exactly: both 0.9373 on a 0.87-linear emitter). So this is a
    contrast curve applied in display space, NOT a scene-referred tonemap.
    Display values here already sit high (the sky is ~0.91 after AgX), so the
    gain may only be applied in the lower half. Above the pivot the curve is
    pulled back below the identity so highlights keep their gradient instead of
    being driven past 1.0 by the contrast boost.
    """
    pts = []
    contrast = max(0.6, min(contrast, 1.4))
    ls = max(0.01, min(linear_start, 0.4))
    # toe: identity up to the linear start
    pts.append((0.0, 0.0))
    pts.append((ls, ls))
    # gain only around the midtones, capped strictly below the input
    mid = min(0.55, ls + max(0.05, linear_len * 0.5))
    pts.append((mid, min(0.995, mid * contrast)))
    # from here on the curve must stay UNDER the identity (y < x) so the bright
    # sky is darkened rather than clipped
    hi = 0.85
    pts.append((hi, hi * 0.97))
    pts.append((1.0, 1.0))
    clean = []
    for (x, y) in sorted(pts):
        if clean and x <= clean[-1][0] + 1e-4:
            continue
        clean.append((x, max(0.0, min(1.0, y))))
    if clean[0][0] != 0.0:
        clean.insert(0, (0.0, 0.0))
    if clean[-1][0] != 1.0:
        clean.append((1.0, 1.0))
    return clean


if args.gt_tonemap:
    pts = gt_tonemap_curve(float(args.gt_black),
                           float(args.gt_linear_start),
                           float(args.gt_linear_len),
                           float(args.gt_contrast))

    def make_gt_node():
        n = cn.nodes.new("CompositorNodeCurveRGB")
        n.mapping.initialize()
        c = n.mapping.curves[3]           # 3 == combined RGB (C)
        # Blender keeps two mandatory endpoints (0,0) and (1,1). Removing only
        # the interior points leaves exactly those; re-adding (0,0)/(1,1) would
        # duplicate them and make the curve evaluate wrongly (measured: the sky
        # was pushed to pure white).
        for p in list(c.points)[1:-1]:
            c.points.remove(p)
        for (x, y) in pts:
            if x <= 1e-4 or x >= 1.0 - 1e-4:
                continue                   # endpoints already exist
            c.points.new(x, y)
        n.mapping.update()
        return n

    # Curve runs on the HAZE-COMPOSITED frame. Note the mist colour is a
    # display-ish pastel (0.58,0.70,0.85) that a MIX with weight up to 0.92
    # pulls the whole sky towards; the curve's job is to keep that from banking
    # up at white, so the shoulder must sit below 1.0.
    gt_img = make_gt_node()
    cn.links.new(mixc.outputs[0], gt_img.inputs["Image"])
    cn.links.new(gt_img.outputs["Image"], comp.inputs["Image"])
    print("TONEMAP_INFO: GT curve black=%.3f lin=%.3f+%.3f contrast=%.2f "
          "(applied after haze)"
          % (args.gt_black, args.gt_linear_start, args.gt_linear_len,
             args.gt_contrast))
else:
    cn.links.new(mixc.outputs[0], comp.inputs["Image"])

if args.engine == "gpu":
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "OPTIX"
        prefs.get_devices()
        found = False
        for dev in prefs.devices:
            if "NVIDIA" in dev.name.upper():
                dev.use = True
                found = True
            elif dev.type == "CPU":
                dev.use = False
        if found:
            cycles.device = "GPU"
            print("RENDER_INFO: OptiX GPU")
    except Exception as e:
        print("RENDER_INFO: GPU failed (%s)" % e)

try:
    # The GT curve node runs AFTER the view transform in display space (measured),
    # so AgX stays in charge of the HDR rolloff and the curve only adds the
    # game's black-point / contrast / shoulder shape on top of it.
    scene.view_settings.view_transform = args.view_transform
    if args.view_transform == "AgX":
        scene.view_settings.look = "AgX - Medium High Contrast"
    else:
        scene.view_settings.look = "None"
except Exception:
    pass
_pbs_expo = PBS_EXPOSURE if TEX_DIR else 0.0
scene.view_settings.exposure = EXPO + args.exposure + _pbs_expo

# --------------------------------------------------------------- fill light
# The game lights ships with a single directional sun (g_sunLight) plus a PMREM
# environment probe -- there is no fill lamp. Default to that arrangement:
# no fill, and the environment (sky + ocean bounce) carries the shadow side.
# --fill-light restores the old area lamp for a brighter but less game-accurate
# look.
if args.fill_light:
    fill_data = bpy.data.lights.new("Fill", type="AREA")
    bpy.context.view_layer.update()
    fill_data.energy = 1500.0 * (length / 100.0) ** 2 * FILL_S
    fill_data.size = length * 2.0
    fill_data.color = (0.72, 0.83, 1.0)
    fill = bpy.data.objects.new("Fill", fill_data)
    fill.location = (center.x - length * 1.4, center.y + length * 0.6, target.location.z + length * 0.55)
    scene.collection.objects.link(fill)
    fcon = fill.constraints.new("TRACK_TO")
    fcon.target = target
    fcon.track_axis = "TRACK_NEGATIVE_Z"
    fcon.up_axis = "UP_Y"
    print("LIGHT_INFO: sun + fill light (non-game look)")
else:
    print("LIGHT_INFO: sun-only + environment (matches the game)")

# --------------------------------------------------------------- views
# azimuth, elevation, distance multiplier (rel. to ship length), focal length
VIEW_PRESETS = {
    # classic three-quarter bow, slightly above deck
    "hero":   (38.0, 8.0, 0.72, 100.0),
    # broadside
    "side":   (90.0, 3.0, 0.74, 110.0),
    # three-quarter stern
    "stern":  (215.0, 10.0, 0.72, 100.0),
    # bow-on, low and dramatic
    "bow":    (0.0, 4.0, 0.66, 100.0),
    # high three-quarter showing deck plan
    "top":    (40.0, 40.0, 0.60, 85.0),
    # very low dramatic angle
    "low":    (55.0, 1.0, 0.70, 90.0),
}

views = [v.strip() for v in args.views.split(",") if v.strip()]
outdir = os.path.dirname(os.path.abspath(args.out))
os.makedirs(outdir, exist_ok=True)
base = os.path.splitext(os.path.basename(args.out))[0]

for vname in views:
    az, el, dm, focal = VIEW_PRESETS.get(vname, VIEW_PRESETS["hero"])
    cam_data.lens = focal
    aim_sun(az)
    dist = length * dm
    place_camera(az, el, dist)

    # Fit the camera by binary-searching distance until every vertex of the
    # world-space hull projects inside the frame. Using the real vertices (not
    # object.bound_box, which is local and ignores parenting) is what makes this
    # reliable across ships.
    def projected_fit(d):
        place_camera(az, el, d)
        minx = miny = 1e9
        maxx = maxy = -1e9
        for ob in meshes:
            mw = ob.matrix_world
            verts = ob.data.vertices
            n = len(verts)
            # sample at most ~400 verts per object: plenty for a bbox fit
            step = max(1, n // 400)
            for i in range(0, n, step):
                w = mw @ verts[i].co
                v = world_to_camera_view(scene, cam, w)
                if v.z <= 0:
                    return None
                if v.x < minx: minx = v.x
                if v.x > maxx: maxx = v.x
                if v.y < miny: miny = v.y
                if v.y > maxy: maxy = v.y
        return (minx, maxx, miny, maxy)

    MARGIN = 0.055
    lo, hi = length * 0.40, length * 8.0
    # start from the preset, then search outward until it fits with margin
    dist = max(length * dm, length * 0.4)
    for _ in range(30):
        res = projected_fit(dist)
        if res is None:
            dist *= 1.3
            continue
        minx, maxx, miny, maxy = res
        if (minx >= MARGIN and maxx <= 1.0 - MARGIN
                and miny >= MARGIN and maxy <= 1.0 - MARGIN):
            break
        dist *= 1.06
    place_camera(az, el, dist)

    # --- auto exposure, mirroring the game's wg_average_luminance pass.
    # The game meters the frame, derives an exposure value and applies it to the
    # next frame; we do the same per view with a cheap low-sample probe render,
    # so a bright sky view and a dark low-angle view do not drift apart.
    if args.auto_exposure:
        probe_w, probe_h = scene.render.resolution_x, scene.render.resolution_y
        scene.render.resolution_x = max(64, RES_X // 16)
        scene.render.resolution_y = max(64, RES_Y // 16)
        scene.cycles.samples = max(1, min(4, args.samples // 8 or 1))
        probe_file = os.path.join(outdir, "_probe_lum.exr")
        scene.render.image_settings.file_format = "OPEN_EXR"
        scene.render.filepath = probe_file
        bpy.ops.render.render(write_still=True)

        try:
            img = bpy.data.images.load(probe_file)
            px = list(img.pixels)
            n = len(px) // 4
            step = max(1, n // 4000)
            lum = []
            for i in range(0, n, step):
                r, g, b = px[i * 4], px[i * 4 + 1], px[i * 4 + 2]
                lum.append(0.2126 * r + 0.7152 * g + 0.0722 * b)
            lum.sort()
            # use the median rather than the mean: a large bright sky in one
            # view would otherwise drag every view down
            key = lum[len(lum) // 2] if lum else 0.0
            # GT-style key: target mid-grey, i.e. exposure shift in stops
            if key > 1e-4:
                shift = math.log2(args.auto_key / key)
                shift = max(-args.auto_max, min(args.auto_max, shift))
            else:
                shift = 0.0
            scene.view_settings.exposure += shift * args.auto_strength
            print("EXPO_INFO: %s key=%.4f shift=%+.2f stops"
                  % (vname, key, shift * args.auto_strength))
            bpy.data.images.remove(img)
        finally:
            try:
                os.remove(probe_file)
            except OSError:
                pass
            scene.render.resolution_x = probe_w
            scene.render.resolution_y = probe_h
            scene.cycles.samples = args.samples
            scene.render.image_settings.file_format = "PNG"

    # Name the output after the view plus the resolution, so a multi-view run
    # yields <dir>/<view>_<W>x<H>.png and renders at different resolutions can
    # sit side by side without overwriting each other.
    outpath = os.path.join(outdir, "%s_%dx%d.png" % (vname, RES_X, RES_Y))
    scene.render.filepath = outpath
    print("RENDER_INFO: rendering %s (dist=%.1f)" % (vname, dist))
    bpy.ops.render.render(write_still=True)
    print("RENDER_INFO: wrote %s" % outpath)

print("RENDER_OK")
