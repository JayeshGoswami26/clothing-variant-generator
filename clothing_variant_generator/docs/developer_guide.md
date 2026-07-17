# Developer Guide

## Architecture

```
ui.py         -- PySide2 dockable window. Gathers user intent only.
   |
   v
processor.py  -- ClothingVariantProcessor (per-task pipeline)
                 BatchRunner (task queue, progress, cancel, stats)
   |
   +--> utils.py     -- defensive Maya helper functions
   +--> exporter.py  -- FBXExporter (fbxmaya via MEL)
   +--> logger.py    -- VariantLogger (Qt signal + log.txt)
config.py     -- constants shared by every module
```

`ui.py` never calls `maya.cmds` for scene mutation directly except for
gathering selections/validating meshes for user feedback -- all actual
geometry processing goes through `processor.py` so it can be reused
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

## The deformation transfer technique

Maya has no single command that says "reshape this mesh the way body A
reshapes into body B". `processor._deform_clothing_to_target()` builds
that behavior out of two standard deformers:

1. **Wrap deformer** (`_create_wrap_deformer`): the clothing is bound to
   a duplicate of the Base body ("the wrap driver") using
   `doWrapArgList` (the same MEL routine Maya's own **Deform > Wrap**
   menu item calls -- `maya.cmds` has no first-class `wrap` command).
   Because the wrap driver starts out shaped exactly like the Base body
   (the shape the clothing was authored against), the bind has zero
   initial offset.

2. **BlendShape** (`_apply_body_delta`): a blendShape is added to the
   wrap driver duplicate with the *Target* body as its target shape,
   weight driven to `1.0`. Since Base and Target share identical vertex
   order, this reshapes the driver into the Target body's exact form.
   The wrap deformer, still bound to that driver, propagates the same
   delta onto the clothing -- the same principle as skin following a
   joint, but here a mesh follows another mesh's shape change.

3. `cmds.delete(..., constructionHistory=True)` bakes the result into
   plain vertex positions, after which every temporary node (wrap
   driver duplicate, wrap node, blendShape node, and Maya's own hidden
   wrap "base" shape) is deleted.

### Why not a direct blendShape on the clothing itself?

The clothing and the bodies do **not** share topology with each other
(a shirt has far fewer vertices than a full body), so a blendShape
cannot be created directly between clothing and body. The wrap
deformer is exactly the tool designed to let a low-vertex mesh follow
the shape changes of a different-topology influence mesh.

### Known limitations / assumptions (document before extending)

- **Topology-match check is vertex-count based**
  (`utils.topology_matches`). This is a fast proxy, not a proof of
  point-order identity. If your body variants are *not* guaranteed
  point-order-identical (e.g. sourced from different sculpts, not a
  shared blendShape rig), replace this check with a stricter topology
  hash before trusting the results.
- **Wrap deformer cleanup is best-effort.** Maya's wrap setup creates
  an internal hidden "base" mesh whose exact naming can vary slightly
  by Maya version/service pack. `_create_wrap_deformer` detects new
  mesh nodes created during the call and marks them for cleanup, which
  is robust in testing across Maya 2022-2024 but should be re-verified
  if Autodesk changes the wrap deformer's internal node graph.
- **Skin weight transfer assumes closest-point / closest-joint
  association** (`cmds.copySkinWeights`), which works well when Base
  and Target bodies share a skeleton and similar proportions. Extreme
  body types (e.g. "Monster" with extra limbs) may need a custom
  influence mapping -- extend `_transfer_skin_weights` with an explicit
  `influenceAssociation` list if so.
- **No true multi-threading.** `maya.cmds` is not thread-safe, so
  `BatchRunner.run_next()` is driven by a `QTimer` one task at a time
  from `ui.py`, rather than a Python `threading.Thread`. This keeps the
  UI responsive and Cancel effective between tasks without ever calling
  Maya API from a non-main thread.
- **Clothing-folder import** (`ui._on_load_clothing_folder`) only
  recognizes `.ma`/`.mb` files. Extend it if your pipeline stores
  clothing as `.fbx` or `.obj` references.

## Extending

- **New body-variant presets:** edit `config.DEFAULT_BODY_VARIANT_PRESETS`.
- **New export formats:** add a sibling to `exporter.FBXExporter` (e.g.
  `OBJExporter`) and let `ui.py`'s options section choose between them.
- **Custom deformation technique:** everything deformation-specific
  lives in `processor._deform_clothing_to_target` and its two helper
  methods -- swap in a different technique (e.g. a proximity wrap, or a
  vertex-delta transfer via `maya.api.OpenMaya` `MFnMesh.setPoints`)
  without touching `ui.py` or `exporter.py`.
- **Headless/batch-farm usage:** see the code sample above -- `processor.py`
  has zero PySide2/UI dependencies.

## Coding conventions used throughout

- Every Maya-mutating call that can throw `RuntimeError` is wrapped and
  converted into either a caught `ClothingProcessingError` /
  `MeshValidationError` (per-task, recoverable) or logged and
  swallowed (best-effort cleanup) -- nothing is allowed to propagate
  and crash Maya.
- `utils.undo_chunk()` wraps each clothing/target pair into one atomic
  undo chunk to keep Maya's undo queue from growing unbounded across a
  large batch.
- All temporary nodes are prefixed with `config.TEMP_PREFIX` so they are
  easy to spot (or scripted-delete) if a batch is interrupted outside
  the tool's own cleanup path (e.g. a Maya crash mid-batch).
