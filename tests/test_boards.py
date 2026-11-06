from pathlib import Path
import numpy as np
import pytest
from scab.boards import pose_matrix, pose_from_matrix, read_pose, write_pose, add_link, remove_link
from scab.kicad_import import find_kicad_links, load_board, write_include, include_names
from scab.stepwrite import write_step

EXAMPLE = Path(__file__).resolve().parent.parent / 'examples' / 'kicad_chassis'


def test_pose_matrix_matches_openscad_order_and_roundtrips():
    m = pose_matrix([10, 0, 5], [0, 0, 90])
    assert m[:3, 3] == pytest.approx([10, 0, 5])
    assert (m @ [1, 0, 0, 1])[:3] == pytest.approx([10, 1, 5])          # rotate first, then translate
    for angles in ([0, 0, 0], [30, 0, 0], [10, 20, 30], [180, 0, 45]):
        pos, rot = pose_from_matrix(pose_matrix([1, 2, 3], angles))
        assert np.allclose(pose_matrix(pos, rot), pose_matrix([1, 2, 3], angles), atol=1e-6)


def test_pose_read_write_in_source():
    source = '// scab: kicad display = d/d.kicad_pcb\ncube(1);\n'
    source = write_pose(source, 'display', [8, 14, 14], [0, 0, 90])
    assert 'display_pose = [[8, 14, 14], [0, 0, 90]];' in source
    assert source.index('display_pose') > source.index('scab: kicad display')
    assert read_pose(source, 'display') == ([8., 14., 14.], [0., 0., 90.])
    moved = write_pose(source, 'display', [1.5, 0, -2], [0, 0, 0])
    assert read_pose(moved, 'display') == ([1.5, 0., -2.], [0., 0., 0.]) and moved.count('display_pose') == 1
    assert read_pose('p_pose = [[1 + 1, 0, 0], [0, 0, -90]];', 'p') == ([2., 0., 0.], [0., 0., -90.])


def test_add_and_remove_links():
    source = add_link('cube(1);\n', 'sensor', 'sensor/sensor.kicad_pcb', 'sensor.scad')
    assert source.startswith('// scab: kicad sensor = sensor/sensor.kicad_pcb\nsensor_pose = [[0, 0, 0], [0, 0, 0]];')
    assert 'include <kicad/sensor.scad>' in source and source.rstrip().endswith('cube(1);')
    source = add_link(source, 'main', 'main/main.kicad_pcb', 'main.scad')
    assert [i for i, _ in find_kicad_links(source, EXAMPLE / 'x.scad')] == ['sensor', 'main']
    removed = remove_link(source, 'sensor')
    assert 'sensor_pose' not in removed and 'scab: kicad sensor' not in removed and 'scab: kicad main' in removed


def test_links_and_prefixed_includes(tmp_path):
    source = (EXAMPLE / 'chassis.scad').read_text()
    links = find_kicad_links(source, EXAMPLE / 'chassis.scad')
    assert [ident for ident, _ in links] == ['kicad', 'display']
    assert include_names('kicad', 'board') == ('board.scad', 'board-parts.stl')
    assert include_names('display', 'display') == ('display.scad', 'display-parts.stl')
    model = load_board(links[1][1], executable=None, cache_dir=tmp_path / 'cache')
    text = write_include(model, tmp_path, 'display').read_text()
    for name in ('display_board_outline', 'display_parts =', 'function display_part(', 'module display_place()',
                 'module display_pcb(', 'translate(display_pose[0]) rotate(display_pose[1])'):
        assert name in text
    code = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('//'))
    assert 'kicad_' not in code
    classic = write_include(load_board(links[0][1], executable=None, cache_dir=tmp_path / 'cache'), tmp_path, 'kicad').read_text()
    assert 'kicad_board_outline' in classic and 'module kicad_pcb(' in classic


def test_step_writer_reads_back(tmp_path):
    cascadio = pytest.importorskip('cascadio')
    from scab import step
    from scab.kicad_import import box_mesh
    v, f, _ = box_mesh((0, 0, 0), (10, 5, 2), (1, 0, 0, 1))
    w, g, _ = box_mesh((20, 0, 0), (25, 5, 9), (0, 0, 1, 1))
    assert write_step(tmp_path / 'a.step', [('box', v, f, (.9, .2, .2, 1)), ('tower', w, g, (.2, .3, .9, 1))]) == 2
    text = (tmp_path / 'a.step').read_text()
    assert text.startswith('ISO-10303-21;') and "FACETED_BREP('tower'" in text and 'COLOUR_RGB' in text
    meshes = step.load(tmp_path / 'a.step', tmp_path / 'cache')
    allv = np.vstack([m[0] for m in meshes])
    assert allv.min(axis=0) == pytest.approx((0, 0, 0), abs=1e-6) and allv.max(axis=0) == pytest.approx((25, 5, 9), abs=1e-6)
    assert sum(len(m[1]) for m in meshes) == 24 and len(meshes) == 2      # two colours kept
