"""Copper layers (tracks, arcs, vias, zone fills), the layer stackup and board colors from a .kicad_pcb.

Copper is optional: it is only parsed and meshed when a board's *Traces* box is
on (or View → Load PCB copper layers). Every copper layer is a thin plate at its
height in the stackup, so a section plane through the board shows the inner
layers too.
"""
import math
import re
import numpy as np

COPPER_T = 0.035

# KiCad's named stackup colors (Board Setup → Board Stackup → Physical Stackup → Color)
MASK_COLORS = {
    'green': (20, 51, 36), 'light green': (91, 168, 12), 'saturated green': (13, 104, 11),
    'red': (181, 19, 21), 'light red': (210, 40, 14), 'red/orange': (239, 53, 41),
    'blue': (2, 59, 162), 'light blue 1': (54, 79, 116), 'light blue 2': (61, 85, 130),
    'green/blue': (21, 70, 80), 'black': (11, 11, 11), 'white': (245, 245, 245),
    'purple': (32, 2, 53), 'light purple': (119, 31, 91), 'yellow': (194, 195, 0),
    'matte black': (11, 11, 11), 'matte green': (20, 51, 36),
}
SILK_COLORS = {'white': (245, 245, 245), 'black': (8, 8, 8), 'yellow': (194, 195, 0), 'blue': (2, 59, 162),
               'red': (181, 19, 21), 'green': (20, 51, 36), 'purple': (32, 2, 53)}
SUBSTRATE_COLORS = {'fr4 natural': (109, 116, 75), 'pti natural': (98, 97, 76)}

# copper plate colors: outer layers copper, inner layers shifted so they can be told apart in a section
COPPER_COLORS = [(0.80, 0.52, 0.25), (0.86, 0.67, 0.30), (0.72, 0.46, 0.38), (0.84, 0.60, 0.42),
                 (0.70, 0.55, 0.30), (0.88, 0.50, 0.32)]


def parse_color(text, table=MASK_COLORS):
    """RGB 0..1 from a KiCad stackup color: a name ('Green') or '#RRGGBB[AA]'; None if unknown."""
    if not text:
        return None
    text = str(text).strip()
    m = re.fullmatch(r'#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?', text)
    if m:
        h = m.group(1)
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    rgb = table.get(text.lower())
    return tuple(c / 255 for c in rgb) if rgb else None


def shade_mask(rgb):
    """A solder mask color as KiCad's 3D view shows it: ~83 % opaque over the FR4 core, lifted a little for our lighting."""
    core = np.array([109, 116, 75]) / 255
    out = (np.asarray(rgb) * 0.83 + core * 0.17) * 1.25
    return tuple(float(v) for v in np.clip(out, 0, 1))


def stackup(root, thickness):
    """({copper layer: (z0, z1)} in SCAD frame (Z = 0 at the bottom of the board), colors dict).

    Layers come from the stackup when the board has one; otherwise from the layer table, evenly spaced.
    """
    from .kicad_import import child, children, numbers
    colors = {}
    copper = []
    setup = child(root, 'setup')
    stack = child(setup, 'stackup') if setup is not None else None
    sequence = []                       # (name, kind, thickness) top to bottom
    if stack is not None:
        for layer in children(stack, 'layer'):
            name = str(layer[1]) if len(layer) > 1 else ''
            kind = child(layer, 'type')
            kind = str(kind[1]).lower() if kind is not None and len(kind) > 1 else ''
            t = numbers(child(layer, 'thickness'), 1)
            color = child(layer, 'color')
            color = str(color[1]) if color is not None and len(color) > 1 else ''
            if name in ('F.Mask', 'B.Mask') and color:
                colors.setdefault('mask', color)
                colors[name] = color
            elif name in ('F.SilkS', 'B.SilkS') and color:
                colors.setdefault('silk', color)
            elif 'core' in kind or 'prepreg' in kind or name.startswith('dielectric'):
                if color:
                    colors.setdefault('substrate', color)
            sequence.append((name, kind, t[0] if t else 0.))
    copper_names = [n for n, k, _ in sequence if n.endswith('.Cu')]
    if not copper_names:
        layers = child(root, 'layers')
        if layers is not None:
            copper_names = [str(l[1]) for l in layers[1:] if isinstance(l, list) and len(l) > 1 and str(l[1]).endswith('.Cu')]
    if not copper_names:
        copper_names = ['F.Cu', 'B.Cu']
    copper_names = sorted(set(copper_names), key=copper_order)
    z = {}
    inner = [n for n in copper_names if n not in ('F.Cu', 'B.Cu')]
    # heights of inner layers from the dielectric thicknesses (top to bottom), scaled onto the board
    stack_core = [(n, t) for n, k, t in sequence if n.endswith('.Cu') or 'core' in k or 'prepreg' in k or n.startswith('dielectric')]
    total = sum(t for _, t in stack_core)
    depth = {}
    if total > 0:
        acc = 0.
        for name, t in stack_core:
            if name.endswith('.Cu'):
                depth[name] = acc / total
            acc += t
    for k, name in enumerate(inner, 1):
        d = depth.get(name, k / (len(inner) + 1))
        mid = thickness * (1 - d)
        z[name] = (mid - COPPER_T / 2, mid + COPPER_T / 2)
    z['F.Cu'] = (thickness, thickness + COPPER_T * 0.97)
    z['B.Cu'] = (-COPPER_T * 0.97, 0.)
    return {n: z[n] for n in copper_names if n in z}, colors


