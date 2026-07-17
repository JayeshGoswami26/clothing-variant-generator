# -*- coding: utf-8 -*-
"""
processor.py
------------
Core workflow logic: transferring one clothing mesh, fitted to a Base body,
onto any number of Target bodies that share the Base body's topology and
skeleton.

DEFORMATION ENGINE (Maya API 2.0, no deformers)
-----------------------------------------------
The previous Wrap-deformer + BlendShape-driver technique has been fully
removed. The new engine performs deformation transfer entirely via direct
mesh-data operations on ``maya.api.OpenMaya`` -- no ``wrap`` node, no
``blendShape``, no ``shrinkWrap``, no ``deltaMush``, no MEL, no
``dgeval``. Nothing is added to the DG that needs cleaning up afterwards.

ALGORITHM (per clothing mesh, done ONCE and cached across all targets):

    For every clothing vertex ``V`` in world space:

      1. Find the closest point ``P`` on the Base body using
         ``MFnMesh.getClosestPoint`` -- this returns the polygon id ``poly``.
      2. Enumerate the triangles of ``poly`` (triangle counts for every
         polygon are precomputed once via ``MItMeshPolygon.numTriangles()``,
         since ``MFnMesh`` itself has no per-polygon triangle-count method)
         and pick the triangle that actually contains ``P`` (or the one
         with the smallest barycentric-distance for edge/corner cases).
      3. Compute the barycentric coordinates (u, v, w) of ``P`` inside
         that triangle (vertices A, B, C).
      4. Interpolate a smooth normal ``N_smooth`` at ``P`` from the base
         body's angle-weighted vertex normals.
      5. Express the offset ``O = V - P`` in a triangle-local basis
         ``M = [E1 | E2 | N_smooth]`` where ``E1 = B - A``, ``E2 = C - A``.
         Solving ``M * [a; b; h] = O`` (Cramer's rule on a 3x3) gives
         three scalars ``(a, b, h)`` that fully describe ``V`` relative
         to the triangle. This encoding is deliberately *not*
         orthonormalised so that when a target triangle stretches (say
         across a fat belly) the offset stretches with it, while ``h``
         is scaled by the target-normal magnitude of 1.0 -- i.e.
         thickness is preserved.

    Bind data stored per clothing vertex:
        (a_idx, b_idx, c_idx, u, v, w, a, b, h)

    -- 9 floats + 3 ints. For a 20k-vertex clothing mesh this is under
    a megabyte, so caching across all target bodies is trivial.

EVALUATION on each Target body (fast, per vertex):

    Read the target body's vertex positions and smooth vertex normals
    ONCE per target. For every stored binding:

        A' = target_pts[a_idx],  B' = target_pts[b_idx],  C' = target_pts[c_idx]
        P'      = u*A' + v*B' + w*C'
        N'      = normalize(u*N'_a + v*N'_b + w*N'_c)
        V_new   = P' + a*(B' - A') + b*(C' - A') + h*N'

    The full new-point set is written to the working clothing shape in
    one ``MFnMesh.setPoints`` call -- no per-vertex DG round-trip.

CONTRACT WITH THE OUTER PIPELINE (unchanged)
--------------------------------------------
``_deform_clothing_to_target()`` still returns
``(working_clothing_transform, temp_nodes_list)``. Because this engine
creates no helper nodes, ``temp_nodes_list`` is always ``[]``. The rest
of ``process_one()`` (skin-weight transfer, history delete, freeze,
centre-pivot, rename, FBX export, scene cleanup) is completely
untouched, as is ``BatchRunner``, the UI, the exporter, the logger and
the config.

ASSUMPTIONS (documented up-front so an artist can debug failures):

    * Maya 2025, Python 3, Maya API 2.0.
    * Base Body and every Target Body share identical vertex count,
      vertex order, UVs and skeleton. ``utils.topology_matches`` is
      already the pre-flight guard for this in ``process_one()``.
    * Clothing meshes have their own (different) topology and are
      already fitted to the Base body.
    * The pipeline is offline: results are baked to plain vertex
      positions and later fed into skinning / blendshape / Unity.
"""

import time

import maya.cmds as cmds
import maya.api.OpenMaya as om

from . import config
from . import utils
from . import adjacency


class ClothingProcessingError(Exception):
    """Raised for any recoverable failure while processing one clothing item."""
    pass


class TaskResult(object):
    """Outcome of processing a single (clothing, target_body) pair."""

    def __init__(self, clothing, target_display_name, success, mesh_name=None,
                 exported_path=None, error=None, duration=0.0):
        self.clothing = clothing
        self.target_display_name = target_display_name
        self.success = success
        self.mesh_name = mesh_name
        self.exported_path = exported_path
        self.error = error
        self.duration = duration


