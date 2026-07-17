# Usage Guide

## Overview

The Clothing Variant Generator takes one clothing mesh (fitted to a
"Base" body) and produces a matching clothing mesh for every "Target"
body you add -- Skinny, Fat, Muscular, Heavy, or any custom body type --
by transferring the shape difference between the Base body and each
Target body onto the clothing.

**Requirement:** every body (Base and all Targets) must share identical
topology and vertex order (i.e. they were all exported/posed from the
same base mesh, just sculpted or deformed differently), and share the
same skeleton hierarchy if you plan to use skin-weight transfer.

## Step-by-step

1. **Open the tool**
   ```python
   import clothing_variant_generator.main as cvg_main
   cvg_main.show()
   ```

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
     a folder and auto-add the resulting meshes.
   - Use the search box to filter a long list.
   - Select entries and click **Remove Selected**, or **Clear List** to
     start over.
   - Drag items within the list to reorder processing order (useful for
     controlling which assets are processed first if you plan to cancel
     partway through a large batch).

5. **Output Folder**
   - Click **Browse...** and choose where FBX files (and `log.txt`)
     should be written. Each body variant gets its own subfolder:

     ```
     OutputFolder/
       Skinny/
         Shirt01_Skinny.fbx
       Fat/
         Shirt01_Fat.fbx
       Muscular/
         Shirt01_Muscular.fbx
     ```

6. **Options**
   - **Delete Construction History** -- bakes the deformation into plain
     vertex positions (recommended; leave on unless you have a reason
     to keep history for further scene work).
   - **Freeze Transforms** -- zeroes out transform values before export.
   - **Center Pivot** -- centers the pivot on the resulting mesh.
   - **Transfer Skin Weights (optional)** -- if the *original* clothing
     mesh already has a skinCluster, the same influences and weights are
     copied onto each generated variant so it is export-ready for
     Unity's mesh renderer / rig.
   - **Export FBX** -- writes an FBX per clothing/body pair. Requires an
     Output Folder.
   - **Overwrite Existing Files** -- if off, existing FBX files are
     skipped (with a warning) instead of being replaced.
   - **Keep Scene Clean** -- deletes every temporary helper node/mesh
     created during processing (wrap driver duplicates, blendShape
     nodes) and empty namespaces once the batch finishes.

7. **Generate Variants**
   - Click **Generate Variants**. The progress bar, current
     clothing/body labels, ETA and processed count update as each item
     finishes.
   - Click **Cancel** at any time to stop after the current item.
   - The Log panel shows a live line-by-line record; the same text is
     written to `log.txt` in the output folder.

8. **Save/Load Settings**
   - **Save Settings...** writes your Base body, Target bodies,
     clothing list, output folder and options to a `.json` file.
   - **Load Settings...** restores them later (or on another machine
     with the same scene setup). The tool also auto-loads your last
     used settings on startup.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| A clothing item is skipped with "Topology mismatch" | The target body doesn't have the same vertex count as the Base body. |
| "No skinCluster found" warning | Transfer Skin Weights was enabled but the *original* clothing mesh isn't skinned. |
| FBX export fails | The `fbxmaya` plugin couldn't load, or the output path isn't writable. Check `log.txt`. |
| Result looks unchanged from the Base body's clothing | The Target body mesh selected in "Add Target Body" is actually a duplicate of the Base body's shape (no sculpted difference). |
