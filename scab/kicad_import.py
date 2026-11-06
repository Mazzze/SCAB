"""Read a KiCad board (.kicad_pcb) into 3D reference geometry and SCAD data.

* Board outline from Edge.Cuts (lines, arcs, circles, rectangles, polygons,
  also inside footprints), cutouts, drilled / NPTH holes, board thickness.
* Footprints: reference, value, library id, position, rotation, side, pads,
  courtyard and 3D models. VRML models (.wrl, 0.1 inch units) are placed with
  KiCad's model transform; a .step reference uses a sibling .wrl when present;
  footprints without a usable model get a courtyard box (flagged).
* Coordinates are converted to the SCAD frame: millimetres, Y up, Z = 0 at
  the bottom of the board, origin at the aux (drill/place) origin when set,
  otherwise at the lower-left corner of the board outline.

The board body and pads are meshed by OpenSCAD so the outline is exact.
"""
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import numpy as np
from . import vrml
from .meshio import load_mesh

BOARD_COLOR = (0.11, 0.38, 0.22, 1.)
PAD_COLOR = (0.78, 0.66, 0.36, 1.)
PLACEHOLDER_COLOR = (0.55, 0.57, 0.62, 0.85)
SILK_COLOR = (0.93, 0.93, 0.9, 1.)


# ------------------------------------------------------------- S-expressions
SEXPR = re.compile(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+')


def parse_sexpr(text):
    stack = [[]]
    for m in SEXPR.finditer(text):
        token = m.group(0)
        if token == '(':
            stack.append([])
        elif token == ')':
            if len(stack) > 1:
                node = stack.pop()
                stack[-1].append(node)
        elif token.startswith('"'):
            stack[-1].append(Str(token[1:-1].replace('\\"', '"').replace('\\\\', '\\')))
        else:
            stack[-1].append(token)
    return stack[0][0] if stack[0] else []


class Str(str):
    """A quoted string (kept distinct from bare atoms)."""


def children(node, name):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def child(node, name):
    found = children(node, name)
    return found[0] if found else None


def numbers(node, count=None):
    if node is None:
        return []
    out = []
    for v in node[1:]:
        if isinstance(v, str) and not isinstance(v, Str):
            try:
                out.append(float(v))
            except ValueError:
                pass
    return out[:count] if count else out


def xyz(node, default=(0., 0., 0.)):
    inner = child(node, 'xyz') if node is not None else None
    values = numbers(inner, 3) if inner is not None else []
    return list(values) + list(default[len(values):]) if values else list(default)


def layer_of(node):
    layer = child(node, 'layer')
    return str(layer[1]) if layer is not None and len(layer) > 1 else ''


# ------------------------------------------------------------------- model
@dataclass
class Pad:
    kind: str            # smd | thru_hole | np_thru_hole | connect
    shape: str
    x: float
    y: float
    angle: float
    size: tuple
    drill: float = 0.
    drill_oval: tuple = None
    layers: tuple = ()
    roundrect: float = 0.


@dataclass
class Model3D:
    path: str
    offset: list
    scale: list
    rotate: list
    hide: bool = False


@dataclass
class Footprint:
    ref: str
    value: str
    lib: str
    x: float
    y: float
    angle: float
    side: str
    pads: list = field(default_factory=list)
    courtyard: list = field(default_factory=list)   # [(x, y)] local points
    models: list = field(default_factory=list)
    edge: list = field(default_factory=list)        # local Edge.Cuts segments
    attributes: tuple = ()
    height_hint: float = None
    silk_lines: list = field(default_factory=list)
    silk_circles: list = field(default_factory=list)
    silk_text: list = field(default_factory=list)


@dataclass
class SilkText:
    text: str
    x: float              # board KiCad coordinates
    y: float
    angle: float
    size: float
    thickness: float
    justify: tuple
    side: str


@dataclass
class Board:
    path: Path
    thickness: float = 1.6
    segments: list = field(default_factory=list)    # board-frame polylines [(x, y), ...] (KiCad coordinates)
    circles: list = field(default_factory=list)     # [(cx, cy, r)] KiCad coordinates
    footprints: list = field(default_factory=list)
    origin: tuple = (0., 0.)                        # KiCad coordinates of the SCAD origin
    aux_origin: tuple = None
    title: str = ''
    silk_lines: list = field(default_factory=list)   # (side, [(x, y), ...] board coords, width)
    silk_circles: list = field(default_factory=list) # (side, cx, cy, r, width)
    silk_text: list = field(default_factory=list)

    def to_scad(self, x, y):
        return x - self.origin[0], self.origin[1] - y


# --------------------------------------------------------------- geometry
def arc_points(start, mid, end, step_deg=6.):
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(p[1] - uy, p[0] - ux) for p in (start, mid, end))

    def ccw(a, b):
        return (b - a) % (2 * math.pi)
    if ccw(a1, a2) <= ccw(a1, a3):
        sweep = ccw(a1, a3)
    else:
        sweep = -ccw(a3, a1)
    n = max(2, int(abs(math.degrees(sweep)) / step_deg) + 1)
    return [(ux + r * math.cos(a1 + sweep * i / n), uy + r * math.sin(a1 + sweep * i / n)) for i in range(n + 1)]


