"""Generate examples/kicad_chassis/board: a small KiCad 7 board with its own 3D models.

Run from the repository root:  python tools/make_example_board.py
"""
import json
import math
from pathlib import Path
import sys
import uuid
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scab import vrml  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / 'examples' / 'kicad_chassis' / 'board'
X0, Y0 = 100., 80.          # board top-left on the KiCad sheet
W, H, R = 70., 45., 3.
uid = lambda: str(uuid.uuid4())


# ------------------------------------------------------------------ meshes
def box(lo, hi, color):
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0], [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]])
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return v, f, color


def cylinder(cx, cy, z0, z1, r, color, n=32):
    a = np.linspace(0, 2 * math.pi, n, endpoint=False)
    ring = np.c_[cx + r * np.cos(a), cy + r * np.sin(a)]
    v = np.vstack([np.c_[ring, np.full(n, z0)], np.c_[ring, np.full(n, z1)], [[cx, cy, z0], [cx, cy, z1]]])
    f = []
    for i in range(n):
        j = (i + 1) % n
        f += [[i, j, n + j], [i, n + j, n + i], [2 * n, j, i], [2 * n + 1, n + i, n + j]]
    return v, np.array(f), color


def merge(shapes):
    by_color = {}
    for v, f, c in shapes:
        by_color.setdefault(c, []).append((v, f))
    out = []
    for c, items in by_color.items():
        vs, fs, off = [], [], 0
        for v, f in items:
            vs.append(v)
            fs.append(f + off)
            off += len(v)
        out.append((np.vstack(vs), np.vstack(fs), c))
    return out


BLACK, GREY, SILVER, GOLD = (.08, .08, .09, 1), (.35, .35, .37, 1), (.78, .79, .8, 1), (.85, .68, .3, 1)
TAN, BLUE, RED_CLEAR, WHITE = (.75, .62, .45, 1), (.12, .2, .45, 1), (.95, .15, .1, .8), (.92, .92, .9, 1)

MODELS = {
    'usb_c': [box((-4.47, -3.65, 0), (4.47, 3.65, 3.26), SILVER), box((-3.3, -3.7, .8), (3.3, -3.0, 2.4), BLACK)],
    'cap_elec': [box((-3.3, -3.3, 0), (3.3, 3.3, 1.0), BLACK), cylinder(0, 0, 1.0, 7.4, 3.15, BLUE),
                 cylinder(0, 0, 7.4, 7.7, 3.0, SILVER)],
    'c0603': [box((-.5, -.4, 0), (.5, .4, .8), TAN), box((-.8, -.4, 0), (-.5, .4, .8), SILVER), box((.5, -.4, 0), (.8, .4, .8), SILVER)],
    'led0805': [box((-1, -.62, 0), (1, .62, .45), WHITE), box((-.8, -.5, .45), (.8, .5, .8), RED_CLEAR)],
    'qfp32': [box((-3.5, -3.5, 0), (3.5, 3.5, 1.4), BLACK)] +
             [box((-4.5, -2.8 + i * .8 - .18, 0), (-3.5, -2.8 + i * .8 + .18, .25), SILVER) for i in range(8)] +
             [box((3.5, -2.8 + i * .8 - .18, 0), (4.5, -2.8 + i * .8 + .18, .25), SILVER) for i in range(8)] +
             [box((-2.8 + i * .8 - .18, -4.5, 0), (-2.8 + i * .8 + .18, -3.5, .25), SILVER) for i in range(8)] +
             [box((-2.8 + i * .8 - .18, 3.5, 0), (-2.8 + i * .8 + .18, 4.5, .25), SILVER) for i in range(8)],
    # KiCad models are Y-up while footprint coordinates are Y-down: pin n is at model y = -n * 2.54.
    'header4': [box((-1.27, -8.89, 0), (1.27, 1.27, 2.5), BLACK)] +
               [box((-.32, -i * 2.54 - .32, -3.0), (.32, -i * 2.54 + .32, 8.5), GOLD) for i in range(4)],
    'switch6': [box((-3, -3, 0), (3, 3, 3.5), GREY), cylinder(0, 0, 3.5, 5.0, 1.75, BLACK)],
}


# --------------------------------------------------------------- footprints
def fmt(v):
    text = f'{v:.4f}'.rstrip('0').rstrip('.')
    return '0' if text in ('-0', '') else text


def pad(number, kind, shape, x, y, w, h, layers, drill=None, angle=0):
    drill_text = f' (drill {fmt(drill)})' if drill else ''
    at = f'{fmt(x)} {fmt(y)}' + (f' {fmt(angle)}' if angle else '')
    layer_text = ' '.join(f'"{l}"' for l in layers)
    return f'    (pad "{number}" {kind} {shape} (at {at}) (size {fmt(w)} {fmt(h)}){drill_text} (layers {layer_text}) (tstamp {uid()}))\n'


