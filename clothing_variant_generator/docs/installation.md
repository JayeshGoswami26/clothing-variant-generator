# Installation

## Requirements

- Autodesk Maya 2022 or newer (ships with Python 3 and PySide2 by default)
- The `fbxmaya` plugin (ships with Maya; the plugin loads it on demand,
  no manual setup required)

## 1. Copy the package

Place the entire `clothing_variant_generator` folder (the one containing
`__init__.py`) inside a directory Maya can import Python packages from.
Common choices:

- **Per-user scripts folder** (recommended for a single artist):
  - Windows: `Documents/maya/scripts/`
  - macOS: `~/Library/Preferences/Autodesk/maya/scripts/`
  - Linux: `~/maya/scripts/`
- **Studio pipeline folder** already on `PYTHONPATH` via a module file or
  `Maya.env` entry (recommended for a team).

After copying, you should have:

```
.../scripts/clothing_variant_generator/__init__.py
.../scripts/clothing_variant_generator/config.py
.../scripts/clothing_variant_generator/ui.py
... etc.
```

`.../scripts/` itself does **not** need an `__init__.py` -- Maya adds the
user scripts folder to `sys.path` automatically, and
`clothing_variant_generator` is imported as a normal package from there.

## 2. Verify the import

In Maya's Script Editor (Python tab):

```python
import clothing_variant_generator.main as cvg_main
cvg_main.show()
```

If you edit the source after Maya is already running, reload with:

```python
import importlib
import clothing_variant_generator
import clothing_variant_generator.main as cvg_main
importlib.reload(clothing_variant_generator.config)
importlib.reload(clothing_variant_generator.utils)
importlib.reload(clothing_variant_generator.logger)
importlib.reload(clothing_variant_generator.processor)
importlib.reload(clothing_variant_generator.exporter)
importlib.reload(clothing_variant_generator.ui)
importlib.reload(cvg_main)
cvg_main.show()
```

## 3. Create a shelf button

1. Select a shelf tab (or create a new one) in Maya's Shelf UI.
2. Menu: **Windows > General Editors > Script Editor**.
3. Paste into the Python tab:

   ```python
   import clothing_variant_generator.main as cvg_main
   cvg_main.show()
   ```

4. Select the text, then **File > Save Script to Shelf...** and give it a
   name (e.g. `CVG`). A shelf button is created that opens the tool with
   one click.

Optionally set a custom icon via the shelf button's right-click **Edit...**
dialog, pointing "Icon Name" at any 32x32 PNG in your icons path.

## 4. Studio-wide deployment (module file, optional)

For distributing to a whole team, create a `.mod` file, e.g.
`clothing_variant_generator.mod`, in a folder listed under
`MAYA_MODULE_PATH`:

```
+ clothing_variant_generator 1.0 /path/to/parent/of/clothing_variant_generator
```

This lets every artist load the tool without manually copying files to
their personal scripts folder.
