"""Procedural Clay - showcase renders for the README / Reddit post.

Run with the add-on installed and enabled:

    blender -b --python showcase.py -- ~/Downloads/clay_showcase

Writes into the output folder:
    presets.png            4 Suzannes, one per preset
    fingerprints.png       same Suzanne, Fingerprints 0 vs 0.8, close up
    boil/0001.png ...      stop-motion boil on a still Suzanne (held poses only)
"""

import math
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = os.path.abspath(os.path.expanduser(argv[0] if argv else "~/Downloads/clay_showcase"))
FAST = "--fast" in argv          # quick low-res pass to check framing
os.makedirs(OUT, exist_ok=True)


def addon():
    """The add-on module, whether installed as a legacy add-on or an extension."""
    for name in list(sys.modules):
        if name.endswith("procedural_clay") and hasattr(sys.modules[name], "find_clay_node"):
            return sys.modules[name]
    import addon_utils
    addon_utils.enable("procedural_clay", default_set=True)
    return sys.modules["procedural_clay"]


pc = addon()


def new_scene():
    bpy.ops.wm.read_homefile(use_empty=True)
    s = bpy.context.scene
    s.render.resolution_x, s.render.resolution_y = (960, 540) if FAST else (1920, 1080)
    s.render.image_settings.file_format = "PNG"
    return s


def suzanne(loc, rot_z=0.0):
    bpy.ops.mesh.primitive_monkey_add(location=loc, rotation=(0, 0, rot_z))
    o = bpy.context.object
    sub = o.modifiers.new("Subdivision", "SUBSURF")
    sub.levels = sub.render_levels = 2
    bpy.ops.object.shade_smooth()
    return o


def clay(obj, color, preset, deform=0.3, **inputs):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.pclay.apply(color=color, intensity=deform, shared=False)
    bpy.ops.pclay.preset(preset=preset)
    node = pc.find_clay_node(obj.active_material)
    for k, v in inputs.items():
        node.inputs[k].default_value = v
    return node


def studio(objs, samples):
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    s = bpy.context.scene
    s.pclay_quality = "FINAL"
    bpy.ops.pclay.studio_setup(dof=True)
    for look in ("AgX - Punchy", "Punchy"):
        try:
            s.view_settings.look = look
            break
        except TypeError:
            pass
    s.eevee.taa_render_samples = 8 if FAST else samples
    return s


def look_at(cam, target, offset):
    cam.location = Vector(target) + Vector(offset)
    cam.rotation_euler = (Vector(target) - cam.location).to_track_quat("-Z", "Y").to_euler()
    cam.data.dof.focus_distance = (cam.location - Vector(target)).length


def render(path):
    bpy.context.scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    print("SHOWCASE saved", path)


# 1) presets row ------------------------------------------------------------
s = new_scene()
row = [
    ((0.95, 0.25, 0.42, 1), "PLAYDOH"),
    ((0.30, 0.70, 0.22, 1), "SMOOTH"),
    ((0.18, 0.35, 0.85, 1), "PLASTICINE"),
    ((0.70, 0.28, 0.12, 1), "TERRACOTTA"),
]
objs = []
for i, (col, preset) in enumerate(row):
    o = suzanne((i * 2.7 - 4.05, 0, 0), rot_z=math.radians(15 - i * 10))
    clay(o, col, preset)
    objs.append(o)
s = studio(objs, 48)
s.camera.data.lens = 50
look_at(s.camera, (0, 0, 0.05), (0, -16, 2.4))
render(os.path.join(OUT, "presets.png"))

# 2) fingerprints before / after ------------------------------------------
s = new_scene()
a = suzanne((-1.35, 0, 0), rot_z=math.radians(20))
b = suzanne((1.35, 0, 0), rot_z=math.radians(-20))
for o, fp in ((a, 0.0), (b, 0.8)):
    clay(o, (0.92, 0.66, 0.30, 1), "PLAYDOH", deform=0.2, Fingerprints=fp, Roughness=0.4)
studio([a, b], 48)
look_at(s.camera, (0, 0, 0.1), (0.4, -12.5, 1.5))
render(os.path.join(OUT, "fingerprints.png"))

# 3) stop-motion clip: Suzanne still and facing the camera; only the boil moves
s = new_scene()
o = suzanne((0, 0, 0))
clay(o, (0.95, 0.55, 0.25, 1), "PLAYDOH", deform=0.2, Fingerprints=0.2)
studio([o], 32)
look_at(s.camera, (0, 0, 0.05), (0, -12.5, 1.0))
s.frame_start, s.frame_end = 1, 36
s.pclay_stop.enabled = True
s.pclay_stop.step = 3
s.pclay_stop.boil = 0.3
# only the held poses need rendering (every 3rd frame); the video repeats them
s.frame_step = 3
s.render.filepath = os.path.join(OUT, "boil", "")
bpy.ops.render.render(animation=True)
print("SHOWCASE done", OUT)
