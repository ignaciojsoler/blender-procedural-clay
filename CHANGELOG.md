# Changelog

## 2.5.0
- **Fingerprints rebuilt as a texture**: a tileable 2048 map of overlapping finger smudges with real-looking loops/whorls, generated once with numpy (~10 s), cached on disk and packed into the .blend. Box-projected in object space (no UVs). Textures are mipmapped, so ridges never shimmer in EEVEE and cost one lookup. Shows mostly in the highlights (touched clay is slightly polished).
- **Custom Prints**: load your own seamless fingerprint texture (Detail panel), or go back to the built-in one.
- **Deform: adaptive subdivision**. Measures the mesh's mean edge length and only adds the levels it needs (none on meshes already subdivided). Before, a fixed 2 levels on top of a Subdivision Surface multiplied faces by 16 and blew up memory.
- **Deform: smoothed normals**. Displacement follows normals blurred over roughly a lump's width, so concave creases (inset + extrude, sockets, mouths) no longer tear.
- EEVEE setup uses a 256 MB shadow pool and half-resolution ray tracing (lighter on integrated GPUs that share system RAM).

## 2.4.0
- **Fingerprints**: scattered finger presses with whorl-like ridges (Voronoi-placed, random rotation/size, elliptical, broken up by noise). Two bumps: a soft press sized to the print and ridges sized to their wavelength.
- Ridges fade with camera distance before they get smaller than ~2-3 px, so they never shimmer in EEVEE; the soft press stays.
- **Print Size** is relative to each object's size. Presets include fingerprint amounts.

## 2.3.0
- **Studio Setup**: one click builds a product-shot scene around the selection: seamless curved backdrop tinted from the clay color, soft key/fill/rim area lights, 85mm camera with depth of field, AgX, EEVEE settings. Scales with the subject, re-running rebuilds it.
- **Presets**: Play-Doh, Smooth Clay, Plasticine, Dry Clay (surface sliders only, color is kept).
- **Subsurface** is back as a slider (default 0.3). Radius scales per object via a `pclay_size` object property, so small and large objects scatter alike.
- **Specks**: sparse light flecks in the clay.
- Softer defaults: Roughness 0.5, more sheen and specular.
- Apply Clay on an object that already has clay upgrades its material in place and keeps its settings.

## 2.2.0
- Deformation keeps the overall shape: lumps are sized relative to the object (default 15% instead of 50%), depth scales with lump size, and displacement is biased inward so the silhouette doesn't swell over attached parts.
- Softer, plasticine-like lumps (less noise detail, lower default Detail).
- Re-applying Clay upgrades existing Clay Deform modifiers to the current node group.

## 2.1.0
- Blender 5.2 support: Geometry Nodes modifier inputs are written through `modifier.properties.inputs` (5.2+) with a fallback to ID properties (≤ 5.1).
- Shape sliders are addon properties on the object that push values to the modifier, so the panel doesn't depend on how each Blender version exposes modifier inputs.

## 2.0.0
- **Clay Deform** Geometry Nodes modifier: subdivides and displaces along normals so the silhouette becomes lumpy (a bump map can't change the outline).
- EEVEE-focused shader: removed subsurface (EEVEE's screen-space blur washed out the grain), added **Cavity** (darkens dents/pores/grain in the albedo so detail reads under flat lighting), switched 4D textures to 3D with a hashed seed offset.
- **EEVEE Clay Setup** button: ray tracing, fast GI (horizon scan AO), soft viewport shadows.

## 1.0.0
- Procedural clay material with Object coordinates (no UVs), Noise + Voronoi + chained Bump.
- Sidebar panel: Clay Color, Roughness, Grain Intensity, Imperfection, plus detail controls.
