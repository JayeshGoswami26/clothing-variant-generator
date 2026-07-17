# -*- coding: utf-8 -*-
"""
utils.py
--------
Low-level, single-purpose Maya helper functions shared by the processor,
exporter and UI layers. Nothing in this module knows about the overall
workflow -- it only wraps individual Maya operations defensively so that
callers get clear exceptions (or safe no-ops) instead of raw RuntimeErrors.
"""

import re
from contextlib import contextmanager

import maya.cmds as cmds

from . import config

TEMP_WILDCARD = config.TEMP_PREFIX + "*"


class MeshValidationError(Exception):
    """Raised when a mesh fails validation (missing, wrong type, etc.)."""
    pass


# ---------------------------------------------------------------------------
# Undo handling
# ---------------------------------------------------------------------------
@contextmanager
def undo_chunk(chunk_name="ClothingVariantOp"):
    """
    Wrap a block of Maya commands into a single undo chunk so an entire
    clothing/body-variant operation can be undone (or safely aborted) as
    one atomic step, and so Maya doesn't build a huge undo queue while
    batch-processing hundreds of meshes.
    """
    cmds.undoInfo(openChunk=True, chunkName=chunk_name)
    try:
        yield
    finally:
        cmds.undoInfo(closeChunk=True)


# ---------------------------------------------------------------------------
# Viewport suspension
#
# Redrawing the viewport after every duplicate/rename/delete during a
# batch costs far more than the geometry work itself. Suspending refresh
# for the duration of a batch is the single biggest speed win available
# without touching the deformation algorithm.
#
# These are module-level (not a context manager) because the batch is
# driven one task per QTimer tick, so it spans many callbacks and cannot
# live inside a single `with` block.
# ---------------------------------------------------------------------------
_viewport_suspended = False


def is_viewport_suspended():
    return _viewport_suspended


def suspend_viewport(logger=None):
    """Pause viewport redraw. Safe to call twice; never raises."""
    global _viewport_suspended
    if _viewport_suspended:
        return
    try:
        cmds.refresh(suspend=True)
        _viewport_suspended = True
    except RuntimeError as exc:
        if logger:
            logger.warning("Could not suspend viewport refresh: %s" % exc)


def resume_viewport(logger=None):
    """
    Resume viewport redraw and force one repaint. Safe to call twice, and
    safe to call when suspend never happened.

    The flag is cleared BEFORE the Maya call: if `refresh` itself throws,
    we must not stay latched in the "suspended" state, or every later
    resume would no-op and leave the artist staring at a frozen viewport
    for the rest of the session.
    """
    global _viewport_suspended
    if not _viewport_suspended:
        return
    _viewport_suspended = False
    try:
        cmds.refresh(suspend=False)
        cmds.refresh(force=True)
    except RuntimeError as exc:
        if logger:
            logger.warning("Could not resume viewport refresh: %s" % exc)


# ---------------------------------------------------------------------------
# Mesh validation
# ---------------------------------------------------------------------------
def node_exists_and_is_mesh(node_name):
    """True if node_name exists and is (or has) a polygon mesh shape."""
    if not node_name or not cmds.objExists(node_name):
        return False
    if cmds.nodeType(node_name) == "mesh":
        return True
    shapes = cmds.listRelatives(node_name, shapes=True, fullPath=True, type="mesh") or []
    return len(shapes) > 0


def get_mesh_shape(transform_name):
    """Return the non-intermediate mesh shape below a transform, or None."""
    if not cmds.objExists(transform_name):
        return None
    if cmds.nodeType(transform_name) == "mesh":
        return transform_name
    shapes = cmds.listRelatives(transform_name, shapes=True, fullPath=True, type="mesh") or []
    live_shapes = [s for s in shapes if not cmds.getAttr(s + ".intermediateObject")]
    if live_shapes:
        return live_shapes[0]
    return shapes[0] if shapes else None


def validate_mesh(node_name, label="mesh"):
    """Raise MeshValidationError with a human-readable message if invalid."""
    if not node_name:
        raise MeshValidationError("%s was not specified." % label)
    if not cmds.objExists(node_name):
        raise MeshValidationError("%s '%s' does not exist in the scene." % (label, node_name))
    if not node_exists_and_is_mesh(node_name):
        raise MeshValidationError("%s '%s' is not a valid polygon mesh." % (label, node_name))


def vertex_count(mesh_transform):
    shape = get_mesh_shape(mesh_transform)
    if not shape:
        return -1
    return cmds.polyEvaluate(shape, vertex=True)


def topology_matches(mesh_a, mesh_b):
    """
    Cheap topology-compatibility check between two meshes.

    ASSUMPTION / LIMITATION: proving true point-order identity between two
    meshes would require walking every vertex (or comparing a topology
    hash), which is expensive across hundreds of assets. In a body-variant
    pipeline where every body is exported from the same base rig/topology,
    vertex-count parity is a fast and, in practice, reliable proxy for the
    "identical point order" requirement of the transfer. If a studio's
    variants are NOT guaranteed same-point-order, this check should be
    swapped for a stricter topology hash comparison.
    """
    count_a = vertex_count(mesh_a)
    count_b = vertex_count(mesh_b)
    return count_a > 0 and count_a == count_b


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------
def unique_name(desired_name):
    """Return desired_name, or desired_name_1, _2, ... if it already exists."""
    if not cmds.objExists(desired_name):
        return desired_name
    index = 1
    candidate = "%s_%d" % (desired_name, index)
    while cmds.objExists(candidate):
        index += 1
        candidate = "%s_%d" % (desired_name, index)
    return candidate