def legacy_arc(center, start, angle):
    cx, cy = center
    r = math.hypot(start[0] - cx, start[1] - cy)
    a0 = math.atan2(start[1] - cy, start[0] - cx)
    sweep = math.radians(angle)
    n = max(2, int(abs(angle) / 6) + 1)
    return [(cx + r * math.cos(a0 + sweep * i / n), cy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def graphic_points(node):
    """Polyline points for a gr_* / fp_* graphic, or ('circle', cx, cy, r)."""
    kind = node[0].split('_', 1)[1]
    pt = lambda name: tuple(numbers(child(node, name), 2))
    if kind == 'line':
        return [pt('start'), pt('end')]
    if kind == 'arc':
        if child(node, 'mid') is not None:
            return arc_points(pt('start'), pt('mid'), pt('end'))
        angle = numbers(child(node, 'angle'), 1)
        return legacy_arc(pt('start'), pt('end'), angle[0] if angle else 90.)
    if kind == 'circle':
        cx, cy = pt('center')
        ex, ey = pt('end')
        return ('circle', cx, cy, math.hypot(ex - cx, ey - cy))
    if kind == 'rect':
        (x1, y1), (x2, y2) = pt('start'), pt('end')
        return [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
    if kind == 'poly':
        pts = child(node, 'pts')
        out = []
        for item in (pts or [])[1:]:
            if isinstance(item, list) and item[0] == 'xy':
                out.append(tuple(numbers(item, 2)))
            elif isinstance(item, list) and item[0] == 'arc':
                out.extend(arc_points(tuple(numbers(child(item, 'start'), 2)), tuple(numbers(child(item, 'mid'), 2)),
                                      tuple(numbers(child(item, 'end'), 2))))
        if out and out[0] != out[-1]:
            out.append(out[0])
        return out
    return []


def local_to_board(fp, x, y):
    """Footprint-local KiCad coordinates -> board KiCad coordinates (Y down, CCW angles)."""
    a = math.radians(fp.angle)
    c, s = math.cos(a), math.sin(a)
    return fp.x + x * c + y * s, fp.y - x * s + y * c


def chain(segments, tolerance=1e-3):
    """Join polylines with matching ends into closed loops."""
    pieces = [list(seg) for seg in segments if len(seg) >= 2]
    loops = []
    while pieces:
        loop = pieces.pop(0)
        changed = True
        while changed and not _closed(loop, tolerance):
            changed = False
            for i, piece in enumerate(pieces):
                if _near(loop[-1], piece[0], tolerance):
                    loop += piece[1:]
                elif _near(loop[-1], piece[-1], tolerance):
                    loop += piece[::-1][1:]
                elif _near(loop[0], piece[-1], tolerance):
                    loop = piece[:-1] + loop
                elif _near(loop[0], piece[0], tolerance):
                    loop = piece[::-1][:-1] + loop
                else:
                    continue
                pieces.pop(i)
                changed = True
                break
        if len(loop) >= 3:
            if _closed(loop, tolerance):
                loop = loop[:-1]
            loops.append(loop)
    return loops


def _near(a, b, tolerance):
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def _closed(loop, tolerance):
    return len(loop) > 2 and _near(loop[0], loop[-1], tolerance)


def area(loop):
    return .5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(loop, loop[1:] + loop[:1]))


# ------------------------------------------------------------------ parsing
def parse_board(path):
    path = Path(path)
    root = parse_sexpr(path.read_text(encoding='utf-8', errors='replace'))
    if not root or root[0] != 'kicad_pcb':
        raise ValueError(f'{path.name} is not a KiCad board file')
    board = Board(path)
    general = child(root, 'general')
    thickness = numbers(child(general, 'thickness'), 1) if general is not None else []
    if thickness:
        board.thickness = thickness[0]
    setup = child(root, 'setup')
    if setup is not None:
        aux = numbers(child(setup, 'aux_axis_origin'), 2)
        if len(aux) == 2 and (aux[0] or aux[1]):
            board.aux_origin = tuple(aux)
    block = child(root, 'title_block')
    if block is not None and child(block, 'title') is not None:
        board.title = str(child(block, 'title')[1])
    for node in root[1:]:
        if isinstance(node, list) and node and node[0] in ('gr_line', 'gr_arc', 'gr_circle', 'gr_rect', 'gr_poly') \
                and layer_of(node) == 'Edge.Cuts':
            pts = graphic_points(node)
            if pts and pts[0] == 'circle':
                board.circles.append(pts[1:])
            elif pts:
                board.segments.append(pts)
        elif isinstance(node, list) and node and node[0] in ('gr_line', 'gr_arc', 'gr_circle', 'gr_rect', 'gr_poly') \
                and layer_of(node).endswith('SilkS'):
            side = 'bottom' if layer_of(node).startswith('B.') else 'top'
            pts = graphic_points(node)
            width = stroke_width(node)
            if pts and pts[0] == 'circle':
                board.silk_circles.append((side, *pts[1:], width))
            elif pts:
                board.silk_lines.append((side, pts, width))
        elif isinstance(node, list) and node and node[0] == 'gr_text' and layer_of(node).endswith('SilkS'):
            text = silk_text(node, None, '', board_level=True)
            if text:
                board.silk_text.append(text)
    for node in children(root, 'footprint') + children(root, 'module'):
        fp = parse_footprint(node)
        board.footprints.append(fp)
        for side, local, width in fp.silk_lines:
            board.silk_lines.append((side, [local_to_board(fp, x, y) for x, y in local], width))
        for side, cx, cy, r, width in fp.silk_circles:
            bx, by = local_to_board(fp, cx, cy)
            board.silk_circles.append((side, bx, by, r, width))
        board.silk_text.extend(fp.silk_text)
        for local in fp.edge:
            if local and local[0] == 'circle':
                cx, cy = local_to_board(fp, local[1], local[2])
                board.circles.append((cx, cy, local[3]))
            else:
                board.segments.append([local_to_board(fp, x, y) for x, y in local])
    pts = [p for seg in board.segments for p in seg] + [(c[0] - c[2], c[1] - c[2]) for c in board.circles] + \
          [(c[0] + c[2], c[1] + c[2]) for c in board.circles]
    if not pts:
        pts = [(fp.x, fp.y) for fp in board.footprints] or [(0., 0.)]
    if board.aux_origin:
        board.origin = board.aux_origin
    else:
        board.origin = (min(p[0] for p in pts), max(p[1] for p in pts))
    return board


def stroke_width(node):
    stroke = child(node, 'stroke')
    width = numbers(child(stroke, 'width'), 1) if stroke is not None else numbers(child(node, 'width'), 1)
    return width[0] if width else .12


def silk_text(node, fp, ref, board_level=False):
    """A visible silkscreen text (fp_text / property / gr_text) in board coordinates."""
    if node[0] == 'fp_text':
        value = str(node[2]) if len(node) > 2 else ''
    elif node[0] == 'property':
        value = str(node[2]) if len(node) > 2 else ''
    else:
        value = str(node[1]) if len(node) > 1 else ''
    if 'hide' in node or any(isinstance(c, list) and c and c[0] == 'hide' and 'yes' in c for c in node):
        return None
    effects = child(node, 'effects')
    if effects is not None and ('hide' in effects or any(isinstance(c, list) and c and c[0] == 'hide' and 'yes' in c for c in effects)):
        return None
    value = value.replace('${REFERENCE}', ref or '').replace('%R', ref or '')
    if not value.strip() or '${' in value:
        return None
    at = numbers(child(node, 'at'))
    x, y = (at + [0., 0.])[:2]
    angle = at[2] if len(at) > 2 else 0.
    size, thickness, justify = 1., .15, ()
    if effects is not None:
        font = child(effects, 'font')
        if font is not None:
            size = (numbers(child(font, 'size'), 1) or [1.])[0]
            thickness = (numbers(child(font, 'thickness'), 1) or [.15])[0]
        just = child(effects, 'justify')
        if just is not None:
            justify = tuple(str(v) for v in just[1:])
    if fp is not None and not board_level:
        x, y = local_to_board(fp, x, y)
    side = 'bottom' if layer_of(node).startswith('B.') else 'top'
    return SilkText(value, x, y, angle, size, thickness, justify, side)


def parse_footprint(node):
    lib = str(node[1]) if len(node) > 1 and isinstance(node[1], str) else ''
    at = numbers(child(node, 'at'))
    x, y = (at + [0., 0.])[:2]
    angle = at[2] if len(at) > 2 else 0.
    side = 'bottom' if layer_of(node).startswith('B.') else 'top'
    ref, value = '', ''
    for prop in children(node, 'property'):
        if len(prop) > 2 and prop[1] == 'Reference':
            ref = str(prop[2])
        elif len(prop) > 2 and prop[1] == 'Value':
            value = str(prop[2])
    for text in children(node, 'fp_text'):
        if len(text) > 2 and text[1] == 'reference' and not ref:
            ref = str(text[2])
        elif len(text) > 2 and text[1] == 'value' and not value:
            value = str(text[2])
    attr = child(node, 'attr')
    fp = Footprint(ref or '?', value, lib, x, y, angle, side, attributes=tuple(str(a) for a in (attr or [])[1:]))
    for pad in children(node, 'pad'):
        kind = str(pad[2]) if len(pad) > 2 else 'smd'
        shape = str(pad[3]) if len(pad) > 3 else 'rect'
        pat = numbers(child(pad, 'at'))
        size = tuple(numbers(child(pad, 'size'), 2) or (1., 1.))
        drill_node = child(pad, 'drill')
        drill, oval = 0., None
        if drill_node is not None:
            values = numbers(drill_node)
            if 'oval' in drill_node and len(values) >= 2:
                oval = (values[0], values[1])
                drill = min(values[:2])
            elif values:
                drill = values[0]
        layers_node = child(pad, 'layers')
        layers = tuple(str(v) for v in (layers_node or [])[1:])
        ratio = numbers(child(pad, 'roundrect_rratio'), 1)
        fp.pads.append(Pad(kind, shape, (pat + [0, 0])[0], (pat + [0, 0])[1], pat[2] if len(pat) > 2 else angle,
                           size, drill, oval, layers, ratio[0] if ratio else 0.))
    for graphic in node[1:]:
        if not (isinstance(graphic, list) and graphic and graphic[0] in ('fp_line', 'fp_arc', 'fp_circle', 'fp_rect', 'fp_poly')):
            continue
        layer = layer_of(graphic)
        pts = graphic_points(graphic)
        if not pts:
            continue
        if layer.endswith('SilkS'):
            silk_side = 'bottom' if layer.startswith('B.') else 'top'
            if pts[0] == 'circle':
                fp.silk_circles.append((silk_side, pts[1], pts[2], pts[3], stroke_width(graphic)))
            else:
                fp.silk_lines.append((silk_side, list(pts), stroke_width(graphic)))
            continue
        if layer.endswith('CrtYd'):
            if pts[0] == 'circle':
                _, cx, cy, r = pts
                fp.courtyard += [(cx - r, cy - r), (cx + r, cy + r)]
            else:
                fp.courtyard += list(pts)
        elif layer == 'Edge.Cuts':
            fp.edge.append(pts)
    for text_node in children(node, 'fp_text') + [p for p in children(node, 'property') if layer_of(p).endswith('SilkS')]:
        if not layer_of(text_node).endswith('SilkS'):
            continue
        text = silk_text(text_node, fp, ref)
        if text:
            fp.silk_text.append(text)
    for model in children(node, 'model'):
        hide = 'hide' in model or any(isinstance(m, list) and m and m[0] == 'hide' and 'yes' in m for m in model)
        fp.models.append(Model3D(str(model[1]), xyz(child(model, 'offset')) if child(model, 'offset') is not None
                                 else xyz(child(model, 'at')), xyz(child(model, 'scale'), (1., 1., 1.)),
                                 xyz(child(model, 'rotate')), hide))
    name = lib.split(':')[-1]
    m = re.search(r'_H(\d+(?:\.\d+)?)(?:mm)?(?:_|$)', name) or re.search(r'^(?:CP_Elec|C_Elec|CP_Radial|L_)\w*?_[\d.]+x(\d+(?:\.\d+)?)', name)
    if m:
        try:
            fp.height_hint = float(m.group(1))
        except ValueError:
            pass
    return fp


# ----------------------------------------------------------------- models
def model_search_dirs(board_dir):
    names = [k for k in os.environ if re.fullmatch(r'KICAD\d*_3DMODEL_DIR|KISYS3DMOD', k)]
    dirs = {name: os.environ[name] for name in names}
    candidates = ['/usr/share/kicad/3dmodels', '/usr/local/share/kicad/3dmodels',
                  '/Applications/KiCad/KiCad.app/Contents/SharedSupport/3dmodels',
                  str(Path.home() / '.local/share/kicad/3dmodels')]
    for version in ('9.0', '8.0', '7.0', '6.0'):
        candidates.append(rf'C:\Program Files\KiCad\{version}\share\kicad\3dmodels')
        candidates.append(str(Path.home() / f'.local/share/kicad/{version}/3dmodels'))
    return dirs, [c for c in candidates if Path(c).is_dir()]


def resolve_model(path_text, board_dir):
    board_dir = Path(board_dir).resolve()
    env, fallbacks = model_search_dirs(board_dir)
    text = path_text.replace('\\', '/')

    def substitute(match):
        name = match.group(1)
        if name == 'KIPRJMOD':
            return str(board_dir)
        if name in env:
            return env[name]
        return match.group(0)
    expanded = re.sub(r'\$\{([^}]+)\}', substitute, text)
    expanded = re.sub(r'\$\(([^)]+)\)', substitute, expanded)
    candidates = []
    if '${' not in expanded and '$(' not in expanded:
        candidate = Path(expanded)
        if not candidate.is_absolute():
            candidate = Path(board_dir) / candidate
        candidates.append(candidate)
    # unresolved library variable: try every known library directory
    tail = re.sub(r'^\$\{[^}]+\}/?', '', text)
    for base in fallbacks:
        candidates.append(Path(base) / tail)
    from . import step
    use_step = step.available()
    out = []
    for candidate in candidates:
        suffix = candidate.suffix.lower()
        if suffix in ('.step', '.stp'):
            # the referenced STEP first (when it can be read), then a VRML twin
            if use_step:
                out.append(candidate)
            out.append(candidate.with_suffix('.wrl'))
        else:
            out.append(candidate)
            if use_step:
                out += [candidate.with_suffix('.step'), candidate.with_suffix('.stp')]
    for candidate in out:
        if candidate.suffix.lower() in ('.wrl', '.step', '.stp') and candidate.is_file():
            return candidate
    return None


_model_cache = {}


def load_model(path, cache_dir=None):
    """(meshes, units_to_mm) for a .wrl (0.1 inch units) or .step (mm) model."""
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _model_cache:
        if path.suffix.lower() in ('.step', '.stp'):
            from . import step
            _model_cache[key] = (step.load(path, cache_dir), 1.)
        else:
            _model_cache[key] = (vrml.load(path), 2.54)
    return _model_cache[key]


def rot(axis, degrees):
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    m = np.eye(4)
    i, j = [(1, 2), (2, 0), (0, 1)][axis]
    m[i, i], m[i, j], m[j, i], m[j, j] = c, -s, s, c
    return m


def translate(x, y, z):
    m = np.eye(4)
    m[:3, 3] = (x, y, z)
    return m


def scale(x, y, z):
    return np.diag([x, y, z, 1.])


def footprint_matrix(board, fp):
    """KiCad 3D placement of a footprint, in the SCAD frame."""
    sx, sy = board.to_scad(fp.x, fp.y)
    m = translate(sx, sy, board.thickness if fp.side == 'top' else 0.) @ rot(2, fp.angle)
    if fp.side == 'bottom':
        m = m @ rot(1, 180) @ rot(2, 180)
    return m


def model_matrix(model, units):
    ox, oy, oz = model.offset
    rx, ry, rz = model.rotate
    return (translate(ox, oy, oz) @ rot(2, -rz) @ rot(1, -ry) @ rot(0, -rx)
            @ scale(*model.scale) @ scale(units, units, units))


# ------------------------------------------------------------------ meshes
@dataclass
class PartMesh:
    ref: str
    footprint: Footprint
    vertices: np.ndarray
    faces: np.ndarray
    colors: np.ndarray        # per face rgba
    has_model: bool
    position: tuple           # SCAD x, y
    rotation: float

    @property
    def box(self):
        return self.vertices.min(axis=0), self.vertices.max(axis=0)


def box_mesh(lo, hi, rgba):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], dtype=np.float32)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
                  [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]], dtype=np.uint32)
    return v, f, np.tile(np.asarray(rgba, dtype=np.float32), (len(f), 1))


