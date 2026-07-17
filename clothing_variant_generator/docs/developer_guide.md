# Developer Guide

## Architecture

```
ui.py           -- PySide2/6 dockable window. Gathers user intent only.
   |
   v
processor.py    -- ClothingVariantProcessor (per-task pipeline)
                   BatchRunner (task queue, progress, cancel, stats)
   |
   +--> utils.py        -- defensive Maya helper functions
   +--> exporter.py     -- FBXExporter (fbxmaya via MEL)
   +--> logger.py       -- VariantLogger (Qt signal + log.txt)
   +--> deform_state.py -- plain-data slider values
config.py       -- constants shared by every module
```

`ui.py` never calls `maya.cmds` for scene mutation directly except to
gather selections and validate meshes for user feedback -- all actual
geometry processing goes through `processor.py`, so it can be reused
headlessly (e.g. from a batch/farm script) without the UI at all:

```python
from clothing_variant_generator.logger import VariantLogger
from clothing_variant_generator.exporter import FBXExporter
from clothing_variant_generator.processor import ClothingVariantProcessor, BatchRunner

logger = VariantLogger()
exporter = FBXExporter(logger)
processor = ClothingVariantProcessor("BaseBody_Mesh", logger, exporter)

targets = [{"display_name": "Skinny", "mesh": "SkinnyBody_Mesh"},
           {"display_name": "Fat", "mesh": "FatBody_Mesh"}]
options = {
    "delete_history": True, "freeze_transforms": True, "center_pivot": True,
    "transfer_skin_weights": True, "export_fbx": True,
    "overwrite_existing": True, "keep_scene_clean": True,
}
runner = BatchRunner(processor, ["Shirt01", "Pants02"], targets, options,
                     "C:/exports", logger)
while not runner.is_finished():
    runner.run_next()
print(runner.stats_summary())
```

`processor.py` has zero PySide/UI dependencies. Deformation sliders are
optional: leave `processor.deform_state = None` for the fully automatic
result.

## The deformation transfer technique

Maya has no single command that says "reshape this mesh the way body A
reshapes into body B". The clothing and the bodies do **not** share
topology with each other (a shirt has far fewer vertices than a full
body), so a blendShape between them is impossible, and a wrap deformer
brings a whole DG subgraph that has to be built and torn down per asset.

Instead, `processor.py` computes the transfer directly on mesh data via
`maya.api.OpenMaya`. Nothing is added to the DG at all -- the only node
created is the result mesh itself.

### Bind (once per clothing mesh, cached across every target)

For every clothing vertex `V` in world space:

1. `MFnMesh.getClosestPoint` finds the closest point `P` on the Base
   body, and the polygon id containing it.
2. That polygon's triangles are enumerated (counts precomputed once via
   `MItMeshPolygon.numTriangles()`; `MFnMesh` has no per-polygon
   triangle-count method) and the triangle actually containing `P` is
   chosen -- or, for edge/corner hits, the one whose barycentric
   coordinates are least negative.
3. The barycentric coordinates `(u, v, w)` of `P` in triangle `(A, B, C)`
   are computed with the Ericson/Gram-matrix formulation.
4. A smooth normal `N` at `P` is interpolated from the base body's
   angle-weighted vertex normals. (Face normals would break at every
   triangle edge and stair-step the result.)
5. The offset `O = V - P` is expressed in the triangle-local basis
   `[E1 | E2 | N]`, where `E1 = B - A`, `E2 = C - A`, by solving
   `M * [a; b; h] = O` with Cramer's rule.

Stored per clothing vertex: `(a_idx, b_idx, c_idx, u, v, w, a, b, h)` --
9 floats + 3 ints, under a megabyte for a 20k-vertex mesh.

That basis is deliberately **not** orthonormalised. When a target
triangle stretches -- say across a fat belly -- `a*E1 + b*E2` stretches
with it, so a shirt widens instead of pinching; while `h` is measured
against a unit-length normal, so thickness is preserved.

### Evaluate (per target body, no closest-point queries)

```
P' = u*A' + v*B' + w*C'
N' = normalize(u*N'a + v*N'b + w*N'c)
V_new = P' + a*(B' - A') + b*(C' - A') + h*N'
```

