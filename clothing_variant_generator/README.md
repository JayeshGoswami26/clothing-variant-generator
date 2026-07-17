# Clothing Variant Generator

A production Maya (2022+, Python 3) plugin that automatically generates
clothing meshes fitted to multiple game-character body types (Skinny, Fat,
Muscular, Heavy, or any custom body you add), starting from a single
clothing mesh authored against one Base body.

- **Language:** Python 3, PySide2/PySide6, `maya.cmds` / `maya.api.OpenMaya` 2.0
- **Maya:** 2022 and newer (Qt5/PySide2 and Qt6/PySide6 both supported)
- **Output:** baked, renamed clothing meshes per body variant, optionally
  skinned via skin-weight transfer and exported to per-variant FBX folders

## The workflow

```
Base Body -> Base Clothing -> Target Bodies (Skinny / Fat / Muscular / ...)
    -> fitted clothing variant per body
    -> (optional) skin weight transfer
    -> FBX per variant folder
    -> Unity
```

That is the whole scope of this tool. It is not a sculpting application.

## Folder structure

```
clothing_variant_generator/
├── __init__.py         # package marker / version
├── config.py           # constants, naming conventions, defaults
├── logger.py           # live UI log + log.txt writer
├── utils.py            # small, defensive Maya helper functions
├── deform_state.py     # plain-data container for the slider values
├── processor.py        # the core body-to-body deformation transfer
├── exporter.py         # FBX export via Maya's fbxmaya plugin
├── ui.py               # dockable PySide2/6 window
├── main.py             # `show()` entry point for shelf buttons
└── docs/
    ├── installation.md
    ├── usage_guide.md
    └── developer_guide.md
```

## Quick start

1. Copy the `clothing_variant_generator` folder into a directory on
   Maya's `PYTHONPATH` (see `docs/installation.md`).
2. In Maya's Script Editor (Python tab):

   ```python
   import clothing_variant_generator.main as cvg_main
   cvg_main.show()
   ```

3. Select your Base body, add one or more Target bodies, add your
   clothing meshes, pick an output folder, and click **Generate Variants**.

The window opens free-floating at 900x850 and reopens wherever you last
left it. Tick **Open Docked** if you would rather it dock (vertically, on
the right) next time you open it.

See `docs/usage_guide.md` for the full workflow and `docs/developer_guide.md`
for how the deformation transfer works internally and how to extend it.

## Requirements for your meshes

- Base body and every Target body must share **identical topology and
  vertex order** (same base mesh, sculpted differently).
- Clothing may have any topology of its own, and must already be fitted
  to the Base body.
- For skin-weight transfer, the original clothing mesh must already be
  skinned, and the bodies must share a skeleton.
