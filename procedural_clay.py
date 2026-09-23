bl_info = {
    "name": "Procedural Clay",
    "author": "Ignacio Soler",
    "version": (2, 7, 1),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Clay",
    "description": "UV-free procedural plasticine: material + silhouette deformation, tuned for EEVEE",
    "category": "Material",
}

"""
Procedural Clay (v2)
--------------------
Two independent parts, both UV-free and driven by object-space position:

1. MATERIAL  (node group "PC_ClayShader", one group node per material)
   Object coords -> seed offset -> light domain warp
     Lumps  : Noise 3D (low freq)   -> Bump (Imperfection)
     Pores  : Voronoi 3D F1         -> Bump (Grain Intensity)
     Grain  : Noise 3D (high freq)  -> Bump (Grain Intensity)
   The same height layers also darken the base color ("Cavity"). This is
   what makes the detail read in EEVEE: a bump only changes shading when a
   light hits it at an angle, and EEVEE has no per-pixel micro-shadowing,
   so detail that lives only in the normal flattens out. Detail baked into
   the albedo survives any lighting.
   No subsurface: EEVEE computes it as a screen-space blur of the diffuse
   light, which smears away the very grain we're adding.
   3D textures instead of 4D: 4D Voronoi evaluates 81 cells instead of 27,
   so the shader compiles and runs noticeably faster in EEVEE.

2. DEFORMATION  (Geometry Nodes modifier "Clay Deform", group "PC_ClayDeform")
   A bump can never change the silhouette - the outline of a sphere stays a
   perfect circle. The modifier subdivides the mesh and pushes vertices
   along their normal with low-frequency noise, so the outline actually
   becomes lumpy. Offsets are relative to the object's size, so the same
   Intensity looks similar on a pebble or a boulder. Real geometry, so it
   works identically in EEVEE, Cycles and Solid view.
"""

import os
import random

import bpy
from bpy.props import (BoolProperty, FloatProperty, FloatVectorProperty, IntProperty,
                       PointerProperty)
from bpy.types import Operator, Panel, PropertyGroup

SHADER_GROUP = "PC_ClayShader"
SHADER_VERSION = 9
DEFORM_GROUP = "PC_ClayDeform"
DEFORM_VERSION = 5
NODE_NAME = "Clay Controls"
MOD_NAME = "Clay Deform"
MAT_TAG = "procedural_clay"
FP_NODE = "PC Fingerprint Map"
FP_IMAGE = "PC_Fingerprints"
FP_CACHE = "fingerprints_v3.png"
FP_RES = 2048

# (name, socket type, default, min, max, factor subtype, tooltip)
SHADER_INPUTS = [
    ("Clay Color", "NodeSocketColor", (0.62, 0.36, 0.25, 1.0), None, None, False,
     "Base color of the clay"),
    ("Roughness", "NodeSocketFloat", 0.5, 0.0, 1.0, True,
     "Overall surface roughness. Play-Doh sits around 0.45-0.55"),
    ("Subsurface", "NodeSocketFloat", 0.3, 0.0, 1.0, True,
     "Light bleeding through the clay: softens shading and glows at thin parts. "
     "Scaled to each object's size"),
    ("Grain Intensity", "NodeSocketFloat", 0.35, 0.0, 1.0, True,
     "Strength of the fine grain and pores"),
    ("Imperfection", "NodeSocketFloat", 0.45, 0.0, 1.0, True,
     "Strength of the soft, hand-pressed dents on the surface"),
    ("Cavity", "NodeSocketFloat", 0.5, 0.0, 1.0, True,
     "Darkens dents, pores and grain in the color itself, so detail stays "
     "visible under flat lighting (important in EEVEE)"),
    ("Imperfection Scale", "NodeSocketFloat", 2.5, 0.01, 100.0, False,
     "Frequency of the dents (higher = smaller)"),
    ("Grain Scale", "NodeSocketFloat", 90.0, 1.0, 2000.0, False,
     "Frequency of the grain and pores"),
    ("Pores", "NodeSocketFloat", 0.35, 0.0, 1.0, True,
     "Size of the small pits in the surface"),
    ("Color Variation", "NodeSocketFloat", 0.4, 0.0, 1.0, True,
     "How much the dents tint the base color"),
    ("Specks", "NodeSocketFloat", 0.25, 0.0, 1.0, True,
     "Tiny light flecks, like dust or bits of other clay pressed in"),
    ("Fingerprints", "NodeSocketFloat", 0.3, 0.0, 1.0, True,
     "Overlapping finger smudges with ridge lines, like handled clay. Shows mostly "
     "in the highlights"),
    ("Print Size", "NodeSocketFloat", 0.12, 0.02, 0.5, False,
     "Size of the finger prints relative to the object's size"),
    ("Texture Scale", "NodeSocketFloat", 1.0, 0.001, 100.0, False,
     "Global scale of all patterns. Raise it for large objects"),
    ("Seed", "NodeSocketFloat", 0.0, 0.0, 10000.0, False,
     "Changes the pattern so copies don't look identical"),
    # Driven by the Stop Motion settings (drivers added by the add-on)
    ("Boil Frame", "NodeSocketFloat", 0.0, 0.0, 1000000.0, False,
     "Stop-motion step index. Driven automatically"),
    ("Boil Amount", "NodeSocketFloat", 0.0, 0.0, 1.0, True,
     "Stop-motion surface jitter. Driven automatically"),
]

DEFORM_INPUTS = [
    ("Intensity", "NodeSocketFloat", 0.35, 0.0, 1.0, True,
     "How lumpy the silhouette gets. 0 keeps the original shape"),
    ("Lump Size", "NodeSocketFloat", 0.15, 0.03, 1.0, False,
     "Size of the deformation bumps relative to the object"),
    ("Detail", "NodeSocketFloat", 0.2, 0.0, 1.0, True,
     "Adds smaller dents on top of the big lumps"),
    ("Subdivisions", "NodeSocketInt", 3, 0, 5, False,
     "Maximum extra subdivision levels. The modifier adds only what the mesh needs "
     "(none on dense meshes). Each level x4 faces"),
    ("Seed", "NodeSocketFloat", 0.0, 0.0, 10000.0, False,
     "Changes the deformation pattern"),
    ("Boil", "NodeSocketFloat", 0.0, 0.0, 1.0, True,
     "Stop-motion jitter of the lumps. Set from the Stop Motion panel"),
    ("Boil Step", "NodeSocketInt", 2, 1, 12, False,
     "Frames each stop-motion pose is held"),
]


# --------------------------------------------------------------------------
# Generic node helpers
# --------------------------------------------------------------------------

class _Builder:
    def __init__(self, tree):
        self.nodes = tree.nodes
        self.links = tree.links

    def node(self, bl_idname, x, y, label=None, **props):
        n = self.nodes.new(bl_idname)
        n.location = (x, y)
        if label:
            n.label = label
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def link(self, out_sock, in_sock):
        self.links.new(out_sock, in_sock)

    def _feed(self, sock, v):
        if isinstance(v, bpy.types.NodeSocket):
            self.link(v, sock)
        else:
            sock.default_value = v

    def math(self, op, x, y, a=None, b=None, c=None, clamp=False, label=None):
        n = self.node("ShaderNodeMath", x, y, label=label, operation=op, use_clamp=clamp)
        for i, v in enumerate((a, b, c)):
            if v is not None:
                self._feed(n.inputs[i], v)
        return n.outputs[0]

    def vmath(self, op, x, y, a=None, b=None, scale=None, label=None):
        n = self.node("ShaderNodeVectorMath", x, y, label=label, operation=op)
        for i, v in enumerate((a, b)):
            if v is not None:
                self._feed(n.inputs[i], v)
        if scale is not None:
            self._feed(n.inputs["Scale"], scale)
        return n.outputs["Value"] if op in ("LENGTH", "DISTANCE", "DOT_PRODUCT") else n.outputs["Vector"]

    def seed_offset(self, seed_sock, x, y, spread=50.0):
        """Seed -> bounded random vector. Adding a raw seed to coordinates
        would push them far from the origin and lose float precision in the
        high-frequency grain; hashing it into [0, spread) avoids that."""
        wn = self.node("ShaderNodeTexWhiteNoise", x, y, label="Seed Hash", noise_dimensions="1D")
        self.link(seed_sock, wn.inputs["W"])
        return self.vmath("SCALE", x + 180, y, a=wn.outputs["Color"], scale=spread)


def _new_interface(ng, inputs, output_name, output_type, input_first=False):
    iface = ng.interface

    def add_out():
        iface.new_socket(name=output_name, in_out="OUTPUT", socket_type=output_type)

    if input_first:
        iface.new_socket(name=output_name, in_out="INPUT", socket_type=output_type)
    add_out()
    for name, stype, default, mn, mx, factor, desc in inputs:
        s = iface.new_socket(name=name, in_out="INPUT", socket_type=stype, description=desc)
        if factor:
            try:
                s.subtype = "FACTOR"
            except (AttributeError, TypeError):
                pass
        s.default_value = default
        if mn is not None:
            s.min_value, s.max_value = mn, mx


def _set_input(node, name, value):
    sock = node.inputs.get(name)
    if sock is not None:
        sock.default_value = value


def _get_group(name, version, builder):
    ng = bpy.data.node_groups.get(name)
    if ng is not None and ng.get("pc_version") == version:
        return ng
    if ng is not None:
        ng.name = name + "_old"  # older materials keep working
    return builder()


