# Clothing Variant Generator

**A Maya plugin that automatically refits clothing across multiple character body types and batch-exports the results to FBX — built for game character customization pipelines.**

![Maya](https://img.shields.io/badge/Maya-2022%2B-0696D7?logo=autodesk&logoColor=white)
![Python](https://img.shields.io/badge/Python-3-3776AB?logo=python&logoColor=white)
![Qt](https://img.shields.io/badge/Qt-PySide2%20%2F%20PySide6-41CD52?logo=qt&logoColor=white)
![Status](https://img.shields.io/badge/status-production-brightgreen)

Author one clothing mesh fitted to a single **Base** body, and generate a
correctly-fitted variant for every other body type your game supports —
Skinny, Fat, Muscular, Heavy, or any custom body — in one batch. Optionally
carries the original skin weights across and exports straight to
per-variant FBX folders, ready to drop into Unity.

```
Base Body + Base Clothing + Target Bodies
    -> fitted clothing variant per body
    -> (optional) skin weight transfer
    -> FBX per variant folder
    -> Unity
```

## Why

Studios that support multiple body types per character normally re-fit
every clothing asset to every body type by hand, or build a custom rig per
garment. This plugin does it with a single closest-point + barycentric
deformation transfer, computed once per clothing mesh and reused across
every target body — no wrap deformers, no blendShapes, no manual
per-garment setup.

## Features

- **Batch generation** across any number of clothing meshes x any number
  of target bodies, with pause / resume / cancel / retry-failed and a live
  progress bar, ETA, and log.
- **Deformation Controls** (optional, off by default): Global Body
  Influence, Surface Offset, and Smooth Iterations, for the rare variant
  that needs a manual touch-up.
- **Skin weight transfer** from the original clothing onto every
  generated variant, skeleton included in the FBX export.
- **FBX export** into a clean `Output/<Variant>/<Clothing>_<Variant>.fbx`
  folder structure.
- **Save / load settings** as JSON — body assignments, clothing list,
  options and slider values.
- **A free-floating, resizable window** that never docks itself across
  your Maya layout uninvited, and remembers where you left it.

## Requirements

- Autodesk Maya 2022 or newer (Python 3). Maya 2022-2024 (PySide2/Qt5) and
  Maya 2025+ (PySide6/Qt6) are both supported.
- The `fbxmaya` plugin (ships with Maya; loaded automatically).
- Base body and every target body must share **identical topology and
  vertex order**. Clothing may have any topology of its own.

## Quick start

1. Copy the `clothing_variant_generator` folder into a directory on
   Maya's `PYTHONPATH` — see
   [`docs/installation.md`](clothing_variant_generator/docs/installation.md).
2. In Maya's Script Editor (Python tab):

   ```python
   import clothing_variant_generator.main as cvg_main
   cvg_main.show()
   ```

3. Select your Base body, add one or more Target bodies, add your
   clothing meshes, pick an output folder, and click **Generate Variants**.

## Documentation

| Guide | What's in it |
|---|---|
| [Installation](clothing_variant_generator/docs/installation.md) | Getting the plugin onto Maya's script path, shelf button, studio-wide module deployment |
| [Usage Guide](clothing_variant_generator/docs/usage_guide.md) | Full walkthrough of every panel and option, plus troubleshooting |
| [Developer Guide](clothing_variant_generator/docs/developer_guide.md) | How the deformation transfer algorithm works, architecture, headless/batch-farm usage, extension points |
| [Changelog](clothing_variant_generator/CHANGELOG.md) | Version history |

## Project structure

```
clothing_variant_generator/
├── config.py           # constants, naming conventions, defaults
├── logger.py            # live UI log + log.txt writer
├── utils.py              # defensive Maya helper functions
├── deform_state.py        # deformation slider state
├── processor.py            # the core deformation-transfer engine
├── exporter.py               # FBX export via Maya's fbxmaya plugin
├── ui.py                       # the PySide2/6 window
├── main.py                       # show() entry point
└── docs/                           # installation / usage / developer guides
```

## License

No license has been added yet — add one (MIT is a common default for
tooling like this) before accepting external contributions or wide
redistribution.
