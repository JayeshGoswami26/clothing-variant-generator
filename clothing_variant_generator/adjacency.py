# -*- coding: utf-8 -*-
"""
adjacency.py
------------
Builds and caches a per-vertex neighbour list for a mesh, using the
Maya API 2.0 ``MItMeshVertex`` iterator. This is the shared building
block for:

    * Smoothing (Deformation Controls -> Smoothing slider)
    * Laplacian Relax (Advanced Tools)
    * Brush "Smooth" / "Relax" modes (Brush Mode)

Building the neighbour list is an O(vertices) walk done ONCE per mesh
and cached by mesh name, exactly like processor.py caches clothing
bindings -- so repeated smoothing passes (every slider tick, every
brush stroke) never re-walk mesh topology.
"""

import maya.api.OpenMaya as om


class AdjacencyCache(object):

    def __init__(self):
        self._cache = {}  # mesh_name -> list[list[int]]

    def neighbors_for(self, mesh_name):
        cached = self._cache.get(mesh_name)
        if cached is not None:
            return cached
        neighbors = self._build(mesh_name)
        self._cache[mesh_name] = neighbors
        return neighbors

    def invalidate(self, mesh_name=None):
        if mesh_name is None:
            self._cache.clear()
        else:
            self._cache.pop(mesh_name, None)

    @staticmethod
    def _build(mesh_name):
        sel = om.MSelectionList()
        sel.add(mesh_name)
        dag = sel.getDagPath(0)
        if dag.apiType() != om.MFn.kMesh:
            dag.extendToShape()

        vert_iter = om.MItMeshVertex(dag)
        neighbors = [None] * vert_iter.count()
        while not vert_iter.isDone():
            idx = vert_iter.index()
            neighbors[idx] = list(vert_iter.getConnectedVertices())
            vert_iter.next()
        return neighbors


default_cache = AdjacencyCache()


def laplacian_smooth(points, neighbors, amount, iterations=1):
    """
    In-place-style Laplacian smoothing of an ``MPointArray``-like list of
    (x, y, z) tuples or ``MPoint``s. Returns a NEW list; does not mutate
    ``points``, so callers can always fall back to the original.

    ``amount`` is the blend factor per iteration (0 = no change,
    1 = fully replaced by the neighbour average). Corner/isolated
    vertices with no neighbours are left untouched.
    """
    if amount <= 0.0 or iterations <= 0:
        return list(points)

    current = [(p.x, p.y, p.z) if hasattr(p, "x") else tuple(p) for p in points]

    for _ in range(max(1, int(iterations))):
        next_pts = list(current)
        for i, nbrs in enumerate(neighbors):
            if not nbrs:
                continue
            sx = sy = sz = 0.0
            for n in nbrs:
                nx, ny, nz = current[n]
                sx += nx
                sy += ny
                sz += nz
            count = len(nbrs)
            avg = (sx / count, sy / count, sz / count)
            cx, cy, cz = current[i]
            next_pts[i] = (
                cx + (avg[0] - cx) * amount,
                cy + (avg[1] - cy) * amount,
                cz + (avg[2] - cz) * amount,
            )
        current = next_pts

    return current
