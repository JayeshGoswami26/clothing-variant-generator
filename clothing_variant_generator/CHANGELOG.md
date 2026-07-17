# Changelog

## v3.1.0 -- Production hardening

Stabilization, cleanup and optimization pass. The tool is now scoped to
exactly one job: Base Body + Base Clothing + Target Bodies -> fitted
variants -> optional skin weights -> FBX -> Unity.

### Bug fixes

- **Skin weights were silently destroyed.** With both "Transfer Skin
  Weights" and "Delete Construction History" enabled, the skinCluster was
  created and then immediately deleted by the history delete, and
  "Freeze Transforms" errored on the (skinned) result. Cleanup now runs
  *before* skinning, so the three options work together.
- **Skinned FBX exported with no skeleton.** `FBXExportSkins` was off and
  only the mesh was selected, so a skinned variant arrived in Unity as an
  unrigged prop. Skins/shapes are now exported and each influence's full
  joint hierarchy travels with the mesh.
- **"Retry Failed Items" re-processed successful items**, re-exporting
  their FBX and creating duplicate meshes. A retry is now a clean pass
  over only the failed tasks.
- **"Load Clothing Folder" added every mesh in the scene** -- including
  the body meshes -- rather than only the meshes from the imported files.
- **Namespaced clothing crashed the rename** (`CVG_tmp_char:shirt_working`
  is not a legal node name). Node names are now sanitized.
- **Overwrite-disabled counted as a task failure.** It is now a logged
  skip, as documented.
- **Search filter was silently dropped** whenever the clothing list
  refreshed.
- **Drag-to-reorder did nothing** -- the widget reordered but the
  processing order did not. It is now wired up.

### Removed

- `adjacency.py` (folded into `processor.py`; Laplacian smoothing is kept).
- The in-memory log buffer (`VariantLogger.full_text` / `_lines`), which
  grew for the whole session and nothing read.
- Per-batch developer statistics (vertices/sec, live vertex counts).
- Dead wrap-deformer constants (`WRAP_ARGS`, `WRAP_DRIVER_SUFFIX`) left
  over from the pre-API-2.0 engine.
- The "Apply these Deformation Controls to Generate Variants" opt-in
  checkbox: the sliders now default to exact identity, so they are always
  safe to apply and the extra switch was noise.
- The Falloff slider, folded into a single artist-facing
  **Smooth Iterations** control.

### Window behaviour

- Opens **free-floating** (900x850, never smaller than 700x700), not
  docked across the top of Maya.
- Remembers its position and size between sessions; a position on a
  monitor that no longer exists is discarded instead of opening the
  window off-screen.
- **Open Docked** checkbox (default off) docks it vertically on the right.
- Never registers a `uiScript`, so Maya never resurrects the window on
  startup; leftover workspaceControls are cleaned up before rebuilding.

### Performance

- Viewport refresh is suspended for the duration of a batch and always
  restored -- on completion, cancel, error, or the window closing.
- Per-clothing bind data is released as soon as that clothing's last
  target is done, instead of accumulating for the whole batch.
- The manual deformation pass is skipped entirely when the sliders are at
  their defaults.
- Mesh point/normal arrays are marshalled from the Maya API into plain
  Python tuples once per mesh rather than crossing the C++ boundary
  nine times per vertex.

### Unchanged

The core deformation-transfer algorithm (barycentric closest-point
binding + triangle-local offset encoding) is byte-for-byte identical:
verified point-for-point against the previous implementation.

---

## v2.0.0

Deformation engine rewritten from Wrap deformer + BlendShape driver to a
direct `maya.api.OpenMaya` transfer: no `wrap`, no `blendShape`, no MEL,
and no temporary DG nodes to clean up.

Fixed a Maya startup crash caused by `show()` registering a `uiScript`
on its workspaceControl, which made Maya re-import and reopen the plugin
during UI-layout restore.