def copper_order(name):
    if name == 'F.Cu':
        return 0
    if name == 'B.Cu':
        return 10 ** 6
    m = re.match(r'In(\d+)\.Cu', name)
    return int(m.group(1)) if m else 5000


def _layers(node, available):
    from .kicad_import import child
    names = []
    for key in ('layer', 'layers'):
        found = child(node, key)
        if found is not None:
            names += [str(v) for v in found[1:] if isinstance(v, str)]
    out = []
    for name in names:
        if name in ('*.Cu', 'F&B.Cu'):
            out += list(available)
        elif name in available:
            out.append(name)
    return out


def _xy(node, key):
    from .kicad_import import child, numbers
    found = child(node, key)
    values = numbers(found, 2) if found is not None else []
    return tuple(values) if len(values) == 2 else None


def triangulate(points):
    """Triangles of a simple (possibly fractured, KiCad zone fill) polygon."""
    pts = np.asarray(points, dtype=np.float64)
    if len(pts) > 2 and np.allclose(pts[0], pts[-1]):
        pts = pts[:-1]
    if len(pts) < 3:
        return pts, np.zeros((0, 3), np.int64)
    try:
        import mapbox_earcut
        tri = mapbox_earcut.triangulate_float64(pts, np.array([len(pts)], dtype=np.uint32))
        return pts, np.asarray(tri, dtype=np.int64).reshape(-1, 3)
    except ImportError:
        from .meshgen import ear_clip
        return pts, np.asarray(ear_clip(pts), dtype=np.int64).reshape(-1, 3)


def copper_mesh(board, root):
    """(vertices, faces, face colors) of every copper layer, or None when the board has no copper items.

    Also returns the per-layer face counts: {layer: count}.
    """
    from .kicad_import import arc_points, child, children, numbers
    from .meshgen import MeshBuilder, circle_outline
    heights, _ = stackup(root, board.thickness)
    available = list(heights)
    per_layer = {name: MeshBuilder() for name in available}
    for node in root[1:]:
        if not isinstance(node, list) or not node:
            continue
        kind = node[0]
        if kind in ('segment', 'arc'):
            layer = _layers(node, available)
            if not layer:
                continue
            width = (numbers(child(node, 'width'), 1) or [0.2])[0]
            start, end = _xy(node, 'start'), _xy(node, 'end')
            if start is None or end is None:
                continue
            if kind == 'arc' and _xy(node, 'mid') is not None:
                pts = arc_points(start, _xy(node, 'mid'), end, step_deg=10.)
            else:
                pts = [start, end]
            pts = [board.to_scad(x, y) for x, y in pts]
            z0, z1 = heights[layer[0]]
            for a, b in zip(pts, pts[1:]):
                per_layer[layer[0]].capsule(a, b, width, z0, z1, segments=4)
        elif kind == 'via':
            at = _xy(node, 'at')
            if at is None:
                continue
            size = (numbers(child(node, 'size'), 1) or [0.6])[0]
            drill = (numbers(child(node, 'drill'), 1) or [0.3])[0]
            span = _layers(node, available) or ['F.Cu', 'B.Cu']
            ordered = sorted(span, key=copper_order)
            first, last = ordered[0], ordered[-1]
            # blind / buried vias connect every layer between their two ends
            between = [n for n in available if copper_order(first) <= copper_order(n) <= copper_order(last)]
            x, y = board.to_scad(*at)
            outer, hole = circle_outline(x, y, size / 2, 16), circle_outline(x, y, drill / 2, 16)
            for name in between:
                per_layer[name].ring(outer, hole, *heights[name])
            low = min(heights[first][0], heights[last][0])
            high = max(heights[first][1], heights[last][1])
            per_layer[first].ring(hole, circle_outline(x, y, max(drill / 2 - 0.03, 0.01), 16), low, high)
        elif kind == 'zone':
            for fill in children(node, 'filled_polygon'):
                layer = _layers(fill, available) or _layers(node, available)[:1]
                pts_node = child(fill, 'pts')
                if not layer or pts_node is None:
                    continue
                pts = [tuple(numbers(p, 2)) for p in pts_node[1:] if isinstance(p, list) and p and p[0] == 'xy']
                if len(pts) < 3:
                    continue
                scad = [board.to_scad(x, y) for x, y in pts]
                vertices, tris = triangulate(scad)
                if not len(tris):
                    continue
                z0, z1 = heights[layer[0]]
                n = len(vertices)
                v = np.vstack([np.c_[vertices, np.full(n, z0)], np.c_[vertices, np.full(n, z1)]])
                i = np.arange(n)
                j = (i + 1) % n
                faces = np.vstack([tris[:, ::-1], tris + n, np.c_[i, j, n + j], np.c_[i, n + j, n + i]])
                per_layer[layer[0]].add(v, faces)
    vertices, faces, colors, counts = [], [], [], {}
    offset = 0
    for index, name in enumerate(available):
        mesh = per_layer[name].mesh()
        if mesh is None:
            continue
        v, f = mesh
        rgb = COPPER_COLORS[0 if name in ('F.Cu', 'B.Cu') else 1 + (index - 1) % (len(COPPER_COLORS) - 1)]
        vertices.append(v)
        faces.append(f.astype(np.int64) + offset)
        colors.append(np.tile(np.array([*rgb, 1.], np.float32), (len(f), 1)))
        counts[name] = len(f)
        offset += len(v)
    if not vertices:
        return None, counts
    return (np.vstack(vertices).astype(np.float32), np.vstack(faces).astype(np.uint32), np.vstack(colors)), counts
