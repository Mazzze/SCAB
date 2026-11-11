import numpy as np

from scab.model import Document
from scab import grouping

SOURCE = '''$fn = 32;
// Base: floor and walls
color("red") translate([10, 0, 0]) {
    cube(5);
    translate([0, 10, 0]) sphere(3);
}

// Post on the right
translate([40, 0, 0]) cylinder(d = 4, h = 10);

x = 1;
// Lid
translate([0, 30, 0]) cube([20, 10, 2]);
'''


def first(doc, name, k=0):
    return [n for n in doc.walk() if n.name == name][k]


def test_options_for_a_nested_part():
    doc = Document(SOURCE)
    sphere = first(doc, 'sphere')
    labels = [o[0] for o in grouping.move_options(doc, grouping.node_path(doc, sphere.start, sphere.end))]
    assert labels[0].startswith('Part · sphere')
    assert labels[1].startswith('Group · translate([10, 0, 0])') and '2 parts' in labels[1]
    assert labels[-1].startswith('Whole object')


def test_move_part_adjusts_its_literal_translate():
    doc = Document(SOURCE)
    sphere = first(doc, 'sphere')
    out = grouping.nudge_source(doc, grouping.node_path(doc, sphere.start, sphere.end), 'translate', 0, 5, [0, 0, 0])
    assert 'translate([5, 10, 0]) sphere(3);' in out
    cube = first(doc, 'cube')
    out = grouping.nudge_source(doc, grouping.node_path(doc, cube.start, cube.end), 'translate', 2, 1.5, [0, 0, 0])
    assert 'translate([0, 0, 1.5]) cube(5);' in out
    # inside a frame turned 90 degrees about Z, world +X is local -Y
    context = np.eye(4)
    context[:3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    out = grouping.nudge_source(doc, grouping.node_path(doc, cube.start, cube.end), 'translate', 0, 2, [0, 0, 0], context)
    assert 'translate([0, -2, 0]) cube(5);' in out


def test_group_neighbours_in_one_block_and_spread_parts_by_variable():
    doc = Document(SOURCE)
    base, post, lid = doc.roots[0], doc.roots[1], doc.roots[2]
    name = grouping.suggest_group_name(doc, [base, post])
    assert name == 'base_and_post_pos'
    block = grouping.group_source(doc, [base, post], name, [0, 0, 3])
    assert block.count(f'translate({name})') == 1 and f'{name} = [0, 0, 3];' in block
    assert block.index(f'{name} = ') < block.index('// Base')          # above the comment of the first part
    spread = grouping.group_source(doc, [base, lid], grouping.suggest_group_name(doc, [base, lid]))
    assert spread.count('translate(base_and_lid_pos)') == 2           # `x = 1;` between them: one variable, two places
    d2 = Document(spread)
    assert grouping.is_group_variable(d2, 'base_and_lid_pos')
    moved = grouping.move_variable_source(d2, 'base_and_lid_pos', [1, 2, 3])
    assert 'base_and_lid_pos = [1, 2, 3];' in moved
    cube = first(d2, 'cube')
    labels = [o[0] for o in grouping.move_options(d2, grouping.node_path(d2, cube.start, cube.end))]
    assert any(l.startswith('Group base_and_lid_pos') for l in labels)
    assert grouping.ungroup_source(d2, 'base_and_lid_pos').strip() == SOURCE.strip()


def test_group_names():
    doc = Document('cube(1);\ncube(2);\nmodule standoff() cylinder(d=3, h=5);\nstandoff();\n')
    a, b, c = doc.roots
    assert grouping.suggest_group_name(doc, [a, b]) == 'cubes_pos'
    assert grouping.suggest_group_name(doc, [a, c]) == 'cube_and_standoff_pos'
    taken = Document('cubes_pos = 1;\ncube(1);\ncube(2);\n')
    assert grouping.suggest_group_name(taken, taken.roots) == 'cubes2_pos'