def courtyard(x0, y0, x1, y1, layer):
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return ''.join(f'    (fp_line (start {fmt(a[0])} {fmt(a[1])}) (end {fmt(b[0])} {fmt(b[1])}) (stroke (width 0.05) (type solid)) (layer "{layer}") (tstamp {uid()}))\n'
                   for a, b in zip(pts, pts[1:]))


def footprint(lib, ref, value, x, y, angle=0, side='top', pads='', crtyd=(-1, -1, 1, 1), model=None, model_rotate=(0, 0, 0)):
    f = 'F' if side == 'top' else 'B'
    mirror = ' (justify mirror)' if side == 'bottom' else ''
    at = f'{fmt(X0 + x)} {fmt(Y0 + y)}' + (f' {fmt(angle)}' if angle else '')
    text = f'  (footprint "{lib}" (layer "{f}.Cu")\n    (tstamp {uid()})\n    (at {at})\n'
    text += f'    (property "Reference" "{ref}")\n    (property "Value" "{value}")\n'
    text += f'    (fp_text reference "{ref}" (at 0 {fmt(crtyd[1] - 1)} {fmt(angle)}) (layer "{f}.SilkS") (effects (font (size 1 1) (thickness 0.15)){mirror}) (tstamp {uid()}))\n'
    text += f'    (fp_text value "{value}" (at 0 {fmt(crtyd[3] + 1)} {fmt(angle)}) (layer "{f}.Fab") (effects (font (size 1 1) (thickness 0.15)){mirror}) (tstamp {uid()}))\n'
    text += courtyard(*crtyd, f'{f}.CrtYd') + pads
    if model:
        rx, ry, rz = model_rotate
        text += (f'    (model "${{KIPRJMOD}}/3d/{model}.wrl"\n      (offset (xyz 0 0 0))\n      (scale (xyz 1 1 1))\n'
                 f'      (rotate (xyz {fmt(rx)} {fmt(ry)} {fmt(rz)}))\n    )\n')
    return text + '  )\n'


def smd2(pitch, w, h, side='top'):
    layers = ['F.Cu', 'F.Paste', 'F.Mask'] if side == 'top' else ['B.Cu', 'B.Paste', 'B.Mask']
    return pad('1', 'smd', 'roundrect', -pitch / 2, 0, w, h, layers) + pad('2', 'smd', 'roundrect', pitch / 2, 0, w, h, layers)


