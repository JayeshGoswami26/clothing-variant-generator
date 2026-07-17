# Clothing Variant Generator

A production-oriented Maya (2022+, Python 3) plugin that automatically
generates clothing meshes fitted to multiple game-character body types
(Skinny, Fat, Muscular, Heavy, or any custom body you add), starting from
a single clothing mesh authored against one Base body.

- **Language:** Python 3, PySide2, `maya.cmds` / `maya.api.OpenMaya` 2.0
- **Maya:** 2022 and newer
- **Output:** baked, renamed clothing meshes per body variant, optionally
  skinned via skin-weight transfer and exported to per-variant FBX folders

## Folder structure

```
clothing_variant_generator/
├── __init__.py        # package marker / version
├── config.py           # constants, naming conventions, defaults
├── logger.py           # live UI log + log.txt writer
├── utils.py             # small, defensive Maya helper functions
├── processor.py         # the core body-to-body deformation transfer
├── exporter.py           # FBX export via Maya's fbxmaya plugin
├── ui.py                  # dockable PySide2 window
├── main.py                 # `show()` entry point for shelf buttons
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

See `docs/usage_guide.md` for the full workflow and `docs/developer_guide.md`
for how the deformation transfer works internally and how to extend it.
