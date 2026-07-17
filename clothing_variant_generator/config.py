# -*- coding: utf-8 -*-
"""
config.py
---------
Central configuration and constants for the Clothing Variant Generator.

Keeping every "magic value" in one module makes the plugin easy to tune
or re-skin for a different studio pipeline without touching logic code.
"""

PLUGIN_NAME = "Clothing Variant Generator"
PLUGIN_VERSION = "3.1.0"
PLUGIN_OBJECT_NAME = "clothingVariantGeneratorWindow"

# The workspaceControl Maya wraps the dockable window in. Derived once
# here so ui.py never rebuilds this string by hand in three places.
WORKSPACE_CONTROL_NAME = PLUGIN_OBJECT_NAME + "WorkspaceControl"

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
# Every node the tool creates while working is prefixed with this, so an
# interrupted batch (e.g. a Maya crash) leaves behind nodes an artist can
# find and delete with one wildcard select.
TEMP_PREFIX = "CVG_tmp_"
WORKING_CLOTHING_SUFFIX = "_working"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FILENAME = "log.txt"
# Hard cap on lines kept in the UI log widget. A 500-asset batch emits
# tens of thousands of lines; without a cap the QTextEdit document grows
# until Maya feels sluggish. log.txt on disk is never truncated.
LOG_VIEW_MAX_LINES = 5000

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
# Bumped whenever the on-disk schema changes shape. Older files still
# load -- every reader uses .get() with a default -- this is purely so a
# future migration can tell generations apart.
SETTINGS_VERSION = 3

# ---------------------------------------------------------------------------
# Window behaviour
# ---------------------------------------------------------------------------
# Deliberately small. Every section lives inside a QScrollArea, so the
# window stays usable when shrunk right down to a slim strip beside the
# viewport -- an artist should never have to give up half of Maya to keep
# this tool open. The default size is the comfortable "working" size.
WINDOW_MIN_WIDTH = 380
WINDOW_MIN_HEIGHT = 300
WINDOW_DEFAULT_WIDTH = 900
WINDOW_DEFAULT_HEIGHT = 850

# The tool opens as a genuine free-floating window: a normal top-level
# window owned by Maya, NOT a workspaceControl. That is what keeps Maya
# from restoring it into whatever dock strip it last occupied -- a
# workspaceControl persists its dock position in the workspace prefs and
# will happily re-dock itself across the top of Maya on the next launch,
# ignoring `floating=True`.
#
# Docking is opt-in only (the "Open Docked" checkbox), and docks
# vertically down the right-hand side.
WINDOW_OPEN_DOCKED_DEFAULT = False
WINDOW_DOCK_AREA = "right"

# ---------------------------------------------------------------------------
# Manual deformation controls (Deformation Controls panel)
#
# The defaults below are deliberately the IDENTITY of the deformation
# pass: influence 1.0 keeps each vertex exactly where the automatic
# transfer put it, offset 0.0 adds nothing, and 0 smoothing iterations
# do nothing. So an artist who never touches these sliders gets exactly
# the fully-automatic result, and processor.py skips the whole pass.
# ---------------------------------------------------------------------------
DEFORM_DEFAULTS = {
    "global_influence": 1.0,    # 0.0 - 2.0
    "surface_offset": 0.0,      # -5.0 .. 5.0 (scene linear units, usually cm)
    "smooth_iterations": 0,     # 0 - 10 Laplacian relax passes
}

DEFORM_RANGES = {
    "global_influence": (0.0, 2.0),
    "surface_offset": (-5.0, 5.0),
    "smooth_iterations": (0, 10),
}

# Blend factor applied per Laplacian smoothing iteration. Exposed here
# rather than inline so a studio can tune smoothing "feel" without the
# artist-facing slider growing a second knob.
SMOOTH_STRENGTH_PER_ITERATION = 0.5