def strip_namespace(node_name):
    """Return a node name without any namespace prefix or DAG path."""
    if not node_name:
        return node_name
    return node_name.split("|")[-1].split(":")[-1]


def sanitize_folder_component(name):
    """Make a string safe to use as a folder / file name component."""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", name)


def sanitize_node_name(name):
    """
    Make a string safe to use as a Maya node name.

    Maya node names may only contain letters, digits and underscores, and
    may not start with a digit. Namespaced/pathed source names ("char:shirt",
    "|grp|shirt") would otherwise produce an invalid rename target.
    """
    clean = re.sub(r"[^A-Za-z0-9_]", "_", strip_namespace(name) or "")
    if not clean:
        clean = "mesh"
    if clean[0].isdigit():
        clean = "_" + clean
    return clean


# ---------------------------------------------------------------------------
# Duplication / transform ops
# ---------------------------------------------------------------------------
def duplicate_mesh(source_name, new_name=None, world=True):
    """Duplicate a mesh transform and return the new transform's name."""
    dup = cmds.duplicate(source_name, renameChildren=False, upstreamNodes=False)[0]
    if world:
        parent = cmds.listRelatives(dup, parent=True, fullPath=True)
        if parent:
            dup = cmds.parent(dup, world=True)[0]
    if new_name:
        dup = cmds.rename(dup, unique_name(new_name))
    return dup


def safe_delete_history(node_name, logger=None):
    """Delete construction history on a node. Never raises."""
    try:
        cmds.delete(node_name, constructionHistory=True)
        return True
    except RuntimeError as exc:
        if logger:
            logger.warning("Could not delete history on '%s': %s" % (node_name, exc))
        return False


def safe_delete_nodes(node_names, logger=None):
    """Delete a list of nodes if they still exist. Never raises."""
    existing = [n for n in node_names if n and cmds.objExists(n)]
    if not existing:
        return
    try:
        cmds.delete(existing)
    except RuntimeError as exc:
        if logger:
            logger.warning("Could not delete temp nodes %s: %s" % (existing, exc))


def delete_leftover_temp_nodes(logger=None):
    """
    Delete any node still carrying the tool's temp prefix.

    Nothing should ever survive a normal run, but a Maya crash or a
    force-quit mid-batch can strand a working duplicate in the scene.
    Sweeping them at the start of a batch keeps a daily-driver scene from
    slowly filling with CVG_tmp_* junk.
    """
    try:
        leftovers = cmds.ls(TEMP_WILDCARD, long=True) or []
    except RuntimeError:
        return 0
    if not leftovers:
        return 0
    safe_delete_nodes(leftovers, logger)
    return len(leftovers)


def freeze_transform(node_name, logger=None):
    try:
        cmds.makeIdentity(node_name, apply=True, translate=True, rotate=True,
                          scale=True, normal=False, preserveNormals=True)
        return True
    except RuntimeError as exc:
        if logger:
            logger.warning("Could not freeze transform on '%s': %s" % (node_name, exc))
        return False


def center_pivot(node_name, logger=None):
    try:
        cmds.xform(node_name, centerPivots=True)
        return True
    except RuntimeError as exc:
        if logger:
            logger.warning("Could not center pivot on '%s': %s" % (node_name, exc))
        return False


# ---------------------------------------------------------------------------
# Skinning
# ---------------------------------------------------------------------------
def get_skin_cluster(mesh_transform):
    """Return the skinCluster node deforming a mesh, or None."""
    shape = get_mesh_shape(mesh_transform)
    if not shape:
        return None
    history = cmds.listHistory(shape, pruneDagObjects=False) or []
    skins = [n for n in history if cmds.nodeType(n) == "skinCluster"]
    return skins[0] if skins else None


def get_influences(skin_cluster):
    """Return the joint influences driving a skinCluster."""
    if not skin_cluster:
        return []
    return cmds.skinCluster(skin_cluster, query=True, influence=True) or []


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
@contextmanager
def preserved_selection():
    """
    Restore the artist's selection afterwards.

    The FBX exporter has to select the mesh it exports (FBXExport -s works
    on the selection). Silently stealing an artist's selection mid-batch is
    the kind of small rudeness that makes a tool feel unfinished.
    """
    try:
        previous = cmds.ls(selection=True, long=True) or []
    except RuntimeError:
        previous = []
    try:
        yield
    finally:
        try:
            if previous:
                cmds.select([n for n in previous if cmds.objExists(n)], replace=True)
            else:
                cmds.select(clear=True)
        except RuntimeError:
            pass


# ---------------------------------------------------------------------------
# Namespace cleanup
# ---------------------------------------------------------------------------
def remove_empty_namespaces():
    """Remove any empty namespaces left behind after a batch run."""
    try:
        all_ns = cmds.namespaceInfo(listOnlyNamespaces=True, recurse=True) or []
    except RuntimeError:
        return
    protected = {"UI", "shared"}
    for ns in sorted(all_ns, reverse=True):
        if ns in protected:
            continue
        try:
            contents = cmds.namespaceInfo(ns, listNamespace=True) or []
            if not contents:
                cmds.namespace(removeNamespace=ns)
        except RuntimeError:
            continue