The whole point set is written in one `MFnMesh.setPoints` call.

### Optional manual controls (`deform_state.py`)

Applied after evaluation, and skipped entirely when
`DeformationState.is_identity()`:

- **Global Body Influence** scales `V_new - P'` (0 collapses the clothing
  onto the body, 1 is the automatic fit, >1 exaggerates).
- **Surface Offset** adds `N' * offset` (an absolute distance, not a
  percentage) -- the fix for clothing poking through a body.
- **Smooth Iterations** runs Laplacian relax passes over the clothing's
  own vertex-neighbour graph (built once per mesh and cached alongside
  the bind data).

## Caching and memory

Two caches live on the processor instance (never at module level, so
nothing outlives the window):

- `_binding_cache` -- bind data per clothing mesh.
- `_neighbor_cache` -- vertex adjacency per clothing mesh, for smoothing.

`BatchRunner` orders tasks `for clothing: for target:` precisely so every
target for one clothing is consumed back-to-back. The expensive bind runs
once per clothing rather than once per clothing*target, and
`processor.release_clothing()` frees it the moment that clothing's last
target completes. `release_all()` is called on scene change, base-body
change and window close.

## Known limitations / assumptions

- **Topology-match check is vertex-count based**
  (`utils.topology_matches`). A fast proxy, not a proof of point-order
  identity. If your body variants are not guaranteed point-order-identical
  (e.g. sourced from separate sculpts), replace this with a topology hash
  before trusting the results.
- **Skin weight transfer assumes closest-point / closest-joint
  association** (`cmds.copySkinWeights`), which works well when Base and
  Target share a skeleton and similar proportions. Extreme bodies (a
  "Monster" with extra limbs) may need an explicit influence mapping in
  `_transfer_skin_weights`.
- **Option order is load-bearing.** History delete and freeze must run
  before the skin transfer: `delete -constructionHistory` would remove the
  new skinCluster, and `makeIdentity` refuses to freeze a skinned mesh.
- **No true multi-threading.** `maya.cmds` is not thread-safe, so
  `BatchRunner.run_next()` is driven one task per `QTimer` tick from
  `ui.py` rather than a `threading.Thread`. This keeps the UI responsive
  and Cancel effective between tasks without ever calling Maya from a
  non-main thread.
- **Clothing-folder import** (`ui._on_load_clothing_folder`) only
  recognizes `.ma`/`.mb`. It adds only meshes reported by
  `cmds.file(..., returnNewNodes=True)`, so it will not sweep up the body
  meshes already in the scene.
- **Viewport refresh is suspended for the whole batch.** Any new early
  return in the batch path must still reach `utils.resume_viewport()`, or
  the artist is left with a frozen viewport.

## Extending

- **New body-variant presets:** edit `config.DEFAULT_BODY_VARIANT_PRESETS`.
- **New export formats:** add a sibling to `exporter.FBXExporter` (e.g.
  `OBJExporter`) and let `ui.py`'s options section choose between them.
- **Studio FBX presets:** extend `FBXExporter._apply_export_settings` --
  every `FBXExport*` MEL call lives in that one method.
- **Custom deformation technique:** everything deformation-specific lives
  in `processor._deform_clothing_to_target` and its bind/evaluate helpers;
  swap it out without touching `ui.py` or `exporter.py`.

## Coding conventions used throughout

- Every Maya-mutating call that can throw `RuntimeError` is wrapped and
  converted into either a caught `ClothingProcessingError` /
  `MeshValidationError` / `ExportError` (per-task, recoverable) or logged
  and swallowed (best-effort cleanup). Nothing is allowed to propagate and
  crash Maya, and one bad asset never stops a batch.
- `utils.undo_chunk()` wraps each clothing/target pair into one atomic
  undo chunk, so the undo queue doesn't grow unbounded across a batch.
- All temporary nodes are prefixed with `config.TEMP_PREFIX`, so debris
  from an interrupted batch is easy to find -- `utils.delete_leftover_temp_nodes()`
  sweeps them at the start of each run.
