# -*- coding: utf-8 -*-
"""
exporter.py
-----------
Handles FBX export of finished clothing-variant meshes into a per-body-type
folder structure, using Maya's own FBX plugin (fbxmaya) via the FBXExport
MEL command (maya.cmds has no native FBX export call).

Output layout:

    Output/
        Skinny/
            Shirt01_Skinny.fbx
        Fat/
            Shirt01_Fat.fbx
        Muscular/
            Shirt01_Muscular.fbx
"""

import os

import maya.cmds as cmds
import maya.mel as mel

from . import config
from . import utils


class ExportError(Exception):
    pass


class FBXExporter(object):
    """
    Thin, defensive wrapper around Maya's FBX plugin.

    ASSUMPTION: the fbxmaya plugin ships with Maya 2022+ and is simply
    loaded on demand here. Studios with a custom FBX preset pipeline can
    extend `_apply_export_settings` to call additional FBXExport* MEL
    commands (animation, cameras, custom axis conversion, etc.) in one
    place.
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
        # Skins and shapes must both be on for a skinned variant to arrive
        # in Unity still bound to its joints; with them off the mesh
        # exports as a static prop and the whole "Transfer Skin Weights"
        # step is silently wasted.
        mel.eval('FBXExportSkins -v 1')
        mel.eval('FBXExportShapes -v 1')
        mel.eval('FBXExportInputConnections -v 0')
        mel.eval('FBXExportEmbeddedTextures -v 0')
        mel.eval('FBXExportInAscii -v 0')

    def _export_selection_for(self, mesh_name):
        """
        Return the node list to select for export: the mesh, plus its skin
        influences (the joints) when it is skinned.

        `FBXExport -s` exports exactly what is selected. A skinned mesh
        selected on its own exports with no skeleton, so Unity receives an
        unrigged mesh -- selecting the influences alongside it is what
        makes the skeleton travel with the clothing.
        """
        nodes = [mesh_name]
        skin = utils.get_skin_cluster(mesh_name)
        if not skin:
            return nodes, False

        influences = utils.get_influences(skin)
        if not influences:
            return nodes, False

        # Include every influence AND all of its ancestors: exporting a
        # hand joint without the arm and root above it would produce a
        # rootless, broken skeleton in Unity. The full ancestor chain is
        # read straight off each joint's DAG path ("|root|spine|hand"
        # -> root, spine, hand) rather than by walking listRelatives,
        # which only reports one level at a time.
        wanted = set()
        for joint in influences:
            long_names = cmds.ls(joint, long=True) or []
            if not long_names:
                continue
            parts = [p for p in long_names[0].split("|") if p]
            for depth in range(len(parts)):
                wanted.add("|" + "|".join(parts[:depth + 1]))

        nodes.extend(sorted(wanted))
        return nodes, True

    def export_mesh(self, mesh_name, output_folder, variant_folder_name,
                    overwrite=True, file_basename=None):
        """
        Export `mesh_name` to:
            <output_folder>/<variant_folder_name>/<file_basename or mesh_name>.fbx

        Returns the full path written, or None if the file already existed
        and `overwrite` is False (a deliberate skip, not a failure).

        Raises ExportError on any real failure; callers (processor.py) are
        expected to catch this per-asset so one failed export never halts
        the batch.
        """
        self._ensure_plugin()

        variant_folder = os.path.join(output_folder, variant_folder_name)
        try:
            if not os.path.isdir(variant_folder):
                os.makedirs(variant_folder)
        except OSError as exc:
            raise ExportError("Could not create export folder '%s': %s" % (variant_folder, exc))

        base_name = file_basename or utils.strip_namespace(mesh_name)
        file_path = os.path.join(variant_folder, base_name + config.EXPORT_EXTENSION)

        if os.path.exists(file_path) and not overwrite:
            # A skip, not an error: the artist turned Overwrite off and the
            # UI already warned that existing files would be left alone.
            if self.logger:
                self.logger.warning(
                    "Skipped export (file exists, overwrite disabled): %s" % file_path
                )
            return None

        if not cmds.objExists(mesh_name):
            raise ExportError("Mesh '%s' no longer exists; cannot export." % mesh_name)

        nodes, skinned = self._export_selection_for(mesh_name)
        mel_path = file_path.replace("\\", "/")

        with utils.preserved_selection():
            try:
                cmds.select(nodes, replace=True)
            except RuntimeError as exc:
                raise ExportError("Could not select '%s' for export: %s" % (mesh_name, exc))

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
            self.logger.success(
                "Exported '%s'%s -> %s"
                % (mesh_name, " (with skeleton)" if skinned else "", file_path)
            )

        return file_path
