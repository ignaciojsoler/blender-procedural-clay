# Changelog

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
