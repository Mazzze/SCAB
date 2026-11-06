from pathlib import Path
import numpy as np
import pytest
from scab import vrml
from scab.kicad_import import parse_board, part_meshes, write_include, find_kicad_link, load_board, outline_loops
from scab.clearance import check

BOARD = Path(__file__).resolve().parent.parent / 'examples' / 'kicad_chassis' / 'board' / 'board.kicad_pcb'

# Positions/rotations reported by `kicad-cli pcb export pos --use-drill-file-origin` (KiCad 7.0.11)
KICAD_POS = {'C1': (47, 15, 0, 'top'), 'C2': (30, 31, 0, 'top'), 'C3': (22, 15, 90, 'bottom'), 'D1': (10, 7, 0, 'top'),
             'H1': (4, 41, 0, 'top'), 'H4': (66, 4, 0, 'top'), 'J1': (67.4, 22.5, 90, 'top'), 'J2': (8, 33, 0, 'top'),
             'SW1': (58, 9, 0, 'top'), 'U1': (22, 27, 45, 'top')}


def test_vrml_roundtrip(tmp_path):
    v = np.array([[0, 0, 0], [2.54, 0, 0], [0, 2.54, 0]], dtype=float)
    vrml.write(tmp_path / 'm.wrl', [(v, np.array([[0, 1, 2]]), (1, 0, 0, 1))])
    shapes = vrml.load(tmp_path / 'm.wrl')
    assert len(shapes) == 1
    loaded, faces, color = shapes[0]
    # KiCad models are written in 0.1 inch units: the file holds 1.0 for 2.54 mm
    assert np.allclose(loaded, v / 2.54, atol=1e-4) and color == (1., 0., 0., 1.)


def test_board_matches_kicad_placement():
    board = parse_board(BOARD)
    assert board.thickness == pytest.approx(1.6) and board.aux_origin == (100., 125.)
    for fp in board.footprints:
        if fp.ref in KICAD_POS:
            x, y, angle, side = KICAD_POS[fp.ref]
            assert board.to_scad(fp.x, fp.y) == pytest.approx((x, y)), fp.ref
            assert fp.angle == pytest.approx(angle) and fp.side == side
    outer, holes = outline_loops(board)
    xs, ys = [p[0] for p in outer], [p[1] for p in outer]
    assert (min(xs), min(ys), max(xs), max(ys)) == pytest.approx((0, 0, 70, 45))


def test_models_are_placed_like_kicad():
    parts = {p.ref: p for p in part_meshes(parse_board(BOARD))}
    assert all(p.has_model for p in parts.values())
    lo, hi = parts['C1'].box                      # 6.3 x 7.7 mm electrolytic on the top side
    assert lo[2] == pytest.approx(1.6) and hi[2] == pytest.approx(9.3)
    assert ((lo + hi) / 2)[:2] == pytest.approx((47, 15), abs=1e-4)
    lo, hi = parts['C3'].box                      # 0603 on the bottom, rotated 90 degrees
    assert hi[2] == pytest.approx(0) and lo[2] == pytest.approx(-0.8)
    assert (hi - lo)[:2] == pytest.approx((0.8, 1.6), abs=1e-4)
    lo, hi = parts['J2'].box                      # header pins run along the pads (towards -Y)
    assert lo[1] < 25.38 and hi[1] > 33 and lo[2] == pytest.approx(-1.4, abs=1e-4)


def test_include_and_link(tmp_path):
    model = load_board(BOARD, executable=None, cache_dir=tmp_path / 'cache')
    target = write_include(model, tmp_path / 'kicad')
    text = target.read_text()
    assert 'kicad_board_size = [70, 45];' in text and '"C1", "220u"' in text
    assert (tmp_path / 'kicad' / 'board-parts.stl').stat().st_size > 84
    assert 'kicad_mounting_holes = [[[4, 41], 3.2]' in text
    link = find_kicad_link('// scab: kicad = board/board.kicad_pcb\n', BOARD.parent.parent / 'chassis.scad')
    assert link == BOARD.resolve()


def box(lo, hi):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0], [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], float)
    f = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return v, f


def test_clearance_collision_headroom_and_gap():
    part = box((0, 0, 1.6), (5, 5, 9.3))[0]
    lid_high = box((-10, -10, 11.6), (20, 20, 13.6))
    lid_low = box((-10, -10, 8.6), (20, 20, 10.6))
    ok, = check([('C1', part, True, 'top')], [lid_high])
    assert not ok.collides and ok.headroom == pytest.approx(2.3) and ok.gap == pytest.approx(2.3)
    assert ok.status(1.0) == 'ok' and ok.status(3.0) == 'tight'
    bad, = check([('C1', part, True, 'top')], [lid_low])
    assert bad.collides and bad.status(1.0) == 'collision'
    inside, = check([('C1', part, True, 'top')], [box((-1, -1, 0), (6, 6, 12))])
    assert inside.collides


def test_silkscreen_parsed():
    board = parse_board(BOARD)
    texts = {t.text: t for t in board.silk_text}
    assert 'SCAB demo board v1' in texts and texts['bottom side'].side == 'bottom'
    assert texts['C1'].side == 'top' and board.to_scad(texts['C1'].x, texts['C1'].y)[0] == pytest.approx(47, abs=1e-6)
    assert any(side == 'top' for side, _, _ in board.silk_lines)
    from scab.kicad_import import silk_scad
    source = silk_scad(board)
    assert 'text("SCAB demo board v1"' in source and 'mirror([1, 0, 0])' in source


def test_step_model_matches_vrml_twin():
    pytest.importorskip('cascadio')
    from scab import step
    meshes = step.load(BOARD.parent / '3d' / 'usb_c.step')
    v = np.vstack([m[0] for m in meshes])
    assert v.min(axis=0) == pytest.approx((-4.47, -3.65, 0), abs=1e-3)
    assert v.max(axis=0) == pytest.approx((4.47, 3.65, 3.26), abs=1e-3)
    parts = {p.ref: p for p in part_meshes(parse_board(BOARD))}
    lo, hi = parts['J1'].box                      # placed like the .wrl: rotated 90 degrees on the top side
    assert lo[2] == pytest.approx(1.6, abs=1e-4) and (hi - lo)[:2] == pytest.approx((7.3, 8.94), abs=1e-3)
