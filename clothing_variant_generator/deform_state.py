# -*- coding: utf-8 -*-
"""
deform_state.py
----------------
Plain-data container for the values the "Deformation Controls" panel
exposes: Global Body Influence, Surface Offset and Smooth Iterations.

Kept deliberately free of any Qt or Maya-scene dependency so it can be
constructed/mutated by the UI sliders, consumed by processor.py's
evaluation step, and serialised straight into the settings JSON.

One ``DeformationState`` is shared for a whole session.
"""

from . import config


class DeformationState(object):

    def __init__(self):
        d = config.DEFORM_DEFAULTS
        self.global_influence = d["global_influence"]
        self.surface_offset = d["surface_offset"]
        self.smooth_iterations = d["smooth_iterations"]

    # ------------------------------------------------------------------
    # Clamping setters -- the UI already clamps via slider ranges, but
    # clamp again defensively here because settings JSON is a plain text
    # file an artist (or a pipeline script) can hand-edit.
    # ------------------------------------------------------------------
    def set_global_influence(self, value):
        lo, hi = config.DEFORM_RANGES["global_influence"]
        self.global_influence = max(lo, min(hi, float(value)))

    def set_surface_offset(self, value):
        lo, hi = config.DEFORM_RANGES["surface_offset"]
        self.surface_offset = max(lo, min(hi, float(value)))

    def set_smooth_iterations(self, value):
        lo, hi = config.DEFORM_RANGES["smooth_iterations"]
        self.smooth_iterations = int(max(lo, min(hi, int(value))))

    # ------------------------------------------------------------------
    # Identity test
    # ------------------------------------------------------------------
    def is_identity(self):
        """
        True when these settings would leave the automatic transfer
        result untouched. processor.py checks this to skip the whole
        manual-deformation pass, so an artist who never moves a slider
        pays exactly zero cost for the panel existing.
        """
        return (
            self.global_influence == 1.0
            and self.surface_offset == 0.0
            and self.smooth_iterations <= 0
        )

    # ------------------------------------------------------------------
    # Serialisation (settings JSON)
    # ------------------------------------------------------------------
    def to_dict(self):
        return {
            "global_influence": self.global_influence,
            "surface_offset": self.surface_offset,
            "smooth_iterations": self.smooth_iterations,
        }

    def from_dict(self, data):
        """Load from a settings dict, ignoring anything malformed."""
        d = config.DEFORM_DEFAULTS
        try:
            self.set_global_influence(data.get("global_influence", d["global_influence"]))
            self.set_surface_offset(data.get("surface_offset", d["surface_offset"]))
            self.set_smooth_iterations(data.get("smooth_iterations", d["smooth_iterations"]))
        except (TypeError, ValueError):
            # A corrupt/hand-edited settings file must never stop the
            # tool from opening -- fall back to defaults.
            self.reset_to_defaults()

    def reset_to_defaults(self):
        d = config.DEFORM_DEFAULTS
        self.global_influence = d["global_influence"]
        self.surface_offset = d["surface_offset"]
        self.smooth_iterations = d["smooth_iterations"]
