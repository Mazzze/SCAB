"""Export a SCAD design to a KiCad board file (.kicad_pcb) and a placement CSV.

Sources of information, best first:

* calls to the ``pcb_*`` modules of ``assets/scadforge/pcb.scad`` found by the
  tracer, at their real world position (inside user modules, loops and
  transforms): exact outline with true arcs, holes, mounting holes, parts;
* otherwise an outline polygon from an OpenSCAD cross-section (SVG) of the
  model at the board plane.

KiCad receives footprints by library id with reference, value, position,
rotation and side. Use *Tools → Update Footprints from Library* in KiCad to
load their pads, then route as usual.
"""
from dataclasses import dataclass, field
import csv
import io
import math
import re
import uuid
import numpy as np

PART_MODULES = ('pcb_part', 'pcb_connector')
HOLE_MODULES = ('pcb_hole',)
MOUNT_MODULES = ('pcb_mounting_hole',)
BOARD_MODULES = ('pcb_board',)


@dataclass
class Footprint:
    ref: str
    footprint: str
    x: float
    y: float
    rotation: float
    side: str = 'top'
    value: str = ''


@dataclass
class Board:
    lines: list = field(default_factory=list)      # ((x1, y1), (x2, y2))
    arcs: list = field(default_factory=list)       # (start, mid, end)
    circles: list = field(default_factory=list)    # (center, radius)
    polygons: list = field(default_factory=list)   # [(x, y), ...]
    footprints: list = field(default_factory=list)
    thickness: float = 1.6
    notes: list = field(default_factory=list)

    def points(self):
        out = []
        for a, b in self.lines:
            out += [a, b]
        for arc in self.arcs:
            out += list(arc)
        for c, r in self.circles:
            out += [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
        for poly in self.polygons:
            out += poly
        for f in self.footprints:
            out.append((f.x, f.y))
        return out

    @property
    def has_outline(self):
        return bool(self.lines or self.arcs or self.polygons)


def _apply(matrix, x, y):
    p = matrix @ np.array([x, y, 0., 1.])
    return float(p[0]), float(p[1])


def _rotation(matrix):
    return math.degrees(math.atan2(matrix[1, 0], matrix[0, 0]))


def _num(v, default=0.):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _xy(v):
    if isinstance(v, list) and len(v) >= 2:
        return _num(v[0]), _num(v[1])
    return 0., 0.


def _inside(call, names):
    f = call.parent
    while f is not None:
        if f.kind == 'module' and f.node.name in names:
            return True
        f = f.parent
    return False


def from_trace(result):
    """Collect board geometry and footprints from pcb_* module calls."""
    tracer = getattr(result, 'tracer', None)
    board = Board()
    if tracer is None:
        return board
    for call in result.calls:
        name = call.node.name
        if name not in PART_MODULES + HOLE_MODULES + MOUNT_MODULES + BOARD_MODULES:
            continue
        values = {k: tracer.value_of(b) for k, b in (call.values or {}).items()}
        m = call.matrix if call.matrix is not None else np.eye(4)
        if name in BOARD_MODULES:
            board.thickness = _num(values.get('thickness'), 1.6)
            outline = values.get('outline')
            if isinstance(outline, list) and len(outline) >= 3:
                board.polygons.append([_apply(m, *_xy(p)) for p in outline])
                continue
            w, h = _xy(values.get('size', [100, 80]))
            r = max(0., min(_num(values.get('radius'), 0.), w / 2, h / 2))
            if r <= 0:
                corners = [(0, 0), (w, 0), (w, h), (0, h)]
                pts = [_apply(m, *c) for c in corners]
                board.lines += [(pts[i], pts[(i + 1) % 4]) for i in range(4)]
                continue
            s = math.sqrt(.5)
            board.lines += [(_apply(m, r, 0), _apply(m, w - r, 0)), (_apply(m, w, r), _apply(m, w, h - r)),
                            (_apply(m, w - r, h), _apply(m, r, h)), (_apply(m, 0, h - r), _apply(m, 0, r))]
            for cx, cy, start, end in ((w - r, r, (w - r, 0), (w, r)), (w - r, h - r, (w, h - r), (w - r, h)),
                                       (r, h - r, (r, h), (0, h - r)), (r, r, (0, r), (r, 0))):
                mid_dir = ((start[0] + end[0]) / 2 - cx, (start[1] + end[1]) / 2 - cy)
                length = math.hypot(*mid_dir) or 1.
                mid = (cx + mid_dir[0] / length * r, cy + mid_dir[1] / length * r)
                board.arcs.append((_apply(m, *start), _apply(m, *mid), _apply(m, *end)))
            del s
        elif name in HOLE_MODULES:
            if _inside(call, MOUNT_MODULES):
                continue
            x, y = _apply(m, *_xy(values.get('pos')))
            board.circles.append(((x, y), _num(values.get('d'), 3.2) / 2))
        elif name in MOUNT_MODULES:
            x, y = _apply(m, *_xy(values.get('pos')))
            board.footprints.append(Footprint(str(values.get('ref') or 'H?'),
                                              str(values.get('footprint') or 'MountingHole:MountingHole_3.2mm_M3'),
                                              x, y, _rotation(m), 'top', ''))
        else:
            if name == 'pcb_part' and _inside(call, ('pcb_connector',)):
                continue
            x, y = _apply(m, *_xy(values.get('pos')))
            side = 'bottom' if str(values.get('side', 'top')).lower().startswith('b') else 'top'
            if np.linalg.det(m[:3, :3]) < 0:
                side = 'top' if side == 'bottom' else 'bottom'
            rotation = (_rotation(m) + _num(values.get('rot'))) % 360
            board.footprints.append(Footprint(str(values.get('ref') or 'U?'), str(values.get('footprint') or ''),
                                              x, y, rotation, side, str(values.get('value') or '')))
    if not board.footprints and not board.has_outline:
        board.notes.append('No pcb_* modules found.')
    return board


def outline_from_svg(svg):
    """Closed polygons from OpenSCAD's SVG export (which writes x, -y)."""
    polygons = []
    for d in re.findall(r'\bd="([^"]*)"', svg):
        current = []
        for cmd, x, y in re.findall(r'([MLz])\s*(?:([-+\d.eE]+),([-+\d.eE]+))?', d):
            if cmd == 'M':
                if len(current) >= 3:
                    polygons.append(current)
                current = [(float(x), -float(y))]
            elif cmd == 'L':
                current.append((float(x), -float(y)))
            elif cmd == 'z':
                if len(current) >= 3:
                    polygons.append(current)
                current = []
        if len(current) >= 3:
            polygons.append(current)
    return polygons


# ------------------------------------------------------------------ writing
def _fmt(v):
    text = f'{v:.4f}'.rstrip('0').rstrip('.')
    return '0' if text in ('-0', '') else text


def _q(text):
    return '"' + str(text).replace('\\', '\\\\').replace('"', '\\"') + '"'


def write_kicad_pcb(board, title='SCAD Forge export', origin=(100., 100.)):
    """Return .kicad_pcb text (KiCad 7 format; opens in KiCad 7, 8 and 9)."""
    pts = board.points() or [(0., 0.)]
    min_x = min(p[0] for p in pts)
    max_y = max(p[1] for p in pts)
    ox, oy = origin[0] - min_x, origin[1] + max_y

    def k(p):
        return f'{_fmt(p[0] + ox)} {_fmt(oy - p[1])}'
    stroke = '(stroke (width 0.1) (type solid))'
    out = io.StringIO()
    w = out.write
    w('(kicad_pcb (version 20221018) (generator scadforge)\n')
    w('  (general (thickness %s))\n' % _fmt(board.thickness))
    w('  (paper "A4")\n')
    w(f'  (title_block (title {_q(title)}) (comment 1 "Generated by SCAD Forge from OpenSCAD source"))\n')
    w('  (layers\n')
    for number, name, kind in ((0, 'F.Cu', 'signal'), (31, 'B.Cu', 'signal'), (32, 'B.Adhes', 'user'),
                               (33, 'F.Adhes', 'user'), (34, 'B.Paste', 'user'), (35, 'F.Paste', 'user'),
                               (36, 'B.SilkS', 'user'), (37, 'F.SilkS', 'user'), (38, 'B.Mask', 'user'),
                               (39, 'F.Mask', 'user'), (40, 'Dwgs.User', 'user'), (41, 'Cmts.User', 'user'),
                               (44, 'Edge.Cuts', 'user'), (45, 'Margin', 'user'), (46, 'B.CrtYd', 'user'),
                               (47, 'F.CrtYd', 'user'), (48, 'B.Fab', 'user'), (49, 'F.Fab', 'user')):
        w(f'    ({number} "{name}" {kind})\n')
    w('  )\n')
    w('  (setup (pad_to_mask_clearance 0))\n')
    w('  (net 0 "")\n')
    for f in board.footprints:
        layer = 'B.Cu' if f.side == 'bottom' else 'F.Cu'
        silk = 'B.SilkS' if f.side == 'bottom' else 'F.SilkS'
        fab = 'B.Fab' if f.side == 'bottom' else 'F.Fab'
        mirror = ' (justify mirror)' if f.side == 'bottom' else ''
        angle = _fmt(f.rotation % 360)
        w(f'  (footprint {_q(f.footprint or "SCADForge:Unassigned")} (layer "{layer}")\n')
        w(f'    (tstamp {uuid.uuid4()})\n')
        w(f'    (at {k((f.x, f.y))}{" " + angle if angle != "0" else ""})\n')
        w(f'    (property "Reference" {_q(f.ref)})\n')
        w(f'    (property "Value" {_q(f.value or f.footprint.split(":")[-1] or f.ref)})\n')
        w(f'    (fp_text reference {_q(f.ref)} (at 0 -2.5 {angle}) (layer "{silk}")\n'
          f'      (effects (font (size 1 1) (thickness 0.15)){mirror}) (tstamp {uuid.uuid4()}))\n')
        w(f'    (fp_text value {_q(f.value or f.footprint.split(":")[-1] or f.ref)} (at 0 2.5 {angle}) (layer "{fab}")\n'
          f'      (effects (font (size 1 1) (thickness 0.15)){mirror}) (tstamp {uuid.uuid4()}))\n')
        w('  )\n')
    for a, b in board.lines:
        w(f'  (gr_line (start {k(a)}) (end {k(b)}) {stroke} (layer "Edge.Cuts") (tstamp {uuid.uuid4()}))\n')
    for start, mid, end in board.arcs:
        w(f'  (gr_arc (start {k(start)}) (mid {k(mid)}) (end {k(end)}) {stroke} (layer "Edge.Cuts") (tstamp {uuid.uuid4()}))\n')
    for center, radius in board.circles:
        w(f'  (gr_circle (center {k(center)}) (end {k((center[0] + radius, center[1]))}) {stroke} (fill none) '
          f'(layer "Edge.Cuts") (tstamp {uuid.uuid4()}))\n')
    for poly in board.polygons:
        w('  (gr_poly (pts')
        for p in poly:
            w(f' (xy {k(p)})')
        w(f') {stroke} (fill none) (layer "Edge.Cuts") (tstamp {uuid.uuid4()}))\n')
    w(')\n')
    return out.getvalue()


def write_placement_csv(board):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['Ref', 'Val', 'Package', 'PosX', 'PosY', 'Rot', 'Side'])
    for f in board.footprints:
        writer.writerow([f.ref, f.value, f.footprint, f'{f.x:.4f}', f'{f.y:.4f}', f'{f.rotation % 360:.2f}', f.side])
    return out.getvalue()


def balanced(text):
    """Cheap structural check of an S-expression (used by tests)."""
    depth = 0
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string