def part_meshes(board, cache_dir=None, files=None):
    out = []
    board_dir = board.path.parent
    for fp in board.footprints:
        if 'board_only' in fp.attributes and not fp.models:
            continue
        placement = footprint_matrix(board, fp)
        vertices, faces, colors = [], [], []
        offset = 0
        for model in fp.models:
            if model.hide:
                continue
            path = resolve_model(model.path, board_dir)
            if path is None:
                continue
            if files is not None:
                files.add(path)
            try:
                shapes, units = load_model(path, cache_dir)
            except Exception:   # unreadable model: fall back to the courtyard box
                continue
            matrix = placement @ model_matrix(model, units)
            for v, f, rgba in shapes:
                placed = (np.c_[v, np.ones(len(v))] @ matrix.T)[:, :3].astype(np.float32)
                vertices.append(placed)
                faces.append(f.astype(np.int64) + offset)
                colors.append(np.tile(np.asarray(rgba, dtype=np.float32), (len(f), 1)))
                offset += len(v)
        sx, sy = board.to_scad(fp.x, fp.y)
        if vertices:
            out.append(PartMesh(fp.ref, fp, np.vstack(vertices), np.vstack(faces).astype(np.uint32), np.vstack(colors),
                                True, (sx, sy), fp.angle))
            continue
        local = fp.courtyard or [(p.x - p.size[0] / 2, p.y - p.size[1] / 2) for p in fp.pads] + \
            [(p.x + p.size[0] / 2, p.y + p.size[1] / 2) for p in fp.pads]
        if not local or fp.lib.lower().startswith('mountinghole'):
            continue
        pts = np.array([board.to_scad(*local_to_board(fp, x, y)) for x, y in local])
        height = fp.height_hint if fp.height_hint and fp.height_hint < 30 else 1.
        z0, z1 = (board.thickness, board.thickness + height) if fp.side == 'top' else (-height, 0.)
        v, f, c = box_mesh((*pts.min(axis=0), z0), (*pts.max(axis=0), z1), PLACEHOLDER_COLOR)
        out.append(PartMesh(fp.ref, fp, v, f, c, False, (sx, sy), fp.angle))
    return out