# --------------------------------------------------------------------------
# Fingerprint map: generated once with numpy, cached on disk, packed in .blend
# --------------------------------------------------------------------------

def _periodic_noise(np, rng, size, sigma_px):
    """Tileable smooth noise: white noise low-passed in the frequency domain
    (periodic by construction, so the map tiles without seams)."""
    f = np.fft.fftfreq(size)
    fx, fy = np.meshgrid(f, f)
    g = np.exp(-(fx ** 2 + fy ** 2) * (2 * np.pi ** 2) * sigma_px ** 2)
    n = np.real(np.fft.ifft2(np.fft.fft2(rng.standard_normal((size, size))) * g))
    return ((n - n.mean()) / (n.std() + 1e-8)).astype(np.float32)


def generate_fingerprint_map(size=FP_RES, count=600, seed=11):
    """Height map of overlapping finger presses (0.5 = untouched clay).

    Each press is a shallow dent with a crisp ridge pattern, and the clay it
    displaces forms a raised rim at the edge. Presses are composited "over"
    (a new press reshapes the clay under it), so the latest print stays crisp
    and cuts through older ones - that rim/cut outline is what makes handled
    plasticine read as fingerprinted from a normal viewing distance.
    Returns (height with ridges, height without ridges)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    s = size / 2048.0
    warp = _periodic_noise(np, rng, size, 10 * s)
    warp2 = _periodic_noise(np, rng, size, 26 * s)
    ragged = _periodic_noise(np, rng, size, 5 * s)
    full_h = np.full((size, size), 0.5, np.float32)
    low_h = np.full((size, size), 0.5, np.float32)
    for _ in range(count):
        cx, cy = rng.uniform(0, size, 2)
        r = rng.uniform(70, 120) * s
        rot = rng.uniform(0, np.pi)
        squash = rng.uniform(0.62, 0.82)
        depth = rng.uniform(0.06, 0.16)
        rim = rng.uniform(0.02, 0.06)
        ridge_amp = rng.uniform(0.05, 0.11)
        lam = rng.uniform(3.6, 4.6) * s
        whorl = rng.uniform(-1.3, 1.3)
        off = rng.uniform(-0.3, 0.3, 2) * r
        R = int(r * 1.3) + 2
        xs = np.arange(int(cx) - R, int(cx) + R)
        ys = np.arange(int(cy) - R, int(cy) + R)
        sl = np.ix_(ys % size, xs % size)
        dx = (xs - cx)[None, :]
        dy = (ys - cy)[:, None]
        c, sn = np.cos(rot), np.sin(rot)
        u = dx * c + dy * sn
        v = (-dx * sn + dy * c) / squash
        d = np.sqrt(u * u + v * v)
        ang = np.arctan2(v, u)
        edge = r * (1 + 0.05 * np.sin(3 * ang + rot) + 0.03 * np.sin(5 * ang + 2 * rot)) \
            + ragged[sl] * 3 * s
        t = d / edge
        tin = np.clip(t, 0, 1)
        low = 0.5 - depth * (1 - tin ** 2) + rim * np.exp(-((t - 1.0) / 0.06) ** 2)
        pu, pv = u - off[0], v - off[1]
        dd = np.sqrt(pu * pu + pv * pv) + warp[sl] * 1.0 * lam + warp2[sl] * 2.0 * lam
        ridges = np.sin(2 * np.pi * dd / lam + whorl * np.arctan2(pv, pu))
        fade = np.clip((1 - t) / 0.15, 0, 1)          # ridges stop just inside the rim
        frag = np.clip(0.75 + 0.5 * warp2[sl], 0.25, 1)
        full = low + ridge_amp * ridges * fade * frag
        w = np.clip((1.12 - t) / 0.12, 0, 1)          # "over" weight, includes the rim
        full_h[sl] = full_h[sl] * (1 - w) + full * w
        low_h[sl] = low_h[sl] * (1 - w) + low * w
    return np.clip(full_h, 0, 1), np.clip(low_h, 0, 1)


def _fp_cache_path():
    folder = bpy.utils.user_resource("DATAFILES", path="procedural_clay", create=True)
    return os.path.join(folder, FP_CACHE)


def get_fingerprint_image():
    """Return the fingerprint image: already in the file, else from the disk
    cache, else generate it (one-time, a few seconds) and cache it."""
    img = bpy.data.images.get(FP_IMAGE)
    if img is not None and img.has_data and img.get("pc_fp_version") == 3:
        return img
    if img is not None:
        bpy.data.images.remove(img)
    path = _fp_cache_path()
    if os.path.isfile(path):
        img = bpy.data.images.load(path, check_existing=False)
        img.name = FP_IMAGE
    else:
        import numpy as np
        full_h, low_h = generate_fingerprint_map()
        img = bpy.data.images.new(FP_IMAGE, FP_RES, FP_RES, alpha=False, float_buffer=False)
        rgba = np.empty((FP_RES, FP_RES, 4), np.float32)
        # R: height with ridges, G: height without ridges (0.5 = untouched).
        # G survives mipmapping at any distance; R - G is the ridge detail.
        rgba[..., 0] = full_h
        rgba[..., 1] = low_h
        rgba[..., 2] = 0.0
        rgba[..., 3] = 1.0
        img.pixels.foreach_set(rgba.ravel())
        img.filepath_raw = path
        img.file_format = "PNG"
        try:
            img.save()
        except RuntimeError:
            pass  # cache is an optimisation; packing below keeps the file self-contained
    img.colorspace_settings.name = "Non-Color"
    img["pc_fp_version"] = 3
    try:
        img.pack()
    except RuntimeError:
        pass
    return img


def ensure_fingerprint_image(group):
    node = group.nodes.get(FP_NODE) if group else None
    if node is not None and (node.image is None or not node.image.has_data):
        node.image = get_fingerprint_image()


# --------------------------------------------------------------------------
# Shader group
# --------------------------------------------------------------------------

def _build_shader_group():
    ng = bpy.data.node_groups.new(SHADER_GROUP, "ShaderNodeTree")
    ng["pc_version"] = SHADER_VERSION
    _new_interface(ng, SHADER_INPUTS, "BSDF", "NodeSocketShader")

    b = _Builder(ng)
    gin = b.node("NodeGroupInput", -1800, 0)
    gout = b.node("NodeGroupOutput", 1500, 0)
    I = gin.outputs

    # Coordinates: object space, scaled, offset by seed
    # Sticky coordinates. Object coordinates are taken from the *deformed*
    # mesh, so with an armature or shape keys the texture slides over the
    # surface. The Clay Deform modifier stores the rest pose ("pc_coords")
    # plus a flag ("pc_has"); without the modifier the flag reads 0 and we
    # fall back to plain Object coordinates.
    tc = b.node("ShaderNodeTexCoord", -2200, 350)
    pcc = b.node("ShaderNodeAttribute", -2200, 150, label="Rest Coords",
                 attribute_type="GEOMETRY", attribute_name="pc_coords")
    pch = b.node("ShaderNodeAttribute", -2200, -50, label="Has Rest Coords",
                 attribute_type="GEOMETRY", attribute_name="pc_has")
    mixc = b.node("ShaderNodeMix", -2000, 300, label="Sticky Coords", data_type="VECTOR")
    b.link(pch.outputs["Fac"], mixc.inputs[0])
    b.link(tc.outputs["Object"], mixc.inputs[4])
    b.link(pcc.outputs["Vector"], mixc.inputs[5])

    # Stop-motion boil: every held pose shifts all surface detail by a small
    # random amount (like the clay being handled between shots).
    bsize = b.node("ShaderNodeAttribute", -2200, -250, label="Object Size",
                   attribute_type="OBJECT", attribute_name="pclay_size")
    bwn = b.node("ShaderNodeTexWhiteNoise", -2000, 0, label="Boil Hash", noise_dimensions="1D")
    b.link(I["Boil Frame"], bwn.inputs["W"])
    bcent = b.vmath("SUBTRACT", -1820, 0, a=bwn.outputs["Color"], b=(0.5, 0.5, 0.5))
    bamt = b.math("MULTIPLY", -1820, -200, a=I["Boil Amount"], b=bsize.outputs["Fac"])
    bamt = b.math("MULTIPLY", -1640, -200, a=bamt, b=0.15)
    boff = b.vmath("SCALE", -1640, 0, a=bcent, scale=bamt, label="Boil Offset")
    base = b.vmath("ADD", -1600, 300, a=mixc.outputs[1], b=boff)
    scaled = b.vmath("SCALE", -1400, 350, a=base, scale=I["Texture Scale"])
    coords = b.vmath("ADD", -1000, 350, a=scaled, b=b.seed_offset(I["Seed"], -1400, 150),
                     label="Seeded Coords")

    # Light domain warp breaks up the regularity of the textures
    warp = b.node("ShaderNodeTexNoise", -800, 600, label="Warp", noise_dimensions="3D")
    b.link(coords, warp.inputs["Vector"])
    warp.inputs["Scale"].default_value = 1.2
    warp.inputs["Detail"].default_value = 1.0
    centered = b.vmath("SUBTRACT", -600, 600, a=warp.outputs["Color"], b=(0.5, 0.5, 0.5))
    offset = b.vmath("SCALE", -420, 600, a=centered, scale=0.12)
    warped = b.vmath("ADD", -250, 450, a=coords, b=offset, label="Warped Coords")

    # Lumps
    lumps = b.node("ShaderNodeTexNoise", -50, 700, label="Lumps", noise_dimensions="3D")
    b.link(warped, lumps.inputs["Vector"])
    b.link(I["Imperfection Scale"], lumps.inputs["Scale"])
    lumps.inputs["Detail"].default_value = 3.0
    lumps.inputs["Roughness"].default_value = 0.5

    # Pores: Voronoi F1 distance, only on ~half the cells
    pore_scale = b.math("MULTIPLY", -250, 150, a=I["Grain Scale"], b=0.18)
    vor = b.node("ShaderNodeTexVoronoi", -50, 250, label="Pores",
                 voronoi_dimensions="3D", feature="F1", distance="EUCLIDEAN")
    b.link(warped, vor.inputs["Vector"])
    b.link(pore_scale, vor.inputs["Scale"])
    vor.inputs["Randomness"].default_value = 1.0
    pore_radius = b.math("MULTIPLY_ADD", -50, 0, a=I["Pores"], b=0.4, c=0.0001)
    mr = b.node("ShaderNodeMapRange", 170, 300, label="Pore Mask", clamp=True,
                interpolation_type="SMOOTHSTEP")
    b.link(vor.outputs["Distance"], mr.inputs["Value"])
    b.link(pore_radius, mr.inputs["From Max"])
    mr.inputs["From Min"].default_value = 0.0
    mr.inputs["To Min"].default_value = 1.0
    mr.inputs["To Max"].default_value = 0.0
    sep = b.node("ShaderNodeSeparateColor", 170, 100)
    b.link(vor.outputs["Color"], sep.inputs["Color"])
    keep = b.math("GREATER_THAN", 350, 100, a=sep.outputs[0], b=0.5)
    pore_mask = b.math("MULTIPLY", 370, 300, a=mr.outputs["Result"], b=keep, label="Pores")

    # Grain (unwarped so it stays crisp)
    grain = b.node("ShaderNodeTexNoise", -50, -250, label="Grain", noise_dimensions="3D")
    b.link(coords, grain.inputs["Vector"])
    b.link(I["Grain Scale"], grain.inputs["Scale"])
    grain.inputs["Detail"].default_value = 1.0
    grain.inputs["Roughness"].default_value = 0.6

    # Bump distances ~ 1/frequency keep the slope stable when scales change
    def bump_distance(freq_sock, k, x, y, label):
        f = b.math("MULTIPLY", x, y, a=freq_sock, b=I["Texture Scale"])
        f = b.math("MAXIMUM", x + 170, y, a=f, b=0.0001)
        return b.math("DIVIDE", x + 340, y, a=k, b=f, label=label)

    imp_dist = bump_distance(I["Imperfection Scale"], 0.3, 350, 900, "Lump Bump Dist")
    pore_dist = bump_distance(pore_scale, 0.12, 550, -50, "Pore Bump Dist")
    grain_dist = bump_distance(I["Grain Scale"], 0.25, 550, -250, "Grain Bump Dist")

    bump1 = b.node("ShaderNodeBump", 900, 650, label="Lump Bump")
    b.link(lumps.outputs["Fac"], bump1.inputs["Height"])
    b.link(I["Imperfection"], bump1.inputs["Strength"])
    b.link(imp_dist, bump1.inputs["Distance"])

    bump2 = b.node("ShaderNodeBump", 1050, 400, label="Pore Bump", invert=True)
    b.link(pore_mask, bump2.inputs["Height"])
    b.link(I["Grain Intensity"], bump2.inputs["Strength"])
    b.link(pore_dist, bump2.inputs["Distance"])
    b.link(bump1.outputs["Normal"], bump2.inputs["Normal"])

    bump3 = b.node("ShaderNodeBump", 1150, 150, label="Grain Bump")
    b.link(grain.outputs["Fac"], bump3.inputs["Height"])
    b.link(I["Grain Intensity"], bump3.inputs["Strength"])
    b.link(grain_dist, bump3.inputs["Distance"])
    b.link(bump2.outputs["Normal"], bump3.inputs["Normal"])

    # --- Fingerprints (image based) -----------------------------------------
    # A pre-generated tileable map of overlapping finger smudges with real
    # ridge patterns, box-projected in object space (no UVs). An image rather
    # than nodes because ridges are finer than a pixel at normal viewing
    # distances: textures get mipmapped/filtered by Blender, procedural nodes
    # don't, so an image never shimmers in EEVEE and costs one lookup.
    fx, fy = -50, -1200
    fsize = b.node("ShaderNodeAttribute", fx - 400, fy, label="Object Size",
                   attribute_type="OBJECT", attribute_name="pclay_size")
    tile = b.math("MULTIPLY", fx - 220, fy, a=fsize.outputs["Fac"], b=I["Print Size"])
    tile = b.math("MULTIPLY", fx - 60, fy, a=tile, b=10.0)
    tile = b.math("MAXIMUM", fx + 100, fy, a=tile, b=0.0001, label="Print Tile Size")
    inv_tile = b.math("DIVIDE", fx + 260, fy, a=1.0, b=tile)
    fp_vec = b.vmath("SCALE", fx + 420, fy, a=coords, scale=inv_tile, label="Print Space")
    fimg = b.node("ShaderNodeTexImage", fx + 600, fy, label="Fingerprint Map",
                  projection="BOX", interpolation="Linear", extension="REPEAT")
    fimg.name = FP_NODE
    fimg.projection_blend = 0.35
    fimg.image = get_fingerprint_image()
    b.link(fp_vec, fimg.inputs["Vector"])
    fsep = b.node("ShaderNodeSeparateColor", fx + 800, fy)
    b.link(fimg.outputs["Color"], fsep.inputs["Color"])
    fp_full = fsep.outputs[0]     # height incl. ridges (close-up detail)
    fp_low = fsep.outputs[1]      # dents + rims: reads at any distance
    fp = b.math("SUBTRACT", fx + 900, fy - 100, a=fp_full, b=fp_low, label="Ridges")
    fpress = b.math("SUBTRACT", fx + 900, fy + 100, a=0.5, b=fp_low)
    fpress = b.math("MULTIPLY", fx + 1000, fy + 100, a=fpress, b=5.0, clamp=True,
                    label="Pressed")
    fp_strength = b.math("MULTIPLY", fx + 900, fy - 250, a=fpress, b=I["Fingerprints"],
                         label="Print Amount")

    # Two bumps at two scales. The ridges are ~5 texels apart, so from a
    # normal viewing distance they are smaller than a pixel and mipmapping
    # averages them away - which is correct (no shimmer), but it means the
    # ridges alone can't carry the effect. The press channel is the shape of
    # each finger pad and survives at any distance.
    press_dist = b.math("MULTIPLY", fx + 900, fy + 450, a=tile, b=0.03)
    bump_press = b.node("ShaderNodeBump", fx + 1000, fy + 200, label="Finger Press Bump")
    b.link(fp_low, bump_press.inputs["Height"])
    b.link(I["Fingerprints"], bump_press.inputs["Strength"])
    b.link(press_dist, bump_press.inputs["Distance"])
    b.link(bump3.outputs["Normal"], bump_press.inputs["Normal"])

    fp_dist = b.math("MULTIPLY", fx + 900, fy + 250, a=tile, b=12.0 / 2048.0)
    bump_fp = b.node("ShaderNodeBump", fx + 1200, fy, label="Ridge Bump")
    b.link(fp, bump_fp.inputs["Height"])
    b.link(I["Fingerprints"], bump_fp.inputs["Strength"])
    b.link(fp_dist, bump_fp.inputs["Distance"])
    b.link(bump_press.outputs["Normal"], bump_fp.inputs["Normal"])
    # touched clay is slightly polished: prints show mostly in the highlights
    fp_cav = b.math("MULTIPLY", fx + 1100, fy - 250, a=fp_strength, b=0.2)

    # Color: tint by lumps
    darker = b.node("ShaderNodeHueSaturation", 350, 1300, label="Darker Clay")
    b.link(I["Clay Color"], darker.inputs["Color"])
    darker.inputs["Saturation"].default_value = 1.1
    darker.inputs["Value"].default_value = 0.65

    lump_c = b.node("ShaderNodeMapRange", 350, 1100, clamp=True)
    b.link(lumps.outputs["Fac"], lump_c.inputs["Value"])
    lump_c.inputs["From Min"].default_value = 0.35
    lump_c.inputs["From Max"].default_value = 0.65
    tint_fac = b.math("MULTIPLY", 550, 1100, a=lump_c.outputs["Result"], b=I["Color Variation"])

    # ShaderNodeMix: color A/B/Result are at socket indices 6/7/2
    mix_tint = b.node("ShaderNodeMix", 750, 1200, label="Tint", data_type="RGBA", blend_type="MIX")
    b.link(tint_fac, mix_tint.inputs[0])
    b.link(I["Clay Color"], mix_tint.inputs[6])
    b.link(darker.outputs["Color"], mix_tint.inputs[7])

    # Cavity: dents (low lumps) + pores + dark grain, weighted by Imperfection
    # and Grain Intensity so it follows the bump sliders.
    low = b.math("SUBTRACT", 550, 950, a=0.55, b=lumps.outputs["Fac"])
    low = b.math("MULTIPLY", 720, 950, a=low, b=I["Imperfection"])
    g_dark = b.math("SUBTRACT", 550, 800, a=0.5, b=grain.outputs["Fac"])
    fine = b.math("MULTIPLY_ADD", 720, 800, a=pore_mask, b=1.5, c=g_dark)
    fine = b.math("MULTIPLY", 890, 800, a=fine, b=I["Grain Intensity"])
    cav = b.math("MULTIPLY_ADD", 890, 950, a=low, b=1.6, c=fine)
    cav = b.math("MULTIPLY_ADD", 960, 900, a=fp_cav, b=0.35, c=cav)
    cav = b.math("MULTIPLY", 1060, 950, a=cav, b=I["Cavity"], clamp=True, label="Cavity")

    mix_cav = b.node("ShaderNodeMix", 1150, 1150, label="Cavity Darken",
                     data_type="RGBA", blend_type="MULTIPLY")
    b.link(cav, mix_cav.inputs[0])
    b.link(mix_tint.outputs[2], mix_cav.inputs[6])
    b.link(darker.outputs["Color"], mix_cav.inputs[7])

    # Roughness broken up by grain, pores a bit rougher
    gc = b.math("SUBTRACT", 550, -500, a=grain.outputs["Fac"], b=0.5)
    rough = b.math("MULTIPLY_ADD", 720, -500, a=gc, b=0.2, c=I["Roughness"])
    rough = b.math("MULTIPLY_ADD", 890, -500, a=pore_mask, b=0.1, c=rough)
    rough = b.math("MULTIPLY_ADD", 1060, -500, a=fp_strength, b=-0.3, c=rough, clamp=True)

    # Specks: a sparse subset of small Voronoi cells gets a lighter dot
    spv = b.node("ShaderNodeTexVoronoi", 700, 1600, label="Specks",
                 voronoi_dimensions="3D", feature="F1", distance="EUCLIDEAN")
    b.link(coords, spv.inputs["Vector"])
    b.link(b.math("MULTIPLY", 500, 1600, a=I["Grain Scale"], b=0.3), spv.inputs["Scale"])
    sp_sep = b.node("ShaderNodeSeparateColor", 880, 1500)
    b.link(spv.outputs["Color"], sp_sep.inputs["Color"])
    sp_on = b.math("GREATER_THAN", 1050, 1500, a=sp_sep.outputs[1], b=0.88)
    sp_dot = b.node("ShaderNodeMapRange", 880, 1700, clamp=True, interpolation_type="SMOOTHSTEP")
    b.link(spv.outputs["Distance"], sp_dot.inputs["Value"])
    sp_dot.inputs["From Min"].default_value = 0.0
    sp_dot.inputs["From Max"].default_value = 0.12
    sp_dot.inputs["To Min"].default_value = 1.0
    sp_dot.inputs["To Max"].default_value = 0.0
    sp_mask = b.math("MULTIPLY", 1050, 1700, a=sp_dot.outputs["Result"], b=sp_on)
    sp_mask = b.math("MULTIPLY", 1200, 1700, a=sp_mask, b=I["Specks"], label="Speck Mask")
    lighter = b.node("ShaderNodeHueSaturation", 1050, 1350, label="Speck Color")
    b.link(I["Clay Color"], lighter.inputs["Color"])
    lighter.inputs["Saturation"].default_value = 0.6
    lighter.inputs["Value"].default_value = 1.6
    mix_sp = b.node("ShaderNodeMix", 1300, 1250, label="Specks", data_type="RGBA", blend_type="MIX")
    b.link(sp_mask, mix_sp.inputs[0])
    b.link(mix_cav.outputs[2], mix_sp.inputs[6])
    b.link(lighter.outputs["Color"], mix_sp.inputs[7])

    # Subsurface radius scales with the object: the apply operator stores
    # each object's size in the "pclay_size" custom property, read here with
    # an Object-type Attribute node (works in Cycles and EEVEE). A fixed
    # radius would make small objects glow and big ones look like plastic.
    size_attr = b.node("ShaderNodeAttribute", 900, 50, label="Object Size",
                       attribute_type="OBJECT", attribute_name="pclay_size")
    sss_scale = b.math("MULTIPLY", 1100, 50, a=size_attr.outputs["Fac"], b=0.02,
                       label="SSS Scale")

    bsdf = b.node("ShaderNodeBsdfPrincipled", 1300, 300)
    b.link(mix_sp.outputs[2], bsdf.inputs["Base Color"])
    b.link(rough, bsdf.inputs["Roughness"])
    b.link(bump_fp.outputs["Normal"], bsdf.inputs["Normal"])
    _set_input(bsdf, "Specular IOR Level", 0.5)
    _set_input(bsdf, "Sheen Weight", 0.15)
    _set_input(bsdf, "Sheen Roughness", 0.5)
    b.link(I["Subsurface"], bsdf.inputs["Subsurface Weight"])
    b.link(sss_scale, bsdf.inputs["Subsurface Scale"])
    _set_input(bsdf, "Subsurface Radius", (1.0, 0.5, 0.3))
    b.link(bsdf.outputs["BSDF"], gout.inputs["BSDF"])
    return ng


# --------------------------------------------------------------------------
# Deformation group (Geometry Nodes)
# --------------------------------------------------------------------------

def _build_deform_group():
    ng = bpy.data.node_groups.new(DEFORM_GROUP, "GeometryNodeTree")
    ng["pc_version"] = DEFORM_VERSION
    ng.is_modifier = True
    _new_interface(ng, DEFORM_INPUTS, "Geometry", "NodeSocketGeometry", input_first=True)

    b = _Builder(ng)
    gin = b.node("NodeGroupInput", -1400, 0)
    gout = b.node("NodeGroupOutput", 900, 0)
    I = gin.outputs

    # Rest-pose coordinates. With the object's "rest_position" attribute
    # (enabled by the add-on) every measurement and noise lookup uses the
    # un-posed mesh, so lumps stick to the surface when an armature or shape
    # keys move it, and sizes/levels don't change from frame to frame.
    rest = b.node("GeometryNodeInputNamedAttribute", -2000, -900, label="Rest Position",
                  data_type="FLOAT_VECTOR")
    rest.inputs["Name"].default_value = "rest_position"
    posn = b.node("GeometryNodeInputPosition", -2000, -1050)
    sw = b.node("GeometryNodeSwitch", -1800, -950, input_type="VECTOR")
    b.link(rest.outputs["Exists"], sw.inputs["Switch"])
    b.link(posn.outputs["Position"], sw.inputs["False"])
    b.link(rest.outputs["Attribute"], sw.inputs["True"])
    rest_co = sw.outputs["Output"]

    # Size of the object (rest-pose bounding-box diagonal), before subdividing
    bstat = b.node("GeometryNodeAttributeStatistic", -1200, -300, data_type="FLOAT_VECTOR",
                   domain="POINT")
    b.link(I["Geometry"], bstat.inputs["Geometry"])
    b.link(rest_co, bstat.inputs["Attribute"])
    diag = b.vmath("SUBTRACT", -1000, -300, a=bstat.outputs["Max"], b=bstat.outputs["Min"])
    size = b.vmath("LENGTH", -820, -300, a=diag)
    size = b.math("MAXIMUM", -650, -300, a=size, b=0.0001, label="Object Size")

    # Adaptive subdivision. Each level multiplies faces by 4, so a fixed level
    # on a mesh that is already dense (e.g. after a Subdivision Surface
    # modifier) explodes memory, in the viewport too. Instead: measure the mean
    # edge length and add only the levels needed to get ~8 vertices per lump;
    # "Subdivisions" is the upper limit.
    ev = b.node("GeometryNodeInputMeshEdgeVertices", -1600, 450)
    def _sample(idx_sock, y):
        n = b.node("GeometryNodeSampleIndex", -1420, y, data_type="FLOAT_VECTOR", domain="POINT")
        b.link(I["Geometry"], n.inputs["Geometry"])
        b.link(rest_co, n.inputs["Value"])
        b.link(idx_sock, n.inputs["Index"])
        return n.outputs["Value"]
    elen = b.vmath("DISTANCE", -1240, 600, a=_sample(ev.outputs["Vertex Index 1"], 650),
                   b=_sample(ev.outputs["Vertex Index 2"], 550))
    stat = b.node("GeometryNodeAttributeStatistic", -1240, 450, data_type="FLOAT", domain="EDGE")
    b.link(I["Geometry"], stat.inputs["Geometry"])
    b.link(elen, stat.inputs["Attribute"])
    mean_edge = b.math("MAXIMUM", -1060, 450, a=stat.outputs["Mean"], b=0.000001, label="Mean Edge")
    lump_len = b.math("MULTIPLY", -1060, 300, a=size, b=I["Lump Size"])
    target = b.math("DIVIDE", -880, 300, a=lump_len, b=8.0, label="Target Edge")
    ratio = b.math("DIVIDE", -880, 450, a=mean_edge, b=target)
    ratio = b.math("MAXIMUM", -700, 450, a=ratio, b=1.0)
    need = b.math("LOGARITHM", -520, 450, a=ratio, b=2.0)
    need = b.math("CEIL", -340, 450, a=need)
    level = b.math("MINIMUM", -160, 450, a=need, b=I["Subdivisions"], label="Auto Level")

    subdiv = b.node("GeometryNodeSubdivideMesh", -1000, 100)
    b.link(I["Geometry"], subdiv.inputs["Mesh"])
    b.link(level, subdiv.inputs["Level"])

    # Smoothed normals. Pushing along the raw normal tears concave creases
    # (inset + extrude, mouths, eye sockets): vertices on each side of the
    # crease move in different directions and cut through each other.
    # Blurring the normal over a distance comparable to a lump makes both
    # sides move together, so the surface stays whole.
    edge_after = b.math("POWER", 20, 450, a=2.0, b=level)
    edge_after = b.math("DIVIDE", 200, 450, a=mean_edge, b=edge_after)
    hops = b.math("MULTIPLY", 200, 300, a=lump_len, b=0.35)
    hops = b.math("DIVIDE", 380, 350, a=hops, b=edge_after)
    hops = b.math("MINIMUM", 560, 350, a=hops, b=40.0)
    hops = b.math("MAXIMUM", 740, 350, a=hops, b=2.0, label="Blur Iterations")

    # Noise sampled in object space normalised by size -> size-independent look
    inv = b.math("DIVIDE", -650, -500, a=1.0, b=size)
    freq = b.math("DIVIDE", -480, -500, a=inv, b=I["Lump Size"])
    p = b.vmath("SCALE", -300, -600, a=rest_co, scale=freq)
    p = b.vmath("ADD", -120, -600, a=p, b=b.seed_offset(I["Seed"], -480, -750))

    # Stop-motion boil: hold each pose for "Boil Step" frames, then jump the
    # noise lookup by a random offset (in lump units), so lumps re-shape in
    # steps like hand-touched clay between shots.
    stime = b.node("GeometryNodeInputSceneTime", -700, -1100)
    stepn = b.math("MAXIMUM", -520, -1150, a=I["Boil Step"], b=1.0)
    sidx = b.math("DIVIDE", -340, -1100, a=stime.outputs["Frame"], b=stepn)
    sidx = b.math("FLOOR", -160, -1100, a=sidx, label="Pose Index")
    gwn = b.node("ShaderNodeTexWhiteNoise", 20, -1100, label="Boil Hash", noise_dimensions="1D")
    b.link(sidx, gwn.inputs["W"])
    gcent = b.vmath("SUBTRACT", 200, -1100, a=gwn.outputs["Color"], b=(0.5, 0.5, 0.5))
    # A separate layer (not a shift of the lumps): works even with
    # Deformation at 0, and each pose looks up a completely new noise region.
    gjit = b.vmath("SCALE", 380, -1100, a=gcent, scale=40.0, label="Pose Offset")
    bp = b.vmath("ADD", 560, -1100, a=p, b=gjit)
    bnoise = b.node("ShaderNodeTexNoise", 740, -1100, label="Boil Noise", noise_dimensions="3D")
    b.link(bp, bnoise.inputs["Vector"])
    bnoise.inputs["Scale"].default_value = 1.3
    bnoise.inputs["Detail"].default_value = 1.0
    hb = b.math("MULTIPLY_ADD", 920, -1100, a=bnoise.outputs["Fac"], b=2.0, c=-1.0)
    bamp = b.math("MULTIPLY", 920, -1250, a=I["Boil"], b=size)
    bamp = b.math("MULTIPLY", 1100, -1250, a=bamp, b=0.05, label="Boil Amplitude")
    boil_disp = b.math("MULTIPLY", 1100, -1100, a=hb, b=bamp)

    # Lumps are sized relative to the object (Lump Size = fraction of its
    # diagonal). They must stay well below the object's size: noise at a
    # scale close to the whole object swells one side and shrinks the other,
    # which reads as a distorted shape instead of a hand-worked surface.
    big = b.node("ShaderNodeTexNoise", 60, -500, label="Lumps", noise_dimensions="3D")
    b.link(p, big.inputs["Vector"])
    big.inputs["Scale"].default_value = 1.0
    big.inputs["Detail"].default_value = 0.5
    big.inputs["Roughness"].default_value = 0.4

    small = b.node("ShaderNodeTexNoise", 60, -800, label="Small Dents", noise_dimensions="3D")
    b.link(p, small.inputs["Vector"])
    small.inputs["Scale"].default_value = 3.5
    small.inputs["Detail"].default_value = 1.0

    # height centred on 0, then biased inward: pressed clay mostly dents in,
    # and it keeps the silhouette from growing over attached parts (eyes...)
    h_big = b.math("MULTIPLY_ADD", 260, -500, a=big.outputs["Fac"], b=2.0, c=-1.0)
    h_small = b.math("MULTIPLY_ADD", 260, -800, a=small.outputs["Fac"], b=2.0, c=-1.0)
    h_small = b.math("MULTIPLY", 430, -800, a=h_small, b=I["Detail"])
    h = b.math("MULTIPLY_ADD", 430, -600, a=h_small, b=0.5, c=h_big)
    h = b.math("SUBTRACT", 520, -600, a=h, b=0.2, label="Inward Bias")

    # amplitude proportional to lump size: small lumps -> shallow, big -> deeper,
    # so the bumps keep the same proportions at any Lump Size
    amp = b.math("MULTIPLY", 430, -300, a=I["Intensity"], b=size)
    amp = b.math("MULTIPLY", 600, -300, a=amp, b=I["Lump Size"])
    amp = b.math("MULTIPLY", 770, -300, a=amp, b=0.22)
    disp = b.math("MULTIPLY", 700, -500, a=h, b=amp)
    disp = b.math("ADD", 1280, -700, a=disp, b=boil_disp, label="Total Offset")

    normal = b.node("GeometryNodeInputNormal", 430, -150)
    blur = b.node("GeometryNodeBlurAttribute", 560, -50, data_type="FLOAT_VECTOR",
                  label="Smooth Normals")
    b.link(normal.outputs["Normal"], blur.inputs["Value"])
    b.link(hops, blur.inputs["Iterations"])
    smooth_n = b.vmath("NORMALIZE", 740, -50, a=blur.outputs["Value"])
    off = b.vmath("SCALE", 900, -150, a=smooth_n, scale=disp)

    # Hand the rest-pose coordinates to the shader (sticky textures)
    st1 = b.node("GeometryNodeStoreNamedAttribute", 300, 250, data_type="FLOAT_VECTOR",
                 domain="POINT", label="Store Rest Coords")
    st1.inputs["Name"].default_value = "pc_coords"
    b.link(subdiv.outputs["Mesh"], st1.inputs["Geometry"])
    b.link(rest_co, st1.inputs["Value"])
    st2 = b.node("GeometryNodeStoreNamedAttribute", 500, 250, data_type="FLOAT",
                 domain="POINT", label="Store Flag")
    st2.inputs["Name"].default_value = "pc_has"
    st2.inputs["Value"].default_value = 1.0
    b.link(st1.outputs["Geometry"], st2.inputs["Geometry"])

    setpos = b.node("GeometryNodeSetPosition", 700, 100)
    b.link(st2.outputs["Geometry"], setpos.inputs["Geometry"])
    b.link(off, setpos.inputs["Offset"])
    b.link(setpos.outputs["Geometry"], gout.inputs["Geometry"])
    return ng


# --------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------

def create_clay_material(color, seed):
    mat = bpy.data.materials.new("Clay")
    if bpy.app.version < (5, 0, 0):
        mat.use_nodes = True
    mat[MAT_TAG] = True
    nt = mat.node_tree
    nt.nodes.clear()

    grp = nt.nodes.new("ShaderNodeGroup")
    grp.node_tree = _get_group(SHADER_GROUP, SHADER_VERSION, _build_shader_group)
    ensure_fingerprint_image(grp.node_tree)
    grp.name = grp.label = NODE_NAME
    grp.width = 220
    grp.inputs["Clay Color"].default_value = color
    grp.inputs["Seed"].default_value = seed

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (320, 0)
    nt.links.new(grp.outputs["BSDF"], out.inputs["Surface"])

    ensure_boil_drivers(mat, bpy.context.scene)
    mat.diffuse_color = color
    mat.roughness = grp.inputs["Roughness"].default_value
    return mat


def find_clay_node(mat):
    if mat is None or mat.node_tree is None or not mat.get(MAT_TAG):
        return None
    node = mat.node_tree.nodes.get(NODE_NAME)
    if node is None or node.type != "GROUP" or node.node_tree is None:
        return None
    return node


def upgrade_clay_material(mat):
    """Point an existing clay material at the current shader group, keeping
    the values of every input that still exists."""
    node = find_clay_node(mat)
    group = _get_group(SHADER_GROUP, SHADER_VERSION, _build_shader_group)
    ensure_fingerprint_image(group)
    if node is None or node.node_tree is group:
        return
    saved = {}
    for sock in node.inputs:
        if hasattr(sock, "default_value"):
            v = sock.default_value
            saved[sock.name] = tuple(v) if hasattr(v, "__len__") else v
    node.node_tree = group
    for sock in node.inputs:
        if sock.name in saved:
            try:
                sock.default_value = saved[sock.name]
            except (TypeError, ValueError):
                pass
    ensure_boil_drivers(mat, bpy.context.scene)
    out = mat.node_tree.nodes.get("Material Output")
    if out is not None and not out.inputs["Surface"].is_linked:
        mat.node_tree.links.new(node.outputs["BSDF"], out.inputs["Surface"])


def tag_object_size(obj):
    """Store world-space size for the shader (subsurface radius)."""
    obj["pclay_size"] = max(obj.dimensions.length, 0.001)


def find_deform_mod(obj):
    if obj is None or obj.type != "MESH":
        return None
    mod = obj.modifiers.get(MOD_NAME)
    if mod is None or mod.type != "NODES" or mod.node_group is None:
        return None
    return mod


def mod_socket_id(mod, name):
    for item in mod.node_group.interface.items_tree:
        if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == name:
            return item.identifier
    return None


def set_mod_input(mod, name, value):
    """Write a Geometry Nodes modifier input across Blender versions.

    Up to 5.1 inputs are ID properties (mod["Socket_1"]). Since 5.2 they are
    real RNA properties under mod.properties.inputs and the old bracket
    access raises TypeError.
    """
    ident = mod_socket_id(mod, name)
    if ident is None:
        return
    props = getattr(mod, "properties", None)
    inputs = getattr(props, "inputs", None) if props is not None else None
    if inputs is not None:
        inp = getattr(inputs, ident, None)
        if inp is None:
            try:
                inp = inputs[ident]
            except (KeyError, TypeError, IndexError):
                inp = None
        if inp is not None:
            inp.value = value
            return
    mod[ident] = value


def push_deform_settings(obj):
    mod = find_deform_mod(obj)
    if mod is None:
        return
    s = obj.pclay_deform
    set_mod_input(mod, "Intensity", s.intensity)
    set_mod_input(mod, "Lump Size", s.lump_size)
    set_mod_input(mod, "Detail", s.detail)
    set_mod_input(mod, "Subdivisions", s.subdivisions)
    set_mod_input(mod, "Seed", s.seed)
    sm = getattr(bpy.context.scene, "pclay_stop", None) if bpy.context else None
    if sm is not None:
        set_mod_input(mod, "Boil", sm.boil if sm.enabled else 0.0)
        set_mod_input(mod, "Boil Step", sm.step)
    obj.update_tag()


def _on_deform_change(self, context):
    push_deform_settings(self.id_data)


# --------------------------------------------------------------------------
# Stop motion
# --------------------------------------------------------------------------

def _all_clay_deform_objects():
    return [o for o in bpy.data.objects if find_deform_mod(o) is not None]


def _on_stop_change(self, context):
    # Upgrade everything in the scene so stop motion works without having to
    # re-run Apply Clay on objects made with an older version.
    for mat in bpy.data.materials:
        if find_clay_node(mat) is not None:
            upgrade_clay_material(mat)
            ensure_boil_drivers(mat, context.scene)
    group = _get_group(DEFORM_GROUP, DEFORM_VERSION, _build_deform_group)
    for obj in _all_clay_deform_objects():
        mod = find_deform_mod(obj)
        if mod.node_group is not group:
            mod.node_group = group
        enable_rest_position(obj)
        push_deform_settings(obj)
    # material drivers read these props directly; just make sure they re-evaluate
    for mat in bpy.data.materials:
        if find_clay_node(mat) is not None:
            mat.node_tree.update_tag()


class PCLAY_StopMotion(PropertyGroup):
    """Scene-wide: stop motion is a look for the whole shot."""
    enabled: BoolProperty(name="Stop Motion", default=False, update=_on_stop_change,
                          description="Clay surface and lumps change in steps, like "
                                      "hand-animated plasticine")
    step: IntProperty(name="Hold Frames", default=2, min=1, max=12, update=_on_stop_change,
                      description="Frames each pose is held (2 = 'on twos', the classic "
                                  "claymation rhythm at 24 fps)")
    boil: FloatProperty(name="Boil", default=0.5, min=0.0, max=1.0, subtype="FACTOR",
                        update=_on_stop_change,
                        description="How much the surface changes between held poses")


def ensure_boil_drivers(mat, scene):
    """Drive the shader's Boil Frame/Amount from the scene's stop-motion props.
    Simple expressions only (floor/max/frame), so they run without Python
    auto-execution being enabled."""
    node = find_clay_node(mat)
    if node is None or "Boil Frame" not in node.inputs:
        return
    nt = mat.node_tree
    specs = (("Boil Frame", "floor(frame / max(step, 1)) * on"),
             ("Boil Amount", "boil * on"))
    for name, expr in specs:
        idx = list(node.inputs).index(node.inputs[name])
        path = f'nodes["{node.name}"].inputs[{idx}].default_value'
        try:
            nt.driver_remove(path)
        except TypeError:
            pass
        fc = nt.driver_add(path)
        drv = fc.driver
        drv.type = "SCRIPTED"
        for var_name, prop in (("step", "pclay_stop.step"), ("on", "pclay_stop.enabled"),
                               ("boil", "pclay_stop.boil")):
            v = drv.variables.new()
            v.name = var_name
            v.type = "SINGLE_PROP"
            v.targets[0].id_type = "SCENE"
            v.targets[0].id = scene
            v.targets[0].data_path = prop
        drv.expression = expr


def _iter_fcurves(action, anim_data=None):
    """F-curves of an action across Blender versions: 5.x layered actions
    (channelbags per slot) and older flat actions."""
    fcs = getattr(action, "fcurves", None)
    if fcs is not None and len(fcs):
        yield from fcs
        return
    for layer in getattr(action, "layers", []):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                yield from bag.fcurves


def _animated_actions(objs):
    seen = set()
    for o in objs:
        for idb in (o, getattr(o, "data", None)):
            ad = getattr(idb, "animation_data", None) if idb is not None else None
            if ad and ad.action and ad.action.name not in seen:
                seen.add(ad.action.name)
                yield ad.action


class PCLAY_OT_step_animation(Operator):
    bl_idname = "pclay.step_animation"
    bl_label = "Animate on Steps"
    bl_description = ("Hold each pose for 'Hold Frames' on the selected objects' animation "
                      "(adds a Stepped modifier to their F-curves; the keys are untouched)")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        step = context.scene.pclay_stop.step
        n = 0
        for action in _animated_actions(context.selected_objects):
            for fc in _iter_fcurves(action):
                mod = next((m for m in fc.modifiers if m.type == "STEPPED"), None)
                if mod is None:
                    mod = fc.modifiers.new("STEPPED")
                mod.frame_step = step
                mod.frame_offset = 0
                n += 1
        if n == 0:
            self.report({"WARNING"}, "Selected objects have no animation (for armatures, "
                                     "select the armature)")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Stepped {n} animation curves on {step}s")
        return {"FINISHED"}


class PCLAY_OT_smooth_animation(Operator):
    bl_idname = "pclay.smooth_animation"
    bl_label = "Smooth Animation"
    bl_description = "Remove the Stepped modifiers added by Animate on Steps"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        for action in _animated_actions(context.selected_objects):
            for fc in _iter_fcurves(action):
                for m in [m for m in fc.modifiers if m.type == "STEPPED"]:
                    fc.modifiers.remove(m)
        return {"FINISHED"}


class PCLAY_DeformSettings(PropertyGroup):
    """Panel-facing copy of the modifier inputs. Drawing our own properties
    avoids depending on how each Blender version exposes modifier inputs."""
    intensity: FloatProperty(name="Deformation", default=0.35, min=0.0, max=1.0,
                             subtype="FACTOR", update=_on_deform_change,
                             description="How lumpy the silhouette gets. 0 keeps the shape")
    lump_size: FloatProperty(name="Lump Size", default=0.15, min=0.03, max=1.0,
                             update=_on_deform_change,
                             description="Size of the bumps relative to the object")
    detail: FloatProperty(name="Detail", default=0.2, min=0.0, max=1.0, subtype="FACTOR",
                          update=_on_deform_change,
                          description="Smaller dents on top of the big lumps")
    subdivisions: IntProperty(name="Max Subdivisions", default=3, min=0, max=5,
                              update=_on_deform_change,
                              description="Upper limit for the automatic extra subdivision. "
                                          "Dense meshes get none. Each level x4 faces")
    seed: FloatProperty(name="Seed", default=0.0, min=0.0, max=10000.0,
                        update=_on_deform_change)


def enable_rest_position(obj):
    """Ask Blender to keep the un-posed vertex positions (before armature and
    shape keys) as the 'rest_position' attribute - the source of sticky
    coordinates."""
    if hasattr(obj, "add_rest_position_attribute"):
        obj.add_rest_position_attribute = True


def add_deform_modifier(obj, intensity):
    mod = find_deform_mod(obj)
    if mod is None:
        mod = obj.modifiers.new(MOD_NAME, "NODES")
    # always (re)point to the current group so re-applying upgrades old objects
    mod.node_group = _get_group(DEFORM_GROUP, DEFORM_VERSION, _build_deform_group)
    enable_rest_position(obj)
    s = obj.pclay_deform
    s["intensity"] = intensity           # raw writes: skip 5 separate updates
    s["seed"] = random.uniform(0.0, 1000.0)
    push_deform_settings(obj)
    return mod


def supports_materials(obj):
    return obj is not None and obj.data is not None and hasattr(obj.data, "materials")


def _active_clay(context):
    obj = context.active_object
    if obj is None:
        return None, None
    mat = obj.active_material
    return mat, find_clay_node(mat)


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class PCLAY_OT_apply(Operator):
    bl_idname = "pclay.apply"
    bl_label = "Apply Clay"
    bl_description = "Add procedural clay (material + deformation) to the selected objects"
    bl_options = {"REGISTER", "UNDO"}

    color: FloatVectorProperty(name="Clay Color", subtype="COLOR", size=4, min=0.0, max=1.0,
                               default=(0.62, 0.36, 0.25, 1.0))
    shared: BoolProperty(name="Shared Material", default=True,
                         description="One material for all selected objects. Off: one per "
                                     "object, each with a different seed")
    replace: BoolProperty(name="Replace All Slots", default=True,
                          description="Remove existing materials. Off: only the active slot")
    deform: BoolProperty(name="Deform Shape", default=True,
                         description="Add the Clay Deform modifier to mesh objects")
    intensity: FloatProperty(name="Deform Intensity", default=0.35, min=0.0, max=1.0,
                             subtype="FACTOR")

    @classmethod
    def poll(cls, context):
        return any(supports_materials(o) for o in context.selected_objects)

    def execute(self, context):
        targets = [o for o in context.selected_objects if supports_materials(o)]
        shared_mat = None
        for obj in targets:
            tag_object_size(obj)
            existing = obj.active_material
            if find_clay_node(existing) is not None:
                # Already clay: upgrade in place and keep the user's settings
                upgrade_clay_material(existing)
            else:
                if self.shared:
                    shared_mat = shared_mat or create_clay_material(self.color, 0.0)
                    mat = shared_mat
                else:
                    mat = create_clay_material(self.color, random.uniform(0.0, 1000.0))
                if self.replace or not obj.material_slots:
                    obj.data.materials.clear()
                    obj.data.materials.append(mat)
                else:
                    obj.active_material = mat
            if self.deform and obj.type == "MESH":
                add_deform_modifier(obj, self.intensity)
        self.report({"INFO"}, f"Clay applied to {len(targets)} object(s)")
        return {"FINISHED"}


class PCLAY_OT_add_deform(Operator):
    bl_idname = "pclay.add_deform"
    bl_label = "Add Deformation"
    bl_description = "Add the Clay Deform modifier to the selected meshes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.selected_objects)

    def execute(self, context):
        for obj in context.selected_objects:
            if obj.type == "MESH":
                add_deform_modifier(obj, 0.35)
        return {"FINISHED"}


class PCLAY_OT_randomize_seed(Operator):
    bl_idname = "pclay.randomize_seed"
    bl_label = "Randomize"
    bl_description = "New random seed for the active clay material and deformation"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.active_object
        node = find_clay_node(obj.active_material) if obj else None
        if node:
            node.inputs["Seed"].default_value = random.uniform(0.0, 1000.0)
        mod = find_deform_mod(obj)
        if mod:
            obj.pclay_deform.seed = random.uniform(0.0, 1000.0)
        return {"FINISHED"}


class PCLAY_OT_eevee_setup(Operator):
    bl_idname = "pclay.eevee_setup"
    bl_label = "EEVEE Clay Setup"
    bl_description = ("Switch to EEVEE and enable ray tracing and horizon-scan AO so the "
                      "clay dents and lumps get proper contact shading")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        engines = {e.identifier for e in
                   bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
        for eid in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
            if eid in engines:
                scene.render.engine = eid
                break
        ee = scene.eevee
        for attr, val in (("use_raytracing", True),   # screen-space reflections/GI
                          ("use_fast_gi", True),      # horizon scan: AO in the dents
                          ("fast_gi_distance", 0.3),
                          ("use_shadows", True),
                          ("use_shadow_jitter_viewport", True),   # soft shadows in viewport
                          ("shadow_pool_size", "256")):           # 512 MB default; iGPUs share RAM
            if hasattr(ee, attr):
                try:
                    setattr(ee, attr, val)
                except (TypeError, AttributeError):
                    pass
        rto = getattr(ee, "ray_tracing_options", None)
        if rto is not None and hasattr(rto, "resolution_scale"):
            try:
                rto.resolution_scale = "2"   # half-res tracing: ~1/4 the buffers
            except TypeError:
                pass
        # Grazing light is what makes clay read; a dim world keeps it from
        # washing out.
        space = context.space_data
        if space and space.type == "VIEW_3D" and space.shading.type in {"SOLID", "WIREFRAME"}:
            space.shading.type = "MATERIAL"
        self.report({"INFO"}, "EEVEE configured for clay")
        return {"FINISHED"}


PRESETS = {
    # values: Roughness, Subsurface, Grain Intensity, Imperfection, Cavity, Pores, Specks,
    #         Color Variation, Fingerprints
    "PLAYDOH": ("Play-Doh", "Soft, slightly glossy, light bleeding through",
                (0.5, 0.3, 0.25, 0.4, 0.35, 0.2, 0.25, 0.3, 0.3)),
    "SMOOTH": ("Smooth Clay", "Very soft and clean, almost no grain",
               (0.45, 0.4, 0.12, 0.3, 0.25, 0.08, 0.1, 0.2, 0.15)),
    "PLASTICINE": ("Plasticine", "Oil-based modelling clay: matte, handled, more dents",
                   (0.6, 0.15, 0.4, 0.6, 0.5, 0.35, 0.1, 0.4, 0.6)),
    "TERRACOTTA": ("Dry Clay", "Air-dry / terracotta: very matte, porous, no translucency",
                   (0.85, 0.0, 0.6, 0.5, 0.6, 0.5, 0.4, 0.5, 0.35)),
}
PRESET_KEYS = ("Roughness", "Subsurface", "Grain Intensity", "Imperfection", "Cavity",
               "Pores", "Specks", "Color Variation", "Fingerprints")


class PCLAY_OT_preset(Operator):
    bl_idname = "pclay.preset"
    bl_label = "Clay Preset"
    bl_description = "Set the surface sliders of the active clay material (color is kept)"
    bl_options = {"REGISTER", "UNDO"}

    preset: bpy.props.EnumProperty(
        name="Preset",
        items=[(k, v[0], v[1]) for k, v in PRESETS.items()])

    @classmethod
    def poll(cls, context):
        return _active_clay(context)[1] is not None

    def execute(self, context):
        mat, node = _active_clay(context)
        upgrade_clay_material(mat)
        node = find_clay_node(mat)
        for key, val in zip(PRESET_KEYS, PRESETS[self.preset][2]):
            if key in node.inputs:
                node.inputs[key].default_value = val
        mat.roughness = node.inputs["Roughness"].default_value
        return {"FINISHED"}


def _world_bounds(objs):
    from mathutils import Vector
    pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def _backdrop_mesh(name, width, depth, height, radius, floor_z, center_x, center_y):
    """Seamless 'cyclorama': floor that curves up into a back wall."""
    import math
    profile = []  # (y, z) from front edge to top of wall
    profile.append((-depth, 0.0))
    profile.append((depth * 0.5 - radius, 0.0))
    for i in range(1, 12):
        a = (math.pi / 2) * i / 12
        profile.append((depth * 0.5 - radius + math.sin(a) * radius, radius - math.cos(a) * radius))
    profile.append((depth * 0.5, radius))
    profile.append((depth * 0.5, height))
    xs = (-width / 2, width / 2)
    verts, faces = [], []
    for y, z in profile:
        for x in xs:
            verts.append((center_x + x, center_y + y, floor_z + z))
    for i in range(len(profile) - 1):
        a = i * 2
        faces.append((a, a + 1, a + 3, a + 2))
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    for poly in me.polygons:
        poly.use_smooth = True
    return me


class PCLAY_OT_studio_setup(Operator):
    bl_idname = "pclay.studio_setup"
    bl_label = "Studio Setup"
    bl_description = ("Build a product-shot scene around the selection: seamless backdrop, "
                      "soft key/fill/rim area lights, camera with depth of field, EEVEE settings. "
                      "Running it again rebuilds it")
    bl_options = {"REGISTER", "UNDO"}

    backdrop_color: FloatVectorProperty(
        name="Backdrop", subtype="COLOR", size=4, min=0.0, max=1.0,
        default=(0.0, 0.0, 0.0, 1.0),
        description="Black = derive a soft tint from the active clay color")
    light_power: FloatProperty(name="Light Power", default=1.0, min=0.05, max=10.0)
    dof: BoolProperty(name="Depth of Field", default=True)

    @classmethod
    def poll(cls, context):
        return any(o.type == "MESH" for o in context.selected_objects)

    def execute(self, context):
        import colorsys
        import math
        from mathutils import Vector

        scene = context.scene
        subjects = [o for o in context.selected_objects if o.type == "MESH"]
        lo, hi = _world_bounds(subjects)
        center = (lo + hi) / 2
        size = max((hi - lo).length, 0.01)

        # fresh collection each run (idempotent)
        coll = bpy.data.collections.get("Clay Studio")
        if coll is not None:
            for o in list(coll.objects):
                bpy.data.objects.remove(o, do_unlink=True)
        else:
            coll = bpy.data.collections.new("Clay Studio")
            scene.collection.children.link(coll)

        # backdrop color: pale, desaturated version of the clay
        bg = tuple(self.backdrop_color)
        if sum(bg[:3]) == 0.0:
            _, node = _active_clay(context)
            base = tuple(node.inputs["Clay Color"].default_value)[:3] if node else (0.7, 0.5, 0.5)
            h, s, v = colorsys.rgb_to_hsv(*base)
            bg = (*colorsys.hsv_to_rgb(h, min(1.0, s * 0.6), min(1.0, 0.2 + v * 0.45)), 1.0)
        bmat = bpy.data.materials.get("Clay Studio Backdrop") or bpy.data.materials.new("Clay Studio Backdrop")
        if bpy.app.version < (5, 0, 0):
            bmat.use_nodes = True
        pb = bmat.node_tree.nodes.get("Principled BSDF")
        if pb:
            pb.inputs["Base Color"].default_value = bg
            pb.inputs["Roughness"].default_value = 0.85
        bmat.diffuse_color = bg

        me = _backdrop_mesh("Clay Backdrop", size * 8, size * 8, size * 4, size * 1.2,
                            lo.z, center.x, center.y)
        me.materials.append(bmat)
        back = bpy.data.objects.new("Clay Backdrop", me)
        coll.objects.link(back)

        def add_light(name, offset, power, radius):
            ld = bpy.data.lights.new(name, "AREA")
            ld.shape = "DISK"
            ld.size = radius
            dist = offset.length
            ld.energy = power * dist * dist * self.light_power
            if hasattr(ld, "use_shadow_jitter"):
                ld.use_shadow_jitter = True  # soft shadows in the EEVEE viewport too
            ob = bpy.data.objects.new(name, ld)
            ob.location = center + offset
            ob.rotation_euler = (center - ob.location).to_track_quat("-Z", "Y").to_euler()
            coll.objects.link(ob)
            return ob

        s = size
        add_light("Clay Key", Vector((-1.4 * s, -1.6 * s, 1.8 * s)), 50.0, 2.5 * s)
        add_light("Clay Fill", Vector((1.8 * s, -1.2 * s, 0.8 * s)), 12.0, 3.5 * s)
        add_light("Clay Rim", Vector((0.6 * s, 1.6 * s, 1.6 * s)), 35.0, 1.5 * s)

        cam = scene.camera
        if cam is None:
            cd = bpy.data.cameras.new("Clay Camera")
            cam = bpy.data.objects.new("Clay Camera", cd)
            coll.objects.link(cam)
            scene.camera = cam
            cd.lens = 85
            direction = Vector((0.35, -1.0, 0.28)).normalized()
            fov = 2 * math.atan(cd.sensor_width / (2 * cd.lens))
            cam.location = center + direction * (0.62 * s / math.tan(fov / 2))
            cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
        cd = cam.data
        cd.dof.use_dof = self.dof
        cd.dof.focus_distance = (cam.location - center).length
        cd.dof.aperture_fstop = 2.8

        world = scene.world or bpy.data.worlds.new("World")
        scene.world = world
        world.color = tuple(c * 0.25 for c in bg[:3])

        try:
            scene.view_settings.view_transform = "AgX"
            scene.view_settings.look = "AgX - Medium High Contrast"
        except TypeError:
            pass
        bpy.ops.pclay.eevee_setup()
        self.report({"INFO"}, "Clay studio ready (camera view: Numpad 0)")
        return {"FINISHED"}


class PCLAY_OT_fp_load(Operator):
    bl_idname = "pclay.fp_load"
    bl_label = "Load Fingerprint Texture"
    bl_description = ("Use your own grayscale fingerprint/smudge texture (ideally seamless). "
                      "Applies to every clay material in the file")
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_image: BoolProperty(default=True, options={"HIDDEN"})
    filter_folder: BoolProperty(default=True, options={"HIDDEN"})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        group = _get_group(SHADER_GROUP, SHADER_VERSION, _build_shader_group)
        node = group.nodes.get(FP_NODE)
        try:
            img = bpy.data.images.load(self.filepath, check_existing=True)
        except RuntimeError as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        img.colorspace_settings.name = "Non-Color"
        node.image = img
        return {"FINISHED"}


class PCLAY_OT_fp_reset(Operator):
    bl_idname = "pclay.fp_reset"
    bl_label = "Built-in Fingerprints"
    bl_description = "Go back to the built-in generated fingerprint texture"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        group = _get_group(SHADER_GROUP, SHADER_VERSION, _build_shader_group)
        group.nodes.get(FP_NODE).image = get_fingerprint_image()
        return {"FINISHED"}


class PCLAY_OT_sync_viewport_color(Operator):
    bl_idname = "pclay.sync_viewport_color"
    bl_label = "Sync Solid View Color"
    bl_description = "Copy the clay color to the material's Solid-mode viewport color"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        mat, node = _active_clay(context)
        if node:
            mat.diffuse_color = node.inputs["Clay Color"].default_value
            mat.roughness = node.inputs["Roughness"].default_value
        return {"FINISHED"}


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

def _split_col(layout):
    col = layout.column()
    col.use_property_split = True
    col.use_property_decorate = False
    return col


class VIEW3D_PT_procedural_clay(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Clay"
    bl_label = "Procedural Clay"

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.scale_y = 1.3
        row.operator(PCLAY_OT_apply.bl_idname, icon="MATERIAL")
        row = layout.row(align=True)
        row.operator(PCLAY_OT_studio_setup.bl_idname, icon="LIGHT_AREA")
        row.operator(PCLAY_OT_eevee_setup.bl_idname, text="EEVEE Only", icon="SHADING_RENDERED")

        obj = context.active_object
        mat, node = _active_clay(context)
        mod = find_deform_mod(obj)

        if node is None and mod is None:
            layout.box().label(text="Active object has no clay", icon="INFO")
            return

        if mod is not None:
            box = layout.box()
            box.label(text="Shape", icon="MOD_NOISE")
            col = _split_col(box)
            col.prop(obj.pclay_deform, "intensity", text="Deformation")
        elif obj and obj.type == "MESH":
            layout.operator(PCLAY_OT_add_deform.bl_idname, icon="MOD_NOISE")

        if node is not None:
            box = layout.box()
            row = box.row(align=True)
            row.prop(mat, "name", text="", icon="MATERIAL")
            users = mat.users - (1 if mat.use_fake_user else 0)
            if users > 1:
                row.label(text=f"{users} users")
            box.operator_menu_enum(PCLAY_OT_preset.bl_idname, "preset", text="Preset", icon="PRESET")
            col = _split_col(box)
            for name in ("Clay Color", "Roughness", "Subsurface", "Grain Intensity", "Imperfection",
                         "Fingerprints"):
                if name in node.inputs:  # older materials may lack newer inputs
                    col.prop(node.inputs[name], "default_value", text=name)

        space = context.space_data
        if space and space.type == "VIEW_3D" and space.shading.type in {"SOLID", "WIREFRAME"}:
            layout.label(text="Material Preview shows the surface", icon="SHADING_TEXTURE")


class VIEW3D_PT_procedural_clay_stop(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Clay"
    bl_label = "Stop Motion"
    bl_parent_id = "VIEW3D_PT_procedural_clay"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.prop(context.scene.pclay_stop, "enabled", text="")

    def draw(self, context):
        sm = context.scene.pclay_stop
        col = _split_col(self.layout)
        col.active = sm.enabled
        col.prop(sm, "step")
        col.prop(sm, "boil")
        row = self.layout.row(align=True)
        row.operator(PCLAY_OT_step_animation.bl_idname, icon="IPO_CONSTANT")
        row.operator(PCLAY_OT_smooth_animation.bl_idname, text="", icon="IPO_BEZIER")


class VIEW3D_PT_procedural_clay_detail(Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Clay"
    bl_label = "Detail"
    bl_parent_id = "VIEW3D_PT_procedural_clay"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return (_active_clay(context)[1] is not None
                or find_deform_mod(context.active_object) is not None)

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        _, node = _active_clay(context)
        mod = find_deform_mod(obj)

        if mod is not None:
            layout.label(text="Shape", icon="MOD_NOISE")
            col = _split_col(layout)
            for prop in ("lump_size", "detail", "subdivisions"):
                col.prop(obj.pclay_deform, prop)

        if node is not None:
            layout.label(text="Surface", icon="MATERIAL")
            col = _split_col(layout)
            for name in ("Cavity", "Specks", "Print Size", "Imperfection Scale", "Grain Scale", "Pores",
                         "Color Variation", "Texture Scale"):
                if name in node.inputs:  # older materials may lack newer inputs
                    col.prop(node.inputs[name], "default_value", text=name)
            col.prop(node.inputs["Seed"], "default_value", text="Seed")
            fpn = node.node_tree.nodes.get(FP_NODE)
            if fpn is not None:
                row = layout.row(align=True)
                row.operator(PCLAY_OT_fp_load.bl_idname, text="Custom Prints", icon="IMAGE_DATA")
                row.operator(PCLAY_OT_fp_reset.bl_idname, text="", icon="LOOP_BACK")
            layout.operator(PCLAY_OT_sync_viewport_color.bl_idname, icon="SHADING_SOLID")

        layout.operator(PCLAY_OT_randomize_seed.bl_idname, text="Randomize Seeds",
                        icon="FILE_REFRESH")


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

classes = (
    PCLAY_StopMotion,
    PCLAY_OT_step_animation,
    PCLAY_OT_smooth_animation,
    PCLAY_DeformSettings,
    PCLAY_OT_apply,
    PCLAY_OT_add_deform,
    PCLAY_OT_randomize_seed,
    PCLAY_OT_eevee_setup,
    PCLAY_OT_preset,
    PCLAY_OT_fp_load,
    PCLAY_OT_fp_reset,
    PCLAY_OT_studio_setup,
    PCLAY_OT_sync_viewport_color,
    VIEW3D_PT_procedural_clay,
    VIEW3D_PT_procedural_clay_stop,
    VIEW3D_PT_procedural_clay_detail,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.pclay_deform = PointerProperty(type=PCLAY_DeformSettings)
    bpy.types.Scene.pclay_stop = PointerProperty(type=PCLAY_StopMotion)


def unregister():
    del bpy.types.Object.pclay_deform
    del bpy.types.Scene.pclay_stop
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
