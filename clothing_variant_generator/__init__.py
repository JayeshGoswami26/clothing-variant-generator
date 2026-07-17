# -*- coding: utf-8 -*-
"""
Clothing Variant Generator
---------------------------
A Maya plugin for automatically generating clothing variants that fit
multiple body types in a game character customization pipeline.

    Base Body + Base Clothing + Target Bodies
        -> fitted clothing variant per body
        -> (optional) skin weight transfer
        -> FBX per variant folder
        -> Unity

See main.py for the entry point used by shelf buttons.
"""

from . import config  # noqa: F401

__version__ = config.PLUGIN_VERSION
