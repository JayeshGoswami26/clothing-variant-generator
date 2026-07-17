# -*- coding: utf-8 -*-
"""
main.py
-------
Entry point for the Clothing Variant Generator.

Typical usage from a Maya shelf button or the Script Editor:

    import clothing_variant_generator.main as cvg_main
    cvg_main.show()

See docs/installation.md for how to get the package onto Maya's
PYTHONPATH / scripts folder, and docs/usage_guide.md for the shelf
button snippet.
"""

from . import ui


def show():
    """Open (or bring forward) the Clothing Variant Generator window."""
    return ui.show()


if __name__ == "__main__":
    show()