def outline_loops(board):
    """SCAD-frame loops: (outer, [holes]) from Edge.Cuts."""
    loops = [[board.to_scad(x, y) for x, y in loop] for loop in chain(board.segments)]
    circles = [(*board.to_scad(cx, cy), r) for cx, cy, r in board.circles]
    if not loops and circles:
        biggest = max(circles, key=lambda c: c[2])
        circles.remove(biggest)
        cx, cy, r = biggest
        loops = [[(cx + r * math.cos(a), cy + r * math.sin(a)) for a in np.linspace(0, 2 * math.pi, 96, endpoint=False)]]
    if not loops:
        return None, []
    outer = max(loops, key=lambda loop: abs(area(loop)))
    holes = [loop for loop in loops if loop is not outer]
    return outer, holes + [('circle', cx, cy, r) for cx, cy, r in circles]


def drill_holes(board):
    holes = []
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.kind in ('thru_hole', 'np_thru_hole') and pad.drill > 0:
                bx, by = local_to_board(fp, pad.x, pad.y)
                sx, sy = board.to_scad(bx, by)
                holes.append((sx, sy, pad.drill, pad.kind == 'np_thru_hole', fp.ref))
    return holes


def _fmt(v):
    text = f'{v:.4f}'.rstrip('0').rstrip('.')
    return '0' if text in ('-0', '') else text


