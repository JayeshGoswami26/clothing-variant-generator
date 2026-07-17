# -*- coding: utf-8 -*-
"""
exporter.py
-----------
Handles FBX export of finished clothing-variant meshes into a per-body-type
folder structure, using Maya's own FBX plugin (fbxmaya) via the FBXExport
MEL command (maya.cmds has no native FBX export call).
"""

import os

import maya.cmds as cmds
import maya.mel as mel

from . import config


class ExportError(Exception):
    pass


class FBXExporter(object):
    """
    Thin, defensive wrapper around Maya's FBX plugin.

    ASSUMPTION: the fbxmaya plugin ships with Maya 2022+ and is simply
    loaded on demand here. Studios with a custom FBX preset pipeline can
    extend `_apply_export_settings` to call additional FBXExport* MEL
    commands (smoothing groups, tangents, animation, etc.) in one place.
    """

    def __init__(self, logger=None):
        self.logger = logger
        self._plugin_loaded = False

    def _ensure_plugin(self):
        if self._plugin_loaded:
            return
        if not cmds.pluginInfo("fbxmaya", query=True, loaded=True):
            try:
                cmds.loadPlugin("fbxmaya")
            except RuntimeError as exc:
                raise ExportError("Could not load the fbxmaya plugin: %s" % exc)
        self._plugin_loaded = True

    def _apply_export_settings(self):
        """Reset FBX export options to sane, explicit defaults each time."""
        mel.eval('FBXResetExport')
        mel.eval('FBXExportSmoothingGroups -v 1')
        mel.eval('FBXExportHardEdges -v 0')
        mel.eval('FBXExportTangents -v 1')
        mel.eval('FBXExportInputConnections -v 0')
        mel.eval('FBXExportEmbeddedTextures -v 0')
        mel.eval('FBXExportInAscii -v 0')

    def export_mesh(self, mesh_name, output_folder, variant_folder_name,
                     overwrite=True, file_basename=None):
        """
        Export `mesh_name` to:
            <output_folder>/<variant_folder_name>/<file_basename or mesh_name>.fbx

        Returns the full path written. Raises ExportError on any failure;
        callers (processor.py) are expected to catch this per-asset so one
        failed export never halts the batch.
        """
        self._ensure_plugin()

        variant_folder = os.path.join(output_folder, variant_folder_name)
        try:
            if not os.path.isdir(variant_folder):
                os.makedirs(variant_folder)
        except OSError as exc:
            raise ExportError("Could not create export folder '%s': %s" % (variant_folder, exc))

        base_name = file_basename or mesh_name
        file_path = os.path.join(variant_folder, base_name + config.EXPORT_EXTENSION)

        if os.path.exists(file_path) and not overwrite:
            raise ExportError("File already exists and overwrite is disabled: %s" % file_path)

        if not cmds.objExists(mesh_name):
            raise ExportError("Mesh '%s' no longer exists; cannot export." % mesh_name)

        cmds.select(mesh_name, replace=True)
        mel_path = file_path.replace("\\", "/")

        try:
            self._apply_export_settings()
            mel.eval('FBXExport -f "%s" -s' % mel_path)
        except RuntimeError as exc:
            raise ExportError("FBX export failed for '%s': %s" % (mesh_name, exc))

        if not os.path.exists(file_path):
            raise ExportError(
                "FBX export reported no error but no file was written: %s" % file_path
            )

        if self.logger:
            self.logger.success("Exported '%s' -> %s" % (mesh_name, file_path))

        return file_path