# ---------------------------------------------------------------------------
# Internal data container for per-clothing binding data
# ---------------------------------------------------------------------------
class _ClothingBinding(object):
    """
    Cached per-vertex mapping from a clothing mesh to the Base body.

    Storing this as a plain object with parallel lists (rather than a
    list of tuples) makes the hot evaluation loop measurably faster
    because Python doesn't have to unpack a tuple per iteration.

    Attributes
    ----------
    vertex_count : int
        Number of clothing vertices this binding describes.
    tri_a, tri_b, tri_c : list[int]
        Global base-body vertex indices for the triangle each clothing
        vertex is bound to.
    bary_u, bary_v, bary_w : list[float]
        Barycentric weights (sum to 1.0) of the closest surface point
        inside that triangle.
    coord_a, coord_b, coord_h : list[float]
        Coefficients of ``V - P`` in the ``[E1 | E2 | N_smooth]`` basis.
    base_body : str
        Base body transform this binding was computed against, so we
        can invalidate the cache if the artist reassigns the Base.
    """

    __slots__ = (
        "vertex_count",
        "tri_a", "tri_b", "tri_c",
        "bary_u", "bary_v", "bary_w",
        "coord_a", "coord_b", "coord_h",
        "base_body",
    )

    def __init__(self, vertex_count, base_body):
        self.vertex_count = vertex_count
        self.base_body = base_body
        self.tri_a = [0] * vertex_count
        self.tri_b = [0] * vertex_count
        self.tri_c = [0] * vertex_count
        self.bary_u = [0.0] * vertex_count
        self.bary_v = [0.0] * vertex_count
        self.bary_w = [0.0] * vertex_count
        self.coord_a = [0.0] * vertex_count
        self.coord_b = [0.0] * vertex_count
        self.coord_h = [0.0] * vertex_count