def build():
    tht = ['*.Cu', '*.Mask']
    parts = []
    for i, (x, y) in enumerate([(4, 4), (W - 4, 4), (4, H - 4), (W - 4, H - 4)]):
        parts.append(footprint('MountingHole:MountingHole_3.2mm_M3', f'H{i + 1}', 'MountingHole', x, y,
                               pads=pad('', 'np_thru_hole', 'circle', 0, 0, 3.2, 3.2, ['*.Cu', '*.Mask'], drill=3.2),
                               crtyd=(-3.45, -3.45, 3.45, 3.45)))
    usb_pads = ''.join(pad(str(i + 1), 'smd', 'rect', -2.75 + i * .5, -4.2, .3, 1.15, ['F.Cu', 'F.Paste', 'F.Mask']) for i in range(12))
    usb_pads += ''.join(pad('S1', 'thru_hole', 'oval', sx, sy, 1.0, 1.8, tht, drill=0.6) for sx in (-4.32, 4.32) for sy in (-3.6, .3))
    # The connector sits on the right edge, opening facing +X (outwards).
    parts.append(footprint('Connector_USB:USB_C_Receptacle_GCT_USB4085', 'J1', 'USB_C', W - 2.6, 22.5, 90,
                           pads=usb_pads, crtyd=(-5.2, -5.3, 5.2, 4.3), model='usb_c'))
    parts.append(footprint('Capacitor_SMD:CP_Elec_6.3x7.7', 'C1', '220u', 47, 30, 0,
                           pads=smd2(5.4, 3.3, 1.7), crtyd=(-4.8, -3.6, 4.8, 3.6), model='cap_elec'))
    parts.append(footprint('Capacitor_SMD:C_0603_1608Metric', 'C2', '100n', 30, 14, 0,
                           pads=smd2(1.55, .9, .95), crtyd=(-1.5, -.75, 1.5, .75), model='c0603'))
    parts.append(footprint('Capacitor_SMD:C_0603_1608Metric', 'C3', '100n', 22, 30, 90, side='bottom',
                           pads=smd2(1.55, .9, .95, 'bottom'), crtyd=(-1.5, -.75, 1.5, .75), model='c0603'))
    qfp = ''
    n = 1
    for side_index in range(4):
        for i in range(8):
            o = -2.8 + i * .8
            x, y, w, h = [(-4.2, o, 1.5, .5), (o, 4.2, .5, 1.5), (4.2, -o, 1.5, .5), (-o, -4.2, .5, 1.5)][side_index]
            qfp += pad(str(n), 'smd', 'rect', x, y, w, h, ['F.Cu', 'F.Paste', 'F.Mask'])
            n += 1
    parts.append(footprint('Package_QFP:LQFP-32_7x7mm_P0.8mm', 'U1', 'MCU', 22, 18, 45,
                           pads=qfp, crtyd=(-5.2, -5.2, 5.2, 5.2), model='qfp32'))
    for i, x in enumerate((10, 15)):
        parts.append(footprint('LED_SMD:LED_0805_2012Metric', f'D{i + 1}', 'LED', x, 38, 0,
                               pads=smd2(2.1, 1.1, 1.4), crtyd=(-1.7, -1, 1.7, 1), model='led0805'))
    header = ''.join(pad(str(i + 1), 'thru_hole', 'rect' if i == 0 else 'oval', 0, i * 2.54, 1.7, 1.7, tht, drill=1.0) for i in range(4))
    parts.append(footprint('Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical', 'J2', 'Debug', 8, 12, 0,
                           pads=header, crtyd=(-1.8, -1.8, 1.8, 9.4), model='header4'))
    switch = ''.join(pad(str(i + 1), 'thru_hole', 'circle', sx, sy, 2, 2, tht, drill=1.1)
                     for i, (sx, sy) in enumerate([(-3.25, -2.25), (3.25, -2.25), (-3.25, 2.25), (3.25, 2.25)]))
    parts.append(footprint('Button_Switch_THT:SW_PUSH_6mm', 'SW1', 'Reset', 58, 36, 0,
                           pads=switch, crtyd=(-4.3, -3.5, 4.3, 3.5), model='switch6'))

    def p(x, y):
        return f'{fmt(X0 + x)} {fmt(Y0 + y)}'
    edge = []
    s = '(stroke (width 0.1) (type solid)) (layer "Edge.Cuts")'
    edge.append(f'  (gr_line (start {p(R, 0)}) (end {p(W - R, 0)}) {s} (tstamp {uid()}))')
    edge.append(f'  (gr_line (start {p(W, R)}) (end {p(W, H - R)}) {s} (tstamp {uid()}))')
    edge.append(f'  (gr_line (start {p(W - R, H)}) (end {p(R, H)}) {s} (tstamp {uid()}))')
    edge.append(f'  (gr_line (start {p(0, H - R)}) (end {p(0, R)}) {s} (tstamp {uid()}))')
    k = R * (1 - math.sqrt(.5))
    for (sx, sy), (mx, my), (ex, ey) in [((W - R, 0), (W - k, k), (W, R)), ((W, H - R), (W - k, H - k), (W - R, H)),
                                         ((R, H), (k, H - k), (0, H - R)), ((0, R), (k, k), (R, 0))]:
        edge.append(f'  (gr_arc (start {p(sx, sy)}) (mid {p(mx, my)}) (end {p(ex, ey)}) {s} (tstamp {uid()}))')
    layers = ''.join(f'    ({n} "{name}" {kind})\n' for n, name, kind in [
        (0, 'F.Cu', 'signal'), (31, 'B.Cu', 'signal'), (34, 'B.Paste', 'user'), (35, 'F.Paste', 'user'),
        (36, 'B.SilkS', 'user'), (37, 'F.SilkS', 'user'), (38, 'B.Mask', 'user'), (39, 'F.Mask', 'user'),
        (44, 'Edge.Cuts', 'user'), (46, 'B.CrtYd', 'user'), (47, 'F.CrtYd', 'user'), (48, 'B.Fab', 'user'), (49, 'F.Fab', 'user')])
    text = ('(kicad_pcb (version 20221018) (generator pcbnew)\n  (general (thickness 1.6))\n  (paper "A4")\n'
            '  (title_block (title "SCAB example board") (comment 1 "Generated by tools/make_example_board.py"))\n'
            f'  (layers\n{layers}  )\n'
            f'  (setup (pad_to_mask_clearance 0) (aux_axis_origin {p(0, H)}) (grid_origin {p(0, H)}))\n'
            '  (net 0 "")\n' + ''.join(parts) + '\n'.join(edge) + '\n)\n')
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / '3d').mkdir(exist_ok=True)
    (ROOT / 'board.kicad_pcb').write_text(text, encoding='utf-8')
    (ROOT / 'board.kicad_pro').write_text(json.dumps({'meta': {'filename': 'board.kicad_pro', 'version': 1},
                                                      'board': {'design_settings': {}}}, indent=2), encoding='utf-8')
    for name, shapes in MODELS.items():
        vrml.write(ROOT / '3d' / f'{name}.wrl', merge(shapes))
    print('wrote', ROOT)


if __name__ == '__main__':
    build()