def _pts(loop):
    return '[' + ', '.join(f'[{_fmt(x)}, {_fmt(y)}]' for x, y in loop) + ']'


def board_scad(board, include_pads=False):
    """OpenSCAD source for the board body (outline, cutouts, drills) or the pads."""
    outer, holes = outline_loops(board)
    if outer is None:
        raise ValueError('The board has no closed Edge.Cuts outline')
    t = board.thickness
    if include_pads:
        lines = ['$fn = 24;']
        for fp in board.footprints:
            for pad in fp.pads:
                copper = [l for l in pad.layers if l.endswith('.Cu')]
                if not copper or pad.kind == 'np_thru_hole':
                    continue
                sides = set()
                for layer in copper:
                    if layer in ('*.Cu', 'F&B.Cu'):
                        sides |= {'F', 'B'}
                    else:
                        sides.add(layer[0])
                if fp.side == 'bottom' and pad.kind == 'smd':
                    sides = {'B'}
                bx, by = local_to_board(fp, pad.x, pad.y)
                sx, sy = board.to_scad(bx, by)
                w, h = pad.size
                if pad.shape == 'circle':
                    shape = f'circle(d = {_fmt(w)})'
                elif pad.shape == 'oval':
                    r = min(w, h) / 2
                    shape = f'hull() {{ translate([{_fmt(-(w / 2 - r))}, {_fmt(-(h / 2 - r))}]) circle(r = {_fmt(r)}); translate([{_fmt(w / 2 - r)}, {_fmt(h / 2 - r)}]) circle(r = {_fmt(r)}); }}'
                else:
                    shape = f'square([{_fmt(w)}, {_fmt(h)}], center = true)'
                if pad.drill > 0:
                    shape = f'difference() {{ {shape}; circle(d = {_fmt(pad.drill)}); }}'
                for side in sides:
                    z = t if side == 'F' else -0.035
                    lines.append(f'translate([{_fmt(sx)}, {_fmt(sy)}, {_fmt(z)}]) rotate({_fmt(pad.angle)}) '
                                 f'linear_extrude(height = 0.035) {shape};')
        return '\n'.join(lines) + '\n'
    lines = ['$fn = 48;', f'linear_extrude(height = {_fmt(t)}) difference() {{', f'    polygon({_pts(outer)});']
    for hole in holes:
        if hole[0] == 'circle':
            lines.append(f'    translate([{_fmt(hole[1])}, {_fmt(hole[2])}]) circle(r = {_fmt(hole[3])});')
        else:
            lines.append(f'    polygon({_pts(hole)});')
    for x, y, d, _, _ in drill_holes(board):
        lines.append(f'    translate([{_fmt(x)}, {_fmt(y)}]) circle(d = {_fmt(d)});')
    lines.append('}')
    return '\n'.join(lines) + '\n'


