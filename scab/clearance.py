"""Clearance between KiCad board parts and the SCAD model (enclosure / chassis).

For every part (its 3D bounding box) the check reports

* collision  — model surface passes through the part's box, or the part sits
               inside solid model material;
* headroom   — free space straight above the part's top (below the part for
               bottom-side parts), up to the model: "how much higher may it be";
* gap        — smallest distance from the part's box to any model surface.
"""
from dataclasses import dataclass
import numpy as np


@dataclass
class Result:
    ref: str
    index: int
    height: float          # top of part above the board surface
    headroom: float        # inf when nothing is above
    gap: float             # 0 when colliding
    collides: bool
    has_model: bool

    def status(self, minimum):
        if self.collides:
            return 'collision'
        if self.gap < minimum - 1e-3 or self.headroom < minimum - 1e-3:
            return 'tight'
        return 'ok'


def triangles_box_overlap(tri, lo, hi):
    """Vectorized Akenine-Möller triangle/AABB test. tri: (n, 3, 3)."""
    center = (lo + hi) / 2
    half = (hi - lo) / 2
    v = tri - center
    e = [v[:, 1] - v[:, 0], v[:, 2] - v[:, 1], v[:, 0] - v[:, 2]]
    ok = np.ones(len(tri), dtype=bool)
    # box face normals
    for axis in range(3):
        mn = v[:, :, axis].min(axis=1)
        mx = v[:, :, axis].max(axis=1)
        ok &= (mn <= half[axis]) & (mx >= -half[axis])
    if not ok.any():
        return ok
    # triangle normal
    normal = np.cross(e[0], e[1])
    d = np.einsum('ij,ij->i', normal, v[:, 0])
    r = np.abs(normal) @ half
    ok &= np.abs(d) <= r + 1e-9
    # nine edge cross products
    axes = np.eye(3)
    for edge in e:
        for a in axes:
            axis = np.cross(np.broadcast_to(a, edge.shape), edge)
            p = np.einsum('nkj,nj->nk', v, axis)
            radius = np.abs(axis) @ half
            ok &= (p.min(axis=1) <= radius + 1e-9) & (p.max(axis=1) >= -radius - 1e-9)
    return ok


def ray_hits(origins, direction, tri):
    """Distances along `direction` from each origin to the nearest triangle (inf if none)."""
    out = np.full(len(origins), np.inf)
    if not len(tri):
        return out
    e1 = tri[:, 1] - tri[:, 0]
    e2 = tri[:, 2] - tri[:, 0]
    p = np.cross(direction, e2)
    det = np.einsum('ij,ij->i', e1, p)
    valid = np.abs(det) > 1e-12
    inv = np.where(valid, 1 / np.where(valid, det, 1), 0)
    for i, o in enumerate(origins):
        t0 = o - tri[:, 0]
        u = np.einsum('ij,ij->i', t0, p) * inv
        q = np.cross(t0, e1)
        w = (q @ direction) * inv
        t = np.einsum('ij,ij->i', e2, q) * inv
        hit = valid & (u >= 0) & (w >= 0) & (u + w <= 1) & (t > 1e-6)
        if hit.any():
            out[i] = t[hit].min()
    return out


def inside(point, tri):
    """Point inside a closed mesh (odd number of crossings along +X)."""
    hits = 0
    direction = np.array([1., 1e-7, 2e-7])
    e1 = tri[:, 1] - tri[:, 0]
    e2 = tri[:, 2] - tri[:, 0]
    p = np.cross(direction, e2)
    det = np.einsum('ij,ij->i', e1, p)
    valid = np.abs(det) > 1e-12
    inv = np.where(valid, 1 / np.where(valid, det, 1), 0)
    t0 = point - tri[:, 0]
    u = np.einsum('ij,ij->i', t0, p) * inv
    q = np.cross(t0, e1)
    w = (q @ direction) * inv
    t = np.einsum('ij,ij->i', e2, q) * inv
    hits = int(np.count_nonzero(valid & (u >= 0) & (w >= 0) & (u + w <= 1) & (t > 0)))
    return hits % 2 == 1


def box_distance(lo, hi, points):
    d = np.maximum(np.maximum(lo - points, points - hi), 0)
    return np.linalg.norm(d, axis=1)


def check(parts, model_meshes, board_thickness=1.6, search=25., obstacles=()):
    """parts: [(ref, vertices, has_model, side)] -> [Result].

    model_meshes: closed solids from OpenSCAD — surfaces, and 'part inside material'.
    obstacles: other meshes (e.g. other boards and their 3D models, often open
    surfaces) — they count for surface intersection, headroom and gap only.
    """
    def stack(meshes):
        items = [np.asarray(v, dtype=np.float64)[np.asarray(f)] for v, f in meshes if len(f)]
        return np.vstack(items) if items else np.zeros((0, 3, 3))
    solid = stack(model_meshes)
    tri = stack(list(model_meshes) + list(obstacles))
    tri_lo = tri.min(axis=1) if len(tri) else np.zeros((0, 3))
    tri_hi = tri.max(axis=1) if len(tri) else np.zeros((0, 3))
    results = []
    for index, (ref, vertices, has_model, side) in enumerate(parts):
        lo = vertices.min(axis=0).astype(np.float64)
        hi = vertices.max(axis=0).astype(np.float64)
        top = side != 'bottom'
        height = hi[2] - board_thickness if top else -lo[2]
        near = np.all((tri_hi >= lo - search) & (tri_lo <= hi + search), axis=1) if len(tri) else np.zeros(0, bool)
        candidates = tri[near]
        collides = False
        if len(candidates):
            close = np.all((tri_hi[near] >= lo) & (tri_lo[near] <= hi), axis=1)
            if close.any():
                shrink = np.minimum((hi - lo) * .02, .02)
                collides = bool(triangles_box_overlap(candidates[close], lo + shrink, hi - shrink).any())
            if not collides and len(solid):
                collides = inside((lo + hi) / 2, solid)
        # headroom: rays from a 3x3 grid on the part's outer face
        xs = np.linspace(lo[0], hi[0], 3)
        ys = np.linspace(lo[1], hi[1], 3)
        z = hi[2] if top else lo[2]
        origins = np.array([[x, y, z] for x in xs for y in ys])
        direction = np.array([0., 0., 1. if top else -1.])
        column = np.all((tri_hi[:, :2] >= lo[:2]) & (tri_lo[:, :2] <= hi[:2]), axis=1) if len(tri) else np.zeros(0, bool)
        headroom = float(ray_hits(origins, direction, tri[column]).min()) if len(tri) and column.any() else float('inf')
        if collides:
            gap = 0.
        elif len(candidates):
            sample = np.concatenate([candidates.reshape(-1, 3), candidates.mean(axis=1),
                                     (candidates[:, 0] + candidates[:, 1]) / 2, (candidates[:, 1] + candidates[:, 2]) / 2,
                                     (candidates[:, 2] + candidates[:, 0]) / 2])
            gap = float(box_distance(lo, hi, sample).min())
            gap = min(gap, headroom)
        else:
            gap = float('inf')
        results.append(Result(ref, index, float(height), headroom, gap, collides, has_model))
    return results
