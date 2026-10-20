"""
WoWs ship GLB -> photorealistic render (Blender 4.5 / Cycles GPU).

Usage:
  blender -b --factory-startup -P render_ship.py -- --glb <in.glb> --out <out.png>
          [--res 3840x2160] [--samples 128] [--views hero,side,bow]
          [--engine gpu|cpu] [--transparent] [--preset day|sunset|blue]

Design notes:
  * WoWs geometry is Z-up in the game; Blender's glTF importer applies the
    Y-up conversion, so after import the long horizontal axis is Y.
  * Auto-frames the camera from the real bounding box so it works on any ship.
  * Needs no external assets: procedural sky + ocean shader.
"""

import bpy
import sys
import os
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
ap.add_argument("--exposure", type=float, default=0.0)
args = ap.parse_args(argv)

RES_X, RES_Y = [int(v) for v in args.res.lower().split("x")]

# --------------------------------------------------------------- light presets
# (sun_elevation_deg, sun_azimuth_deg, sun_energy, sky_strength, fill_energy_scale, exposure)
PRESETS = {
    "day":    (30.0, 125.0, 4.2, 1.15, 0.45, 0.15),   # high sun, clean blue
    "sunset": (8.0,  100.0, 2.2, 0.28, 0.12, 0.15),  # low warm sun
    "blue":   (55.0, 150.0, 1.6, 0.28, 0.30, -0.1),  # midday studio
    "storm":  (20.0, 110.0, 0.6, 0.35, 0.15, 0.1),   # overcast flat
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
sky.dust_density = 3.0 if args.preset == "sunset" else 1.5
sky.ozone_density = 1.0

bg = wnt.nodes.new("ShaderNodeBackground")
bg.inputs["Strength"].default_value = SKY_S
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
sun_data.energy = SUN_E
sun_data.angle = math.radians(0.8)
sun_data.color = (1.0, 0.93, 0.82) if args.preset == "sunset" else (1.0, 0.95, 0.87)
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
scene.use_nodes = True
scene.view_layers[0].use_pass_mist = True
scene.world.mist_settings.start = length * 0.8
scene.world.mist_settings.depth = length * 18.0
scene.world.mist_settings.falloff = "QUADRATIC"

cn = scene.node_tree
cn.nodes.clear()
rl = cn.nodes.new("CompositorNodeRLayers")
haze = cn.nodes.new("CompositorNodeMath")
haze.operation = "MULTIPLY"
haze.inputs[1].default_value = 0.92
mixc = cn.nodes.new("CompositorNodeMixRGB")
mixc.blend_type = "MIX"
mixc.inputs[2].default_value = (0.58, 0.70, 0.85, 1.0)
comp = cn.nodes.new("CompositorNodeComposite")
cn.links.new(rl.outputs["Image"], mixc.inputs[1])
cn.links.new(rl.outputs["Mist"], haze.inputs[0])
cn.links.new(haze.outputs["Value"], mixc.inputs[0])
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
    scene.view_settings.view_transform = "AgX"
    scene.view_settings.look = "AgX - Medium High Contrast"
except Exception:
    pass
scene.view_settings.exposure = EXPO + args.exposure

# --------------------------------------------------------------- fill light
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

    # Name the output after the view so a multi-view run yields
    # <dir>/<view>.png files side by side (the web UI expects exactly that).
    outpath = os.path.join(outdir, "%s.png" % vname)
    scene.render.filepath = outpath
    print("RENDER_INFO: rendering %s (dist=%.1f)" % (vname, dist))
    bpy.ops.render.render(write_still=True)
    print("RENDER_INFO: wrote %s" % outpath)

print("RENDER_OK")