def silk_scad(board):
    """OpenSCAD source for the silkscreen (white ink) on both sides."""
    t = board.thickness
    lines = ['$fn = 16;']

    def place(side, body):
        z = t + 0.036 if side == 'top' else -0.048
        return f'translate([0, 0, {_fmt(z)}]) linear_extrude(height = 0.012) {body}'
    for side, pts, width in board.silk_lines:
        scad = [board.to_scad(x, y) for x, y in pts]
        for (x1, y1), (x2, y2) in zip(scad, scad[1:]):
            lines.append(place(side, f'hull() {{ translate([{_fmt(x1)}, {_fmt(y1)}]) circle(d = {_fmt(width)}); '
                                     f'translate([{_fmt(x2)}, {_fmt(y2)}]) circle(d = {_fmt(width)}); }}'))
    for side, cx, cy, r, width in board.silk_circles:
        x, y = board.to_scad(cx, cy)
        lines.append(place(side, f'translate([{_fmt(x)}, {_fmt(y)}]) difference() {{ circle(r = {_fmt(r + width / 2)}, $fn = 48); '
                                 f'circle(r = {_fmt(max(0, r - width / 2))}, $fn = 48); }}'))
    for text in board.silk_text:
        x, y = board.to_scad(text.x, text.y)
        angle = text.angle % 360
        if 90 < angle <= 270:           # KiCad keeps text upright
            angle -= 180
        halign = 'left' if 'left' in text.justify else 'right' if 'right' in text.justify else 'center'
        valign = 'top' if 'top' in text.justify else 'bottom' if 'bottom' in text.justify else 'center'
        if text.side == 'bottom':
            halign = {'left': 'right', 'right': 'left'}.get(halign, halign)
        body = (f'text({scad_string(text.text)}, size = {_fmt(text.size / 0.72)}, font = "Liberation Sans:style=Bold", '
                f'halign = "{halign}", valign = "{valign}");')
        mirror = 'mirror([1, 0, 0]) ' if text.side == 'bottom' else ''
        lines.append(place(text.side, f'translate([{_fmt(x)}, {_fmt(y)}]) rotate({_fmt(angle)}) {mirror}{body}'))
    return '\n'.join(lines) + '\n' if len(lines) > 1 else ''


