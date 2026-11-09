import numpy as np

from scab.boards import copy_poses, add_copies, rename_link, read_pose, remove_link
from scab.kicad_import import find_kicad_links
from scab.kicad_silk import parse_svg, _path
from scab.meshgen import MeshBuilder, ear_clip, pad_outline


def test_prism_and_ring_counts():
    m = MeshBuilder()
    m.prism(pad_outline('rect', 2, 1), 0, 0.1)
    m.ring(pad_outline('circle', 2, 2), pad_outline('circle', 1, 1), 0, 0.1)
    vertices, faces = m.mesh()
    assert faces.max() < len(vertices)
    assert np.isclose(vertices[:, 2].max(), 0.1)


def test_ear_clip_concave():
    l_shape = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]
    assert len(ear_clip(l_shape)) == 4


def test_svg_path_and_scale():
    lines, closed = _path('M0 0 L10 0 l0 10 Z M 5 5 H 8')
    assert closed == [True, False]
    assert lines[1] == [(5., 5.), (8., 5.)]
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" viewBox="0 0 2000 1000">'
           '<g style="fill:none;stroke:#000;stroke-width:15"><path d="M100 100 L300 100"/></g>'
           '<text>ignored</text></svg>')
    layer = parse_svg(svg)
    (points, width), = layer.strokes
    assert np.allclose(points, [(1, 1), (3, 1)])
    assert np.isclose(width, 0.15)


def test_board_copies_and_rename():
    source = '// scab: kicad display = d/display.kicad_pcb\ndisplay_pose = [[1, 0, 0], [0, 0, 0]];\ninclude <kicad/display.scad>\n'
    poses = copy_poses(([1, 0, 0], [0, 0, 0]), 2, [10, 0, 0], 90)
    assert poses[1] == ([21, 0, 0], [0, 0, -180])
    source, new = add_copies(source, 'display', 'd/display.kicad_pcb', poses, {'display'})
    assert new == ['display2', 'display3']
    assert [i for i, _ in find_kicad_links(source, None)] == ['display', 'display2', 'display3']
    assert read_pose(source, 'display3')[0] == [21., 0., 0.]
    renamed = rename_link(source, 'display2', 'screen', 'd/display.kicad_pcb')
    assert 'screen_pose' in renamed and 'include <kicad/screen.scad>' in renamed
    assert [i for i, _ in find_kicad_links(renamed, None)] == ['display', 'screen', 'display3']
    assert 'display.kicad_pcb' in renamed


def test_remove_link_drops_include_and_rename_comment():
    source = ('// scab: kicad d2 = d/display.kicad_pcb\n'
              'd2_pose = [[0, 0, 0], [0, 0, 0]];   // where the d2 board sits\ninclude <kicad/d2.scad>\nd2_pcb();\n')
    assert 'where the x board' in rename_link(source, 'd2', 'x', 'd/display.kicad_pcb')
    removed = remove_link(source, 'd2', 'd2.scad')
    assert 'include' not in removed and 'scab:' not in removed and 'd2_pose' not in removed
