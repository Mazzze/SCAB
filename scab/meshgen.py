"""Small mesh builders for thin board layers (pads, silkscreen): NumPy, no CSG.

Everything here is a set of prisms: a 2D outline (convex, or a ring) extruded
between two heights. Overlapping prisms are fine for display, so no boolean
union is needed — which is what made OpenSCAD slow for hundreds of strokes.
"""
import math
import numpy as np


class MeshBuilder:
    def __init__(self):
        self.vertices = []
        self.faces = []
        self.count = 0

    def add(self, vertices, faces):
        self.vertices.append(np.asarray(vertices, dtype=np.float32))
        self.faces.append(np.asarray(faces, dtype=np.int64) + self.count)
        self.count += len(vertices)

    def mesh(self):
        if not self.vertices:
            return None
        return np.vstack(self.vertices), np.vstack(self.faces).astype(np.uint32)

    def prism(self, outline, z0, z1):
        """Extrude a convex (or star-shaped around its centroid) outline."""
        pts = np.asarray(outline, dtype=float)
        n = len(pts)
        if n < 3:
            return
        center = pts.mean(axis=0)
        v = np.vstack([np.c_[pts, np.full(n, z0)], np.c_[pts, np.full(n, z1)], [[*center, z0], [*center, z1]]])
        i = np.arange(n)
        j = (i + 1) % n
        faces = np.vstack([np.c_[np.full(n, 2 * n), j, i],                    # bottom fan
                           np.c_[np.full(n, 2 * n + 1), n + i, n + j],        # top fan
                           np.c_[i, j, n + j], np.c_[i, n + j, n + i]])       # sides
        self.add(v, faces)

    def ring(self, outer, inner, z0, z1):
        """Extrude the area between two outlines sampled with the same number of points."""
        o = np.asarray(outer, dtype=float)
        h = np.asarray(inner, dtype=float)
        n = len(o)
        v = np.vstack([np.c_[o, np.full(n, z0)], np.c_[o, np.full(n, z1)], np.c_[h, np.full(n, z0)], np.c_[h, np.full(n, z1)]])
        i = np.arange(n)
        j = (i + 1) % n
        O0, O1, I0, I1 = 0, n, 2 * n, 3 * n
        faces = np.vstack([np.c_[O1 + i, O1 + j, I1 + j], np.c_[O1 + i, I1 + j, I1 + i],     # top
                           np.c_[O0 + j, O0 + i, I0 + i], np.c_[O0 + j, I0 + i, I0 + j],     # bottom
                           np.c_[O0 + i, O0 + j, O1 + j], np.c_[O0 + i, O1 + j, O1 + i],     # outer side
                           np.c_[I0 + j, I0 + i, I1 + i], np.c_[I0 + j, I1 + i, I1 + j]])    # hole side
        self.add(v, faces)

    def capsule(self, a, b, width, z0, z1, segments=6):
        """A stroke from a to b with round ends (as KiCad draws lines and text)."""
        self.prism(capsule_outline(a, b, width, segments), z0, z1)

    def polygon(self, points, z0, z1):
        """Filled simple polygon (ear clipping) extruded."""
        pts = np.asarray(points, dtype=float)
        if len(pts) > 2 and np.allclose(pts[0], pts[-1]):
            pts = pts[:-1]
        tris = ear_clip(pts)
        if not tris:
            return
        n = len(pts)
        v = np.vstack([np.c_[pts, np.full(n, z0)], np.c_[pts, np.full(n, z1)]])
        t = np.asarray(tris)
        i = np.arange(n)
        j = (i + 1) % n
        faces = np.vstack([t[:, ::-1], t + n, np.c_[i, j, n + j], np.c_[i, n + j, n + i]])
        self.add(v, faces)


def capsule_outline(a, b, width, segments=6):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    r = max(width, 1e-4) / 2
    d = b - a
    length = float(np.hypot(*d))
    base = math.atan2(d[1], d[0]) if length > 1e-9 else 0.
    out = []
    for k in range(segments + 1):                      # around b
        t = base - math.pi / 2 + math.pi * k / segments
        out.append(b + r * np.array([math.cos(t), math.sin(t)]))
    for k in range(segments + 1):                      # around a
        t = base + math.pi / 2 + math.pi * k / segments
        out.append(a + r * np.array([math.cos(t), math.sin(t)]))
    return out


def circle_outline(cx, cy, r, segments=32):
    t = np.linspace(0, 2 * math.pi, segments, endpoint=False)
    return np.c_[cx + r * np.cos(t), cy + r * np.sin(t)]


def pad_outline(shape, w, h, ratio=0.25, segments=8):
    """Outline of a pad shape centred at the origin (unrotated)."""
    hw, hh = w / 2, h / 2
    if shape == 'circle':
        return circle_outline(0, 0, hw, 32)
    if shape == 'oval':
        r = min(hw, hh)
        if hw >= hh:
            return capsule_outline((-(hw - r), 0), (hw - r, 0), 2 * r, 10)
        return capsule_outline((0, -(hh - r)), (0, hh - r), 2 * r, 10)
    if shape == 'roundrect' and ratio > 0:
        r = min(hw, hh) * min(ratio * 2, 1.)
        out = []
        for cx, cy, start in ((hw - r, hh - r, 0), (-hw + r, hh - r, 90), (-hw + r, -hh + r, 180), (hw - r, -hh + r, 270)):
            for k in range(segments + 1):
                t = math.radians(start + 90 * k / segments)
                out.append((cx + r * math.cos(t), cy + r * math.sin(t)))
        return np.asarray(out)
    return np.array([(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)])


def ray_hits_outline(outline, angles):
    """Distance from the origin to a convex outline along each angle."""
    pts = np.asarray(outline, dtype=float)
    out = []
    nxt = np.roll(pts, -1, axis=0)
    for a in angles:
        d = np.array([math.cos(a), math.sin(a)])
        best = None
        for p, q in zip(pts, nxt):
            e = q - p
            det = d[0] * (-e[1]) - d[1] * (-e[0])
            if abs(det) < 1e-12:
                continue
            t = (p[0] * (-e[1]) - p[1] * (-e[0])) / det
            s = (d[0] * p[1] - d[1] * p[0]) / det
            if t > 0 and -1e-9 <= s <= 1 + 1e-9:
                best = t if best is None else min(best, t)
        out.append(d * (best or 0.))
    return np.asarray(out)


def ear_clip(points):
    """Triangles (index triples) of a simple polygon."""
    pts = [tuple(p) for p in points]
    n = len(pts)
    if n < 3:
        return []
    area = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))
    order = list(range(n)) if area > 0 else list(range(n))[::-1]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def inside(p, a, b, c):
        return cross(a, b, p) >= 0 and cross(b, c, p) >= 0 and cross(c, a, p) >= 0
    tris = []
    guard = 0
    while len(order) > 3 and guard < 10 * n:
        guard += 1
        for k in range(len(order)):
            i0, i1, i2 = order[k - 1], order[k], order[(k + 1) % len(order)]
            a, b, c = pts[i0], pts[i1], pts[i2]
            if cross(a, b, c) <= 1e-12:
                continue
            if any(inside(pts[m], a, b, c) for m in order if m not in (i0, i1, i2)):
                continue
            tris.append((i0, i1, i2))
            order.pop(k)
            break
        else:
            break
    if len(order) == 3:
        tris.append(tuple(order))
    return tris
