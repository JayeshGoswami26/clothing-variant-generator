# -*- coding: utf-8 -*-
"""
config.py
---------
Central configuration and constants for the Clothing Variant Generator.

Keeping every "magic value" in one module makes the plugin easy to tune
or re-skin for a different studio pipeline without touching logic code.
"""

PLUGIN_NAME = "Clothing Variant Generator"
PLUGIN_VERSION = "2.0.0"
PLUGIN_OBJECT_NAME = "clothingVariantGeneratorWindow"

# ---------------------------------------------------------------------------
# Default body variant presets offered in the "Add Target Body" dialog.
# Users are never limited to this list -- "Custom..." lets them name anything.
# ---------------------------------------------------------------------------
DEFAULT_BODY_VARIANT_PRESETS = [
    "Skinny",
    "Fat",
    "Muscular",
    "Heavy",
]

# ---------------------------------------------------------------------------
# Naming / temp node conventions
# ---------------------------------------------------------------------------
TEMP_PREFIX = "CVG_tmp_"
WRAP_DRIVER_SUFFIX = "_wrapDriver"
WORKING_CLOTHING_SUFFIX = "_working"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILENAME = "log.txt"

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
EXPORT_EXTENSION = ".fbx"

# ---------------------------------------------------------------------------
# Default processing options (mirrors the checkboxes in the UI)
# ---------------------------------------------------------------------------
DEFAULT_OPTIONS = {
    "delete_history": True,
    "freeze_transforms": True,
    "center_pivot": True,
    "transfer_skin_weights": False,
    "export_fbx": True,
    "overwrite_existing": False,
    "keep_scene_clean": True,
}

# ---------------------------------------------------------------------------
# Settings persistence (Save/Load project settings as JSON)
# ---------------------------------------------------------------------------
SETTINGS_FILE_FILTER = "JSON Files (*.json)"
DEFAULT_SETTINGS_FILENAME = "clothing_variant_settings.json"

# Wrap deformer creation args used with MEL's doWrapArgList.
# Order: exclusiveBind, autoWeightThreshold, falloffMode, maxDistance,
#        wrapSamples, bindToOriginalGeometry, softNormalization
# These are Maya's own defaults for a generic (non-NURBS, non-lattice) wrap
# and work well for high-resolution game clothing meshes wrapped to a body.
WRAP_ARGS = ["7", "1", "0", "1", "2", "1", "0", "0"]

# ---------------------------------------------------------------------------
# Manual deformation controls (Deformation Controls panel)
# ---------------------------------------------------------------------------
DEFORM_DEFAULTS = {
    "global_influence": 1.0,   # 0.0 - 2.0
    "surface_offset": 0.0,     # -5.0 .. 5.0 (cm)
    "smoothing": 20.0,         # 0 - 100
    "falloff": 0.5,            # 0 - 1
}

DEFORM_RANGES = {
    "global_influence": (0.0, 2.0),
    "surface_offset": (-5.0, 5.0),
    "smoothing": (0.0, 100.0),
    "falloff": (0.0, 1.0),
}