class ClothingVariantProcessor(object):
    """
    Owns the per-task deformation-transfer pipeline. Holds no UI state --
    the UI layer drives this class one task at a time so it can update
    progress and respond to Cancel between tasks.
    """

    # Small epsilons for numerical stability. Kept as class constants so
    # they are trivially tunable without magic numbers scattered inline.
    _DEGENERATE_TRI_EPS = 1e-20   # Gram determinant floor for barycentric
    _DEGENERATE_MAT_EPS = 1e-12   # 3x3 determinant floor for offset solve
    _NORMAL_LENGTH_EPS = 1e-10    # smooth normal length floor before normalise

    def __init__(self, base_body, logger, exporter=None):
        self.base_body = base_body
        self.logger = logger
        self.exporter = exporter
        # Shared deformation state for the Deformation Controls panel.
        # None (the default) means "fully automatic, exactly as before"
        # -- process_one() never requires a DeformationState, preserving
        # backward compatibility with any external script that calls it
        # directly.
        self.deform_state = None

        # Bindings are expensive to compute (a closest-point query per
        # clothing vertex) but a batch run processes each clothing
        # against MANY targets in a row. Caching per-clothing turns an
        # O(clothings * targets) bind cost into O(clothings), which is
        # the difference between "runs overnight" and "finishes in a
        # coffee break". Keyed by clothing transform name.
        self._binding_cache = {}

    # ------------------------------------------------------------------
    # Public entry point: process ONE clothing mesh for ONE target body
    # ------------------------------------------------------------------
    def process_one(self, clothing_mesh, target_body, options, output_folder=None):
        """
        Run the full pipeline for a single clothing/target pair.
        Returns a TaskResult. Never raises -- all failures are captured
        and reported through the TaskResult / logger so a batch of
        hundreds of assets can never be brought down by one bad mesh.
        """
        start_time = time.time()
        target_mesh = target_body["mesh"]
        target_display = target_body["display_name"]

        temp_nodes = []
        working_clothing = None

        try:
            utils.validate_mesh(clothing_mesh, "Clothing mesh")
            utils.validate_mesh(self.base_body, "Base body")
            utils.validate_mesh(target_mesh, "Target body '%s'" % target_display)

            if not utils.topology_matches(self.base_body, target_mesh):
                raise ClothingProcessingError(
                    "Topology mismatch between Base body and target '%s' "
                    "(vertex counts differ) -- cannot transfer safely." % target_display
                )

            with utils.undo_chunk("CVG_%s_%s" % (clothing_mesh, target_display)):
                working_clothing, temp_nodes = self._deform_clothing_to_target(
                    clothing_mesh, target_mesh
                )

                if options.get("transfer_skin_weights"):
                    self._transfer_skin_weights(clothing_mesh, working_clothing)

                if options.get("delete_history", True):
                    utils.safe_delete_history(working_clothing, self.logger)

                if options.get("freeze_transforms", True):
                    utils.freeze_transform(working_clothing, self.logger)

                if options.get("center_pivot", True):
                    utils.center_pivot(working_clothing, self.logger)

                final_name = self._rename_result(clothing_mesh, working_clothing, target_display)

            exported_path = None
            if options.get("export_fbx") and output_folder:
                exported_path = self.exporter.export_mesh(
                    final_name,
                    output_folder,
                    utils.sanitize_folder_component(target_display),
                    overwrite=options.get("overwrite_existing", False),
                )

            if options.get("keep_scene_clean", True):
                utils.safe_delete_nodes(temp_nodes, self.logger)

            duration = time.time() - start_time
            self.logger.success(
                "Generated '%s' for '%s' in %.2fs" % (final_name, target_display, duration)
            )
            return TaskResult(clothing_mesh, target_display, True, final_name,
                               exported_path, None, duration)

        except (ClothingProcessingError, utils.MeshValidationError) as exc:
            utils.safe_delete_nodes(temp_nodes, self.logger)
            if working_clothing and cmds.objExists(working_clothing):
                utils.safe_delete_nodes([working_clothing], self.logger)
            self.logger.error("Skipped '%s' for '%s': %s" % (clothing_mesh, target_display, exc))
            return TaskResult(clothing_mesh, target_display, False, error=str(exc),
                               duration=time.time() - start_time)

        except Exception as exc:  # noqa: BLE001 - last line of defense, must never crash Maya
            utils.safe_delete_nodes(temp_nodes, self.logger)
            if working_clothing and cmds.objExists(working_clothing):
                utils.safe_delete_nodes([working_clothing], self.logger)
            self.logger.error(
                "Unexpected error processing '%s' for '%s': %s" % (clothing_mesh, target_display, exc)
            )
            return TaskResult(clothing_mesh, target_display, False, error=str(exc),
                               duration=time.time() - start_time)

    # ------------------------------------------------------------------
    # NEW deformation transfer (pure Maya API 2.0, no deformers)
    # ------------------------------------------------------------------
    def _deform_clothing_to_target(self, clothing_mesh, target_mesh):
        """
        Bake ``clothing_mesh`` onto ``target_mesh`` and return
        ``(working_clothing_transform, temp_nodes_list)``.

        Contract preserved with the outer pipeline. Because this
        implementation creates NO helper deformers, driver duplicates,
        blendShapes or any other DG nodes, ``temp_nodes_list`` is always
        empty; the outer ``safe_delete_nodes(temp_nodes, ...)`` call is
        then simply a no-op, which is what we want.
        """
        # Step 1 -- bind clothing to Base body (cached across targets).
        binding = self._get_or_build_binding(clothing_mesh)

        # Step 2 -- evaluate that binding on the Target body to get the
        # new world-space clothing point positions, plus the per-vertex
        # normals/surface-points needed by the manual Deformation
        # Controls below.
        points, normals, surface_points = self._evaluate_binding_on_target(binding, target_mesh)

        # Step 3 -- (optional) manual Deformation Controls: global
        # influence and surface offset, then smoothing. A None
        # deform_state means "fully automatic", i.e. the original
        # behaviour with zero extra cost -- this step is skipped
        # entirely.
        if self.deform_state is not None:
            points = self._apply_deform_state(
                binding, clothing_mesh, points, normals, surface_points, self.deform_state
            )

        # Step 4 -- duplicate the original clothing (so we inherit its
        # UVs, materials, normals config, vertex order) and rewrite its
        # point positions in-place via the mesh function set. No
        # construction history is created by MFnMesh.setPoints.
        new_points = om.MPointArray([om.MPoint(p[0], p[1], p[2]) for p in points])
        working_clothing = self._write_result_mesh(clothing_mesh, new_points)

        return working_clothing, []

    # ------------------------------------------------------------------
    # Manual Deformation Controls: scale the offset each clothing
    # vertex has from its bound surface point by the Global Body
    # Influence, add the Global Surface Offset along the body normal,
    # then blend toward a Laplacian-smoothed version of the result by
    # Smoothing * Falloff.
    # ------------------------------------------------------------------
    def _apply_deform_state(self, binding, clothing_mesh, points, normals, surface_points, state):
        vcount = len(points)
        global_infl = state.global_influence
        offset_amount = state.surface_offset

        out = [None] * vcount
        for i in range(vcount):
            p = points[i]
            surf = surface_points[i]
            n = normals[i]

            # Scale the deviation from the surface point by the Global
            # Body Influence, then push along the normal by the
            # (unscaled -- it's an absolute cm value, not a percentage)
            # Global Surface Offset.
            dx = (p[0] - surf[0]) * global_infl + n[0] * offset_amount
            dy = (p[1] - surf[1]) * global_infl + n[1] * offset_amount
            dz = (p[2] - surf[2]) * global_infl + n[2] * offset_amount
            out[i] = (surf[0] + dx, surf[1] + dy, surf[2] + dz)

        if state.smoothing > 0.0:
            neighbors = adjacency.default_cache.neighbors_for(clothing_mesh)
            # Falloff controls how gradually smoothing spreads: low
            # falloff -> a single gentle pass; high falloff -> more
            # iterations so the smoothing reaches further across the
            # mesh per slider unit of "Smoothing".
            amount = min(1.0, state.smoothing / 100.0)
            iterations = 1 + int(round(state.falloff * 4))
            out = adjacency.laplacian_smooth(out, neighbors, amount * 0.5, iterations)

        return out

    # ------------------------------------------------------------------
    # Binding: clothing -> Base body (cached)
    # ------------------------------------------------------------------
    def _get_or_build_binding(self, clothing_mesh):
        """
        Return a cached ``_ClothingBinding`` for ``clothing_mesh`` if one
        exists and is still valid (same Base body, same vertex count),
        otherwise compute a fresh binding and cache it.
        """
        cached = self._binding_cache.get(clothing_mesh)
        if cached is not None:
            still_valid = (
                cached.base_body == self.base_body
                and cached.vertex_count == utils.vertex_count(clothing_mesh)
            )
            if still_valid:
                return cached
            # Cache miss due to invalidation -- fall through and rebuild.
            self._binding_cache.pop(clothing_mesh, None)

        bind_start = time.time()
        binding = self._build_binding(clothing_mesh)
        self._binding_cache[clothing_mesh] = binding
        self.logger.info(
            "Bound '%s' to Base body '%s' (%d verts) in %.2fs"
            % (clothing_mesh, self.base_body, binding.vertex_count,
               time.time() - bind_start)
        )
        return binding

    def _build_binding(self, clothing_mesh):
        """
        Compute per-vertex binding data mapping every clothing vertex to
        a triangle on the Base body plus a local-frame offset.

        This is the expensive step (one closest-point query per
        clothing vertex). It runs once per clothing mesh regardless of
        how many target bodies the artist has queued.
        """
        clothing_shape_path = self._shape_dag_path(clothing_mesh)
        base_shape_path = self._shape_dag_path(self.base_body)

        clothing_fn = om.MFnMesh(clothing_shape_path)
        base_fn = om.MFnMesh(base_shape_path)

        clothing_points = clothing_fn.getPoints(om.MSpace.kWorld)
        base_points = base_fn.getPoints(om.MSpace.kWorld)
        # Angle-weighted per-vertex normals give C1-ish continuity across
        # the body surface, which is what avoids stair-stepping artifacts
        # at polygon boundaries when the offset is reconstructed on the
        # target. Face normals would break at every triangle edge.
        base_vnormals = base_fn.getVertexNormals(True, om.MSpace.kWorld)

        # Per-polygon triangle counts, computed ONCE via MItMeshPolygon
        # (MFnMesh has no polygonTriangleCount() method -- the only way
        # to get a face's triangle count in the API is numTriangles()
        # on the polygon iterator). Precomputing this array up front
        # keeps the per-vertex hot loop below free of iterator overhead.
        poly_tri_counts = self._build_polygon_triangle_counts(base_shape_path)

        vcount = len(clothing_points)
        binding = _ClothingBinding(vcount, self.base_body)

        # Local aliases inside the hot loop -- these save attribute
        # lookups and matter when iterating over tens of thousands of
        # vertices from Python.
        kWorld = om.MSpace.kWorld
        get_closest = base_fn.getClosestPoint
        pick_triangle = self._pick_triangle_in_polygon
        encode_offset = self._encode_offset_in_triangle_frame

        for i in range(vcount):
            v_world = clothing_points[i]
            # getClosestPoint returns (MPoint, int). The int is the
            # polygon id on the base body; that polygon may contain
            # multiple triangles (n-gons are legal on production meshes).
            surface_pt, poly_id = get_closest(v_world, kWorld)

            tri_verts, bary = pick_triangle(base_fn, poly_id, surface_pt, base_points, poly_tri_counts)
            a_idx, b_idx, c_idx = tri_verts
            u, v, w = bary

            A = base_points[a_idx]
            B = base_points[b_idx]
            C = base_points[c_idx]

            # Smooth interpolated normal at the surface point.
            n_smooth = self._interpolate_smooth_normal(
                base_vnormals, a_idx, b_idx, c_idx, u, v, w
            )

            a_coef, b_coef, h_coef, _degenerate = encode_offset(A, B, C, n_smooth, v_world, surface_pt)

            binding.tri_a[i] = a_idx
            binding.tri_b[i] = b_idx
            binding.tri_c[i] = c_idx
            binding.bary_u[i] = u
            binding.bary_v[i] = v
            binding.bary_w[i] = w
            binding.coord_a[i] = a_coef
            binding.coord_b[i] = b_coef
            binding.coord_h[i] = h_coef

        return binding

    # ------------------------------------------------------------------
    # Evaluation: binding -> new clothing points on a Target body
    # ------------------------------------------------------------------
    def _evaluate_binding_on_target(self, binding, target_mesh):
        """
        Apply a stored binding to a Target body and return
        ``(points, normals, surface_points)`` as three parallel lists of
        plain ``(x, y, z)`` tuples in world space:

            points         -- final clothing vertex positions (the
                               original, fully-automatic result)
            normals        -- interpolated smooth body normal at each
                               clothing vertex's bind point, evaluated
                               on THIS target
            surface_points -- the barycentric surface point ("P'") each
                               clothing vertex is bound to on THIS
                               target

        Plain tuples (rather than ``MPoint``/``MVector``) are returned
        so the manual Deformation Controls step in ``process_one()`` can
        operate on them with plain Python math, at zero Maya-API
        object-creation cost in its hot loop. No
        closest-point queries happen here -- everything is a fixed
        number of vector operations per clothing vertex, so this scales
        linearly with mesh size and is fast even for hero clothing.
        """
        target_shape_path = self._shape_dag_path(target_mesh)
        target_fn = om.MFnMesh(target_shape_path)

        target_points = target_fn.getPoints(om.MSpace.kWorld)
        target_vnormals = target_fn.getVertexNormals(True, om.MSpace.kWorld)

        # Local aliases -- see comment in _build_binding for the reason.
        tri_a = binding.tri_a
        tri_b = binding.tri_b
        tri_c = binding.tri_c
        bu = binding.bary_u
        bv = binding.bary_v
        bw = binding.bary_w
        ca = binding.coord_a
        cb = binding.coord_b
        ch = binding.coord_h

        vcount = binding.vertex_count
        points = [None] * vcount
        normals = [None] * vcount
        surface_points = [None] * vcount

        for i in range(vcount):
            a_idx = tri_a[i]
            b_idx = tri_b[i]
            c_idx = tri_c[i]
            u = bu[i]
            v = bv[i]
            w = bw[i]

            A = target_points[a_idx]
            B = target_points[b_idx]
            C = target_points[c_idx]

            # Barycentric surface point on the target.
            px = A.x * u + B.x * v + C.x * w
            py = A.y * u + B.y * v + C.y * w
            pz = A.z * u + B.z * v + C.z * w

            # Interpolated smooth normal on the target.
            n_smooth = self._interpolate_smooth_normal(
                target_vnormals, a_idx, b_idx, c_idx, u, v, w
            )

            # Reconstruct offset in target's triangle frame.
            #   V_new = P' + a*(B'-A') + b*(C'-A') + h*N'
            e1x = B.x - A.x
            e1y = B.y - A.y
            e1z = B.z - A.z
            e2x = C.x - A.x
            e2y = C.y - A.y
            e2z = C.z - A.z

            a_coef = ca[i]
            b_coef = cb[i]
            h_coef = ch[i]

            vx = px + a_coef * e1x + b_coef * e2x + h_coef * n_smooth.x
            vy = py + a_coef * e1y + b_coef * e2y + h_coef * n_smooth.y
            vz = pz + a_coef * e1z + b_coef * e2z + h_coef * n_smooth.z

            points[i] = (vx, vy, vz)
            normals[i] = (n_smooth.x, n_smooth.y, n_smooth.z)
            surface_points[i] = (px, py, pz)

        return points, normals, surface_points

    # ------------------------------------------------------------------
    # Result mesh: duplicate the original clothing and setPoints() on it
    # ------------------------------------------------------------------
    def _write_result_mesh(self, clothing_mesh, new_points):
        """
        Duplicate the original clothing mesh, then overwrite its point
        positions with ``new_points`` using ``MFnMesh.setPoints``.

        Duplicating preserves UVs, normals, materials and vertex order
        (which is exactly what the pipeline needs downstream for
        skinning and blendshape creation). ``setPoints`` writes the new
        positions directly to the mesh data with no construction
        history and no DG nodes created.
        """
        working = utils.duplicate_mesh(
            clothing_mesh,
            config.TEMP_PREFIX + clothing_mesh + config.WORKING_CLOTHING_SUFFIX,
        )

        working_shape_path = self._shape_dag_path(working)
        working_fn = om.MFnMesh(working_shape_path)

        expected = working_fn.numVertices
        if len(new_points) != expected:
            raise ClothingProcessingError(
                "Internal error: computed %d points but working mesh '%s' "
                "has %d vertices." % (len(new_points), working, expected)
            )

        # kWorld tells Maya to inverse-transform through the working
        # mesh's own transform. That is what makes this robust to any
        # residual transforms left on the duplicate before the outer
        # pipeline calls freeze_transform().
        working_fn.setPoints(new_points, om.MSpace.kWorld)
        # Tag the surface as edited so the viewport refreshes without a
        # full DG evaluation.
        working_fn.updateSurface()

        return working

    # ------------------------------------------------------------------
    # API-2.0 low-level helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _shape_dag_path(transform_name):
        """
        Return an ``MDagPath`` pointing at the mesh SHAPE below
        ``transform_name`` (or at the shape itself if a shape was passed).
        Constructing ``MFnMesh`` from a transform-level dag path works
        in some Maya versions and fails in others, so we always extend
        to the shape.
        """
        sel = om.MSelectionList()
        sel.add(transform_name)
        dag = sel.getDagPath(0)
        if dag.apiType() != om.MFn.kMesh:
            dag.extendToShape()
        return dag

    @classmethod
    def _pick_triangle_in_polygon(cls, base_fn, poly_id, surface_pt, base_points, poly_tri_counts):
        """
        Given the polygon id returned by ``getClosestPoint`` and the
        closest-point position, work out which of the polygon's
        triangles the point actually sits inside. Returns
        ``((a_idx, b_idx, c_idx), (u, v, w))`` -- the triangle's global
        vertex indices and the barycentric coordinates of the point.

        For triangular polygons this is trivially the only triangle.
        For n-gons we test each internal triangle in turn: if the point
        falls strictly inside one, that's our answer; otherwise we pick
        the triangle whose barycentric coords are least negative, which
        picks the geometrically closest sub-triangle for
        edge-adjacent hits.

        ``poly_tri_counts`` is a precomputed list (index = polygon id,
        value = triangle count for that polygon), built once per Base
        body by ``_build_polygon_triangle_counts`` -- MFnMesh has no
        ``polygonTriangleCount()`` method, so this is queried via
        ``MItMeshPolygon.numTriangles()`` up front rather than per
        vertex.
        """
        tri_count = poly_tri_counts[poly_id] if poly_id < len(poly_tri_counts) else 1
        if tri_count < 1:
            tri_count = 1

        best_tri = None
        best_bary = None
        best_score = float("inf")

        for tri_id in range(tri_count):
            a_idx, b_idx, c_idx = base_fn.getPolygonTriangleVertices(poly_id, tri_id)
            A = base_points[a_idx]
            B = base_points[b_idx]
            C = base_points[c_idx]

            u, v, w = cls._barycentric(surface_pt, A, B, C)

            # Score = amount by which the point falls outside this
            # triangle. Zero when strictly inside.
            neg = 0.0
            if u < 0.0:
                neg -= u
            if v < 0.0:
                neg -= v
            if w < 0.0:
                neg -= w

            if neg < best_score:
                best_score = neg
                best_tri = (a_idx, b_idx, c_idx)
                best_bary = (u, v, w)
                if neg == 0.0:
                    # Strictly inside -- no better triangle possible.
                    break

        if best_tri is None:
            # Shouldn't happen in practice (every real polygon has at
            # least 1 triangle), but keep the pipeline defensive.
            a_idx, b_idx, c_idx = base_fn.getPolygonTriangleVertices(poly_id, 0)
            best_tri = (a_idx, b_idx, c_idx)
            best_bary = (1.0, 0.0, 0.0)

        return best_tri, best_bary

    @staticmethod
    def _build_polygon_triangle_counts(shape_path):
        """
        Walk every polygon on a mesh ONCE via ``MItMeshPolygon`` and
        return a list (index = polygon id) of each polygon's triangle
        count. This is the correct API-2.0 way to get a per-face
        triangle count -- ``MFnMesh`` itself has no
        ``polygonTriangleCount()`` method.
        """
        poly_iter = om.MItMeshPolygon(shape_path)
        counts = [1] * poly_iter.count()
        while not poly_iter.isDone():
            counts[poly_iter.index()] = poly_iter.numTriangles()
            poly_iter.next()
        return counts

    @classmethod
    def _barycentric(cls, p, a, b, c):
        """
        Barycentric coordinates of ``p`` with respect to triangle
        ``(a, b, c)``. Returns ``(u, v, w)`` where
        ``p ~= u*a + v*b + w*c`` and ``u + v + w == 1``.

        Uses the Ericson / "Real-Time Collision Detection" formulation
        (Gram matrix), which is numerically robust for the near-planar
        projection case we care about here.
        """
        v0x = b.x - a.x
        v0y = b.y - a.y
        v0z = b.z - a.z
        v1x = c.x - a.x
        v1y = c.y - a.y
        v1z = c.z - a.z
        v2x = p.x - a.x
        v2y = p.y - a.y
        v2z = p.z - a.z

        d00 = v0x * v0x + v0y * v0y + v0z * v0z
        d01 = v0x * v1x + v0y * v1y + v0z * v1z
        d11 = v1x * v1x + v1y * v1y + v1z * v1z
        d20 = v2x * v0x + v2y * v0y + v2z * v0z
        d21 = v2x * v1x + v2y * v1y + v2z * v1z

        denom = d00 * d11 - d01 * d01
        if abs(denom) < cls._DEGENERATE_TRI_EPS:
            # Degenerate triangle -- dump all weight on vertex A. This
            # only happens on genuinely broken meshes; the outer
            # topology guard normally catches those, but the algorithm
            # must never divide by zero.
            return 1.0, 0.0, 0.0

        inv_denom = 1.0 / denom
        v = (d11 * d20 - d01 * d21) * inv_denom
        w = (d00 * d21 - d01 * d20) * inv_denom
        u = 1.0 - v - w
        return u, v, w

    @staticmethod
    def _interpolate_smooth_normal(vnormals, a_idx, b_idx, c_idx, u, v, w):
        """
        Return the barycentric interpolation of three per-vertex smooth
        normals, renormalised. Returns an ``MVector``.

        ``vnormals`` is an ``MFloatVectorArray`` from
        ``MFnMesh.getVertexNormals``. We deliberately avoid constructing
        an ``MVector`` per component multiplication and instead work in
        scalars for speed.
        """
        na = vnormals[a_idx]
        nb = vnormals[b_idx]
        nc = vnormals[c_idx]

        nx = na.x * u + nb.x * v + nc.x * w
        ny = na.y * u + nb.y * v + nc.y * w
        nz = na.z * u + nb.z * v + nc.z * w

        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length < ClothingVariantProcessor._NORMAL_LENGTH_EPS:
            # Opposing normals cancelled to zero -- extremely unlikely
            # on real character topology but must be handled. Fall back
            # to the un-averaged normal of vertex A.
            return om.MVector(na.x, na.y, na.z)

        inv = 1.0 / length
        return om.MVector(nx * inv, ny * inv, nz * inv)

    @classmethod
    def _encode_offset_in_triangle_frame(cls, A, B, C, n_smooth, v_world, surface_pt):
        """
        Express ``V - P`` in the local basis ``[E1 | E2 | N_smooth]``
        where ``E1 = B - A``, ``E2 = C - A``. Returns ``(a, b, h)``.

        Solves the 3x3 linear system via Cramer's rule -- for a 3x3 with
        one right-hand-side this is faster than any general solver and
        completely avoids NumPy dependencies (Maya's shipped Python
        environment has NumPy available in 2022+, but this module
        intentionally stays dependency-free).

        The encoding preserves two properties we want in a game
        pipeline: (i) when the target triangle stretches, ``a*E1' +
        b*E2'`` stretches with it, so a shirt widens over a fat belly
        rather than pinching; (ii) ``h`` is measured against a
        unit-length smooth normal, so thickness is preserved.
        """
        # Offset vector components -- inline, no MVector allocation.
        ox = v_world.x - surface_pt.x
        oy = v_world.y - surface_pt.y
        oz = v_world.z - surface_pt.z

        e1x = B.x - A.x
        e1y = B.y - A.y
        e1z = B.z - A.z
        e2x = C.x - A.x
        e2y = C.y - A.y
        e2z = C.z - A.z

        nx = n_smooth.x
        ny = n_smooth.y
        nz = n_smooth.z

        # det(M) = e1 . (e2 x n)
        cx = e2y * nz - e2z * ny
        cy = e2z * nx - e2x * nz
        cz = e2x * ny - e2y * nx
        det = e1x * cx + e1y * cy + e1z * cz

        if abs(det) < cls._DEGENERATE_MAT_EPS:
            # Frame is degenerate (smooth normal parallel to triangle,
            # or triangle collinear). Fall back to a pure-normal offset:
            # h is the along-normal component, tangential terms are 0.
            h = ox * nx + oy * ny + oz * nz
            return 0.0, 0.0, h, True

        inv_det = 1.0 / det

        # By Cramer's rule with columns [E1 | E2 | N]:
        #   a = o . (e2 x n)  / det
        #   b = o . (n  x e1) / det
        #   h = o . (e1 x e2) / det
        a_coef = (ox * cx + oy * cy + oz * cz) * inv_det

        # n x e1
        c2x = ny * e1z - nz * e1y
        c2y = nz * e1x - nx * e1z
        c2z = nx * e1y - ny * e1x
        b_coef = (ox * c2x + oy * c2y + oz * c2z) * inv_det

        # e1 x e2
        c3x = e1y * e2z - e1z * e2y
        c3y = e1z * e2x - e1x * e2z
        c3z = e1x * e2y - e1y * e2x
        h_coef = (ox * c3x + oy * c3y + oz * c3z) * inv_det

        return a_coef, b_coef, h_coef, False

    # ------------------------------------------------------------------
    # Skin weights (optional) -- UNCHANGED from the original pipeline
    # ------------------------------------------------------------------
    def _transfer_skin_weights(self, source_clothing, new_clothing):
        skin = utils.get_skin_cluster(source_clothing)
        if not skin:
            self.logger.warning(
                "No skinCluster found on '%s'; skin weight transfer skipped." % source_clothing
            )
            return False

        influences = utils.get_influences(skin)
        if not influences:
            self.logger.warning(
                "skinCluster on '%s' has no influences; transfer skipped." % source_clothing
            )
            return False

        try:
            new_skin_name = utils.unique_name(new_clothing + "_skinCluster")
            cmds.skinCluster(influences, new_clothing, toSelectedBones=True,
                              name=new_skin_name, removeUnusedInfluence=False)
            cmds.copySkinWeights(sourceSkin=skin, destinationSkin=new_skin_name,
                                  noMirror=True, surfaceAssociation="closestPoint",
                                  influenceAssociation=["oneToOne", "closestJoint"])
            return True
        except RuntimeError as exc:
            self.logger.warning(
                "Skin weight transfer failed for '%s': %s" % (new_clothing, exc)
            )
            return False

    # ------------------------------------------------------------------
    # Renaming -- UNCHANGED
    # ------------------------------------------------------------------
    def _rename_result(self, original_clothing, working_clothing, target_display):
        base_name = utils.strip_namespace(original_clothing)
        desired = "%s_%s" % (base_name, utils.sanitize_folder_component(target_display))
        final_name = utils.unique_name(desired)
        return cmds.rename(working_clothing, final_name)


