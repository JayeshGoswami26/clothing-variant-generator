# -*- coding: utf-8 -*-
"""
deform_state.py
----------------
Plain-data container for the values the "Deformation Controls" panel
exposes: Global Body Influence, Global Surface Offset, Smoothing and
Falloff.

Kept deliberately free of any Qt or Maya-scene dependency so it can be
constructed/mutated by the UI sliders and consumed by processor.py's
evaluation step.

One ``DeformationState`` is shared for a whole session.
"""

from . import config


class DeformationState(object):

    def __init__(self):
        d = config.DEFORM_DEFAULTS
        self.global_influence = d["global_influence"]
        self.surface_offset = d["surface_offset"]
        self.smoothing = d["smoothing"]
        self.falloff = d["falloff"]

    # ------------------------------------------------------------------
    # Clamping helpers -- the UI already clamps via slider ranges, but
    # clamp again defensively here in case this is ever driven directly.
    # ------------------------------------------------------------------
    def set_global_influence(self, value):
        lo, hi = config.DEFORM_RANGES["global_influence"]
        self.global_influence = max(lo, min(hi, value))

    def set_surface_offset(self, value):
        lo, hi = config.DEFORM_RANGES["surface_offset"]
        self.surface_offset = max(lo, min(hi, value))

    def set_smoothing(self, value):
        lo, hi = config.DEFORM_RANGES["smoothing"]
        self.smoothing = max(lo, min(hi, value))

    def set_falloff(self, value):
        lo, hi = config.DEFORM_RANGES["falloff"]
        self.falloff = max(lo, min(hi, value))

    def reset_to_defaults(self):
        fresh = DeformationState()
        self.global_influence = fresh.global_influence
        self.surface_offset = fresh.surface_offset
        self.smoothing = fresh.smoothing
        self.falloff = fresh.falloff