def openscad_mesh(executable, source, cache_dir):
    """Mesh a small SCAD source with OpenSCAD (cached by content)."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((source + str(executable)).encode()).hexdigest()[:24]
    stl = cache_dir / f'kicad-{key}.stl'
    if not stl.exists():
        with tempfile.TemporaryDirectory(prefix='scab-kicad-') as tmp:
            scad = Path(tmp) / 'board.scad'
            scad.write_text(source, encoding='utf-8')
            out = Path(tmp) / 'out.stl'
            env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
            result = subprocess.run([str(executable), '-o', str(out), '--export-format', 'binstl', str(scad)],
                                    capture_output=True, text=True, env=env, timeout=600)
            if not out.exists():
                raise RuntimeError('OpenSCAD could not mesh the board:\n' + (result.stdout + result.stderr)[-4000:])
            os.replace(out, stl)
    return load_mesh(stl)


@dataclass
class BoardModel:
    board: Board
    body: tuple                # (vertices, faces) or None
    pads: tuple
    silk: tuple
    parts: list                # PartMesh
    notes: list = field(default_factory=list)
    part_offset = 3            # reference_parts(): body, pads, silkscreen, then parts

    def reference_parts(self):
        """[(vertices, faces, face_colors)] for the viewer: body, pads, silkscreen, then one entry per part."""
        out = []
        for mesh, color in ((self.body, BOARD_COLOR), (self.pads, PAD_COLOR), (self.silk, SILK_COLOR)):
            if mesh is not None:
                v, f = mesh
                out.append((v, f, np.tile(np.asarray(color, dtype=np.float32), (len(f), 1))))
            else:
                out.append((np.zeros((3, 3), np.float32), np.array([[0, 1, 2]], np.uint32), np.zeros((1, 4), np.float32)))
        for part in self.parts:
            out.append((part.vertices, part.faces, part.colors))
        return out


def step_available():
    from . import step
    return step.available()


def load_board(path, executable=None, cache_dir=None, meshes=True):
    """Board + 3D parts. meshes=False skips the OpenSCAD-meshed body / pads / silkscreen (fast path)."""
    board = parse_board(path)
    notes = []
    cache_dir = Path(cache_dir) if cache_dir else Path(path).parent / '.scab-cache'
    body = pads = silk = None
    if not meshes:
        pass
    elif executable:
        try:
            body = openscad_mesh(executable, board_scad(board), cache_dir)
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
            notes.append(f'Board body: {error}')
        try:
            pads = openscad_mesh(executable, board_scad(board, include_pads=True), cache_dir)
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
            notes.append(f'Pads: {error}')
        source = silk_scad(board)
        if source:
            try:
                silk = openscad_mesh(executable, source, cache_dir)
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
                notes.append(f'Silkscreen: {error}')
    else:
        notes.append('OpenSCAD not configured: board body not meshed')
    files = set()
    parts = part_meshes(board, cache_dir / 'step', files)
    missing = [p.ref for p in parts if not p.has_model]
    if missing:
        notes.append('No 3D model found for ' + ', '.join(missing[:12]) + (' …' if len(missing) > 12 else '') +
                     ' (courtyard box used; set KICAD8_3DMODEL_DIR' + ('' if step_available()
                                                                       else ', or pip install cascadio to read .step models') + ')')
    result = BoardModel(board, body, pads, silk, parts, notes)
    result.partial = not meshes
    result.files = sorted(str(f) for f in files)     # 3D model files, watched for changes
    return result


# ------------------------------------------------------------ SCAD include
def write_stl(path, vertices, faces):
    tri = vertices[faces].astype(np.float32)
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, lengths, out=np.zeros_like(normals), where=lengths > 0)
    records = np.zeros(len(faces), dtype=[('n', '<f4', 3), ('v', '<f4', (3, 3)), ('a', '<u2')])
    records['n'] = normals
    records['v'] = tri
    with open(path, 'wb') as f:
        f.write(b'SCAB KiCad parts'.ljust(80, b' '))
        f.write(np.uint32(len(faces)).tobytes())
        f.write(records.tobytes())


def scad_string(text):
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"') + '"'


def include_names(ident, stem):
    """(include file name, parts STL name) for a board id ('kicad' keeps the classic names)."""
    base = stem if ident == 'kicad' else ident
    return f'{base}.scad', f'{base}-parts.stl'


def write_include(model, directory, ident='kicad'):
    """Write <id>.scad (+ <id>-parts.stl) describing the board for OpenSCAD.

    Every name is prefixed with the board id: with ident "display" you get
    display_parts, display_part("J1"), display_part_box(...), display_pcb() and
    display_place() — which moves children to the board's pose (display_pose,
    a variable SCAB keeps in your file). The id "kicad" gives the classic
    kicad_* names.
    """
    board = model.board
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    include_name, stl_name = include_names(ident, board.path.stem)
    parts_stl = directory / stl_name
    if model.parts:
        vertices = np.vstack([p.vertices for p in model.parts])
        offsets = np.cumsum([0] + [len(p.vertices) for p in model.parts[:-1]])
        faces = np.vstack([p.faces.astype(np.int64) + o for p, o in zip(model.parts, offsets)])
        write_stl(parts_stl, vertices, faces)
    outer, holes = outline_loops(board)
    rel = os.path.relpath(board.path, directory).replace(os.sep, '/')
    lines = [f'// Generated by SCAB from {rel}. Re-written whenever the board is saved in KiCad — do not edit.',
             '// Board coordinates: mm, origin = ' + ('KiCad aux origin' if board.aux_origin else 'lower-left corner of the board') +
             ', Z = 0 at the bottom of the board.',
             f'// Where the board sits in the design: {ident}_pose = [[x, y, z], [rx, ry, rz]] in your file;',
             f'// {ident}_place() {{ ... }} puts geometry given in board coordinates there.', '']
    xs = [p[0] for p in outer] if outer else [0]
    ys = [p[1] for p in outer] if outer else [0]
    P = ident + '_'
    lines.append(f'{P}board_min = [{_fmt(min(xs))}, {_fmt(min(ys))}];')
    lines.append(f'{P}board_max = [{_fmt(max(xs))}, {_fmt(max(ys))}];')
    lines.append(f'{P}board_size = [{_fmt(max(xs) - min(xs))}, {_fmt(max(ys) - min(ys))}];')
    lines.append(f'{P}board_thickness = {_fmt(board.thickness)};')
    lines.append(f'{P}board_outline = {_pts(outer) if outer else "[]"};')
    cutouts = [h for h in holes if h[0] != 'circle']
    circles = [h for h in holes if h[0] == 'circle']
    lines.append(f'{P}board_cutouts = [' + ', '.join(_pts(h) for h in cutouts) + '];')
    drills = drill_holes(board)
    lines.append('// [[x, y], diameter, plated, reference]')
    lines.append(f'{P}board_holes = [' + ', '.join(
        [f'[[{_fmt(x)}, {_fmt(y)}], {_fmt(d)}, {"false" if npth else "true"}, {scad_string(ref)}]' for x, y, d, npth, ref in drills] +
        [f'[[{_fmt(cx)}, {_fmt(cy)}], {_fmt(2 * r)}, false, "Edge.Cuts"]' for _, cx, cy, r in circles]) + '];')
    mounting = [(x, y, d) for x, y, d, npth, ref in drills if ref.upper().startswith('H') or d >= 2.5]
    lines.append(f'{P}mounting_holes = [' + ', '.join(f'[[{_fmt(x)}, {_fmt(y)}], {_fmt(d)}]' for x, y, d in mounting) + '];')
    lines.append('// [reference, value, footprint, [x, y], rotation, side, [min x, y, z], [max x, y, z], has_3d_model]')
    rows = []
    for p in model.parts:
        lo, hi = p.box
        rows.append(f'    [{scad_string(p.ref)}, {scad_string(p.footprint.value)}, {scad_string(p.footprint.lib)}, '
                    f'[{_fmt(p.position[0])}, {_fmt(p.position[1])}], {_fmt(p.rotation)}, {scad_string(p.footprint.side)}, '
                    f'[{", ".join(_fmt(v) for v in lo)}], [{", ".join(_fmt(v) for v in hi)}], {"true" if p.has_model else "false"}]')
    lines.append(f'{P}parts = [\n' + ',\n'.join(rows) + '\n];')
    lines.append(f'{P}parts_top = max([for (p = {P}parts) p[7][2]]);')
    lines.append(f'''
// The record of one part, e.g. {P}part("J1")[3] is its position (board coordinates).
function {P}part(ref) = let(i = search([ref], {P}parts)[0]) i == [] ? undef : {P}parts[i];

// 3D bounding box of a part, grown by margin (handy for connector cutouts), in board coordinates.
module {P}part_box(ref, margin = 0.5, extend = [0, 0, 0]) {{
    p = {P}part(ref);
    if (!is_undef(p))
        translate(p[6] - [margin, margin, margin] - [for (e = extend) min(e, 0)])
            cube(p[7] - p[6] + 2 * [margin, margin, margin] + [for (e = extend) abs(e)]);
}}

// Children given in board coordinates, placed where the board sits in the design.
module {P}place() {{
    translate({P}pose[0]) rotate({P}pose[1]) children();
}}

// The board itself (body + 3D parts), in board coordinates. parts = false: bare board.
module {P}pcb(parts = true, board_color = "#1c6138", parts_color = "#8c8f96") {{
    color(board_color) linear_extrude(height = {P}board_thickness) difference() {{
        polygon({P}board_outline);
        for (c = {P}board_cutouts) polygon(c);
        for (h = {P}board_holes) translate(h[0]) circle(d = h[1], $fn = 32);
    }}
    if (parts) color(parts_color) import("{parts_stl.name}");
}}
''')
    target = directory / include_name
    text = '\n'.join(lines)
    if not target.exists() or target.read_text(encoding='utf-8') != text:
        target.write_text(text, encoding='utf-8')
    return target


LINK = re.compile(r'^[ \t]*//\s*scab\s*:\s*kicad(?:[ \t]+([A-Za-z_][A-Za-z0-9_]*))?\s*=\s*(\S+)', re.M)


def board_id(name, taken):
    ident = re.sub(r'[^A-Za-z0-9_]', '_', name).strip('_').lower() or 'board'
    if ident[0].isdigit():
        ident = 'b_' + ident
    base, n = ident, 2
    while ident in taken:
        ident = f'{base}{n}'
        n += 1
    return ident


def find_kicad_links(source, source_path):
    """[(id, path)] from `// scab: kicad [id] = path/board.kicad_pcb` lines (first unnamed one is 'kicad')."""
    base = Path(source_path).parent if source_path else Path.cwd()
    links, taken = [], set()
    for m in LINK.finditer(source or ''):
        ident = m.group(1)
        path = (base / m.group(2)).resolve()
        if not ident:
            ident = 'kicad' if 'kicad' not in taken else board_id(path.stem, taken)
        if ident in taken:
            continue
        taken.add(ident)
        links.append((ident, path))
    return links


def find_kicad_link(source, source_path):
    """`// scab: kicad = board/board.kicad_pcb` in a SCAD file links a board to it."""
    m = re.search(r'//\s*scab\s*:\s*kicad\s*=\s*(\S+)', source or '')
    if not m:
        return None
    base = Path(source_path).parent if source_path else Path.cwd()
    path = (base / m.group(1)).resolve()
    return path if path.is_file() else None