# ---------------------------------------------------------------------------
# Batch orchestration -- UNCHANGED
# ---------------------------------------------------------------------------
class BatchRunner(object):
    """
    Builds the full (clothing x target_body) task list and exposes a
    single `run_next()` step. The UI drives this with a QTimer so the
    Maya main thread stays responsive, the progress bar / log update
    live, and Cancel can take effect between tasks (Maya's API is not
    thread-safe, so true background threading is intentionally avoided).

    NOTE ON TASK ORDER
    ------------------
    Tasks are ordered ``for clothing in clothing_list: for target in
    target_bodies``. That grouping is important because the processor
    caches a per-clothing binding to the Base body: all targets for one
    clothing are consumed back-to-back, so the (expensive) closest-point
    binding is computed once per clothing and reused across every
    target variant.
    """

    def __init__(self, processor, clothing_list, target_bodies, options,
                 output_folder, logger):
        self.processor = processor
        self.options = options
        self.output_folder = output_folder
        self.logger = logger
        self.tasks = [
            (clothing, target) for clothing in clothing_list for target in target_bodies
        ]
        self.total = len(self.tasks)
        self.index = 0
        self.cancelled = False
        self.paused = False
        self.results = []
        self._task_durations = []
        self._vertex_counts = []
        self.start_time = None

    def cancel(self):
        self.cancelled = True

    def pause(self):
        """Pause between tasks -- Part 10. The current task always
        finishes first; run_next() simply becomes a no-op while paused,
        so the UI timer can keep ticking (updating elapsed time) without
        starting new work."""
        self.paused = True

    def resume(self):
        self.paused = False

    def is_paused(self):
        return self.paused and not self.is_finished()

    def is_finished(self):
        return self.cancelled or self.index >= self.total

    def retry_failed(self):
        """Re-queue every failed task (Part 10: Retry Failed Items) by
        appending them to the end of the task list and clearing their
        prior failure results. Safe to call once the batch has finished
        (partially or fully) but not while it is still mid-run."""
        failed_tasks = [
            self.tasks[i] for i, r in enumerate(self.results) if not r.success
        ]
        if not failed_tasks:
            return 0
        # Drop the failed results so stats_summary() doesn't double
        # count them, then re-append the tasks for another pass.
        self.results = [r for r in self.results if r.success]
        self.tasks = self.tasks[:self.index] + failed_tasks
        self.total = len(self.tasks)
        self.index = len(self.results)
        self.cancelled = False
        self.paused = False
        return len(failed_tasks)

    def run_next(self):
        """Process exactly one task. Returns the TaskResult, or None if
        finished OR currently paused (paused is intentionally distinct
        from finished so the UI can tell them apart)."""
        if self.is_finished() or self.paused:
            return None
        if self.start_time is None:
            self.start_time = time.time()
        clothing, target = self.tasks[self.index]
        result = self.processor.process_one(clothing, target, self.options, self.output_folder)
        self.results.append(result)
        self._task_durations.append(result.duration)
        self._vertex_counts.append(utils.vertex_count(clothing) if utils.node_exists_and_is_mesh(clothing) else 0)
        self.index += 1
        return result

    def current_vertex_count(self):
        return self._vertex_counts[-1] if self._vertex_counts else 0

    def processing_speed(self):
        """Vertices processed per second, averaged over the batch so
        far -- shown in the UI's Processing Statistics row."""
        if not self._task_durations or self.start_time is None:
            return 0.0
        elapsed = time.time() - self.start_time
        if elapsed <= 0:
            return 0.0
        return sum(self._vertex_counts) / elapsed

    def elapsed_seconds(self):
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time

    def progress_fraction(self):
        if self.total == 0:
            return 1.0
        return float(self.index) / float(self.total)

    def eta_seconds(self):
        """Simple moving-average ETA based on tasks completed so far."""
        if not self._task_durations:
            return None
        avg = sum(self._task_durations) / len(self._task_durations)
        remaining = self.total - self.index
        return avg * remaining

    def stats_summary(self):
        succeeded = sum(1 for r in self.results if r.success)
        failed = sum(1 for r in self.results if not r.success)
        total_time = sum(r.duration for r in self.results)
        return {
            "total": self.total,
            "processed": self.index,
            "succeeded": succeeded,
            "failed": failed,
            "total_time": total_time,
        }
