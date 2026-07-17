# Usage Guide

## Overview

The Clothing Variant Generator takes one clothing mesh (fitted to a
"Base" body) and produces a matching clothing mesh for every "Target"
body you add -- Skinny, Fat, Muscular, Heavy, or any custom body type --
by transferring the shape difference between the Base body and each
Target body onto the clothing.

**Requirement:** every body (Base and all Targets) must share identical
topology and vertex order (i.e. they were all sculpted or deformed from
the same base mesh), and share the same skeleton hierarchy if you plan to
use skin-weight transfer.

## Step-by-step

1. **Open the tool**
   ```python
   import clothing_variant_generator.main as cvg_main
   cvg_main.show()
   ```
   The window opens free-floating, and reopens wherever you last left it.
   Tick **Open Docked** (top right) if you want it docked to the right-hand
   side next time you open it. You can also just drag it onto a dock
   yourself at any time.

2. **Base Body**
   - Select your base body mesh in the viewport/outliner.
   - Click **Select** in the Body section. The label updates to show
     the mesh name.

3. **Target Bodies**
   - Click **Add Target Body**.
   - Pick a preset (Skinny / Fat / Muscular / Heavy) or choose
     **Custom...** and type any name (Hero, Monster, Alien, etc.).
   - Select the corresponding body mesh in the scene, then click
     **Use Selected Mesh** inside the dialog, then **OK**.
   - Repeat for as many body types as you need -- there is no limit.
   - Select an entry and click **Remove Target Body** to delete it.

4. **Clothing**
   - Select one or more clothing meshes in the scene and click
     **Add Selected Clothing**, OR
   - Click **Load Clothing Folder** to import every `.ma`/`.mb` file in
     a folder and add the meshes those files bring in.
   - Use the search box to filter a long list.
   - Drag items within the list to change the processing order.
   - Select entries and click **Remove Selected**, or **Clear List** to
     start over.

5. **Output Folder**
   - Click **Browse...** and choose where FBX files (and `log.txt`)
     should be written. Each body variant gets its own subfolder:

     ```
     OutputFolder/
       log.txt
       Skinny/
         Shirt01_Skinny.fbx
       Fat/
         Shirt01_Fat.fbx
       Muscular/
         Shirt01_Muscular.fbx
     ```

6. **Options**
   - **Delete Construction History** -- bakes the deformation into plain
     vertex positions (recommended).
   - **Freeze Transforms** -- zeroes out transform values before export.
   - **Center Pivot** -- centers the pivot on the resulting mesh.
   - **Transfer Skin Weights** -- if the *original* clothing mesh already
     has a skinCluster, the same influences and weights are copied onto
     each generated variant, and the skeleton is included in the FBX so
     the variant arrives in Unity still rigged.
   - **Export FBX** -- writes an FBX per clothing/body pair. Requires an
     Output Folder.
   - **Overwrite Existing Files** -- if off, existing FBX files are left
     alone (and the skip is logged) instead of being replaced.
   - **Keep Scene Clean** -- deletes temporary nodes and empty namespaces
     created during processing.

7. **Deformation Controls** (optional)

   At their default values these do nothing at all -- the result is the
   fully automatic fit. Reach for them only when a specific variant needs
   help:

   - **Global Body Influence** (default 1.0) -- how strongly the clothing
     follows the target body. Below 1.0 hugs the body more tightly; above
     1.0 exaggerates the change.
   - **Surface Offset** (default 0.0) -- pushes the clothing outward along
     the body's surface normal. A small positive value is the usual fix
     for clothing poking through a body.
   - **Smooth Iterations** (default 0) -- relax passes over the fitted
     clothing, to soften pinching on extreme body shapes.
   - **Reset Sliders To Default** returns all three to the automatic fit.

8. **Generate Variants**
   - Click **Generate Variants**. The progress bar, current
     clothing/body labels, ETA and processed count update as each item
     finishes. The viewport stops redrawing while the batch runs -- that
     is deliberate, and it makes large batches considerably faster.
   - **Pause** stops after the current item; **Resume** continues.
   - **Cancel** stops the batch after the current item.
   - A failed item never stops the batch: it is logged and the run
     continues. **Retry Failed Items** re-runs just those.
   - The Log panel shows a live record; the same text is written to
     `log.txt` in the output folder.

9. **Save/Load Settings**
   - **Save Settings...** writes your Base body, Target bodies, clothing
     list, output folder, options and slider values to a `.json` file.
   - **Load Settings...** restores them later.
   - Your settings, window size and window position are also remembered
     automatically when you close the window, and restored next time.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| A clothing item is skipped with "Topology mismatch" | The target body doesn't have the same vertex count as the Base body. |
| "No skinCluster found" warning | Transfer Skin Weights was enabled but the *original* clothing mesh isn't skinned. |
| FBX export fails | The `fbxmaya` plugin couldn't load, or the output path isn't writable. Check `log.txt`. |
| Result looks unchanged from the Base body's clothing | The Target body mesh is actually a duplicate of the Base body's shape (no sculpted difference). |
| Clothing pokes through the body | Raise **Surface Offset** slightly, and/or add a couple of **Smooth Iterations**. |
| Nothing was exported for some items | **Overwrite Existing Files** is off and those files already exist -- check `log.txt` for the skip warnings. |
| The variant is unrigged in Unity | The original clothing mesh wasn't skinned, so there was nothing to transfer. Skin it first, then re-run with **Transfer Skin Weights** on. |
