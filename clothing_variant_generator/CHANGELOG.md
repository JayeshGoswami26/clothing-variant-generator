# Changelog

## v2.0.0

### Critical fix -- Maya startup crash (Part 7)
`show()` no longer passes `uiScript=` to the workspaceControl. That
argument is what caused Maya to silently re-import and reopen the
plugin every time Maya started up and restored its saved UI layout --
the root cause of the "close Maya, reopen Maya, immediate crash" bug.
The plugin now **only** ever opens via an explicit call:

```python
import clothing_variant_generator.main as cvg_main
cvg_main.show()
```

Running `show()` again brings the existing window forward / restores
it if minimized instead of creating a duplicate (Part 8). Closing the
window (via its own close box or the workspaceControl's) now releases
every timer, scriptJob, and temporary node the session created, and
discards any live Preview/Brush session (Part 9).

### New: Deformation Controls panel
- Global Body Influence, Surface Offset, Smoothing, Falloff sliders,
  plus one 0-2 slider per body region (Chest, Waist, Hip, Shoulders,
  Arms, Forearms, Hands, Legs, Calves, Feet, Neck). Region assignment
  is height/proportion-based (see `region_map.py`), so it works
  regardless of joint-naming conventions.
- **Preview / Accept / Discard**: Preview generates a temporary mesh;
  slider changes update that SAME mesh in place (no full rebuild) so
  dragging a slider stays responsive even on dense clothing.
- **Batch Generate stays fully automatic by default**, exactly like
  v1.0 -- there's an explicit "Apply these Deformation Controls to
  Generate Variants" checkbox an artist has to turn on to have the
  batch pass honor the panel. Preview always reflects the sliders.

### New: Brush Mode
Paints a per-vertex weight multiplier via Maya's own Artisan paint
tool (radius/strength/falloff/mirror/undo are Maya's native Artisan
features, reused rather than reimplemented) using a temporary
`cluster` deformer as the paint target; painted weights are read back
into the shared deformation state and the temporary cluster is deleted
when you exit Brush Mode.

### New: Presets
Save/Load/Delete/Rename slider configurations as JSON under Maya's
user app directory (`clothing_variant_generator_presets/`).

### New: Advanced Tools (all optional, off by default)
Collision Push, Shrink/Inflate, Relax (Laplacian), Volume Preserve,
Thickness Preserve, Normal Relax -- see `advanced_ops.py` for the exact
math and the documented approximations each one makes.

### New: Debug Mode
Draws closest-point mapping lines, flags vertices that hit a
degenerate-frame fallback during binding, and logs timing.

### UX / batch improvements
Pause / Resume / Retry Failed Items, plus live vertex count,
processing speed, and elapsed time in the Processing panel.

### Unchanged
Exporter, logger, config schema, batch folder structure, and the
core deformation-transfer algorithm (barycentric offset transfer) are
untouched -- v2.0 only adds optional layers on top.
