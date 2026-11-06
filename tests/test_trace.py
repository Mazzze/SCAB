import math
import numpy as np
import pytest
from scadforge import lang
from scadforge.trace import trace, fmt
from scadforge.inspector import Explainer, apply_edit, color_literal

CONFIG = '''
board_w = 60;
board_h = 40;
board_t = 1.6;
hole_d = 3.2;
led_color = "red";
'''

MAIN = '''include <config.scad>
use <parts.scad>

module board() {
    color("green")
    difference() {
        cube([board_w, board_h, board_t]);
        for (p = [[4, 4], [board_w - 4, 4]])
            translate([p[0], p[1], -1]) cylinder(d = hole_d, h = board_t + 2, $fn = 24);
    }
}

module on_board(x, y) {
    translate([x, y, board_t]) children();
}

board();
on_board(20, 20) led(c = led_color);
on_board(40, 20) led();
'''

PARTS = '''
module led(c = "yellow", d = 5, h = 8) {
    color(c) cylinder(d = d, h = h);
}
'''


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'config.scad').write_text(CONFIG)
    (tmp_path / 'parts.scad').write_text(PARTS)
    main = tmp_path / 'main.scad'
    main.write_text(MAIN)
    return main


def test_parser_roundtrip_spans():
    text = 'x = [for (i = [0:2]) if (i % 2 == 0) i * 2];\nf = function(a) a + 1;\nmodule m(a = 1) { cube(a); }\n!m(a = -2);'
    ast = lang.parse(text)
    assert [type(s).__name__ for s in ast] == ['Assign', 'Assign', 'ModuleDef', 'Instance']
    assign = ast[0]
    assert text[assign.value.start:assign.value.end].startswith('[for')
    call = ast[3]
    assert call.modifier == '!' and text[call.args[0].value.start:call.args[0].value.end] == '-2'


def test_parse_error_position():
    with pytest.raises(lang.ParseError) as info:
        lang.parse('x = 3\ncube(x);')
    assert info.value.position == 5


def test_expression_evaluation():
    text = '''
    function sq(x) = x * x;
    v = [for (i = [1:3]) sq(i)];
    s = str("a", 1, [1, 2]);
    l = let(a = 2, b = a + 1) a * b;
    r = [ for (i = 0; i < 3; i = i + 1) i ];
    cube(v[2] + l + len(r));
    '''
    result = trace(None, text)
    leaf = result.leaves[0]
    assert leaf.box[1] == [9 + 6 + 3] * 3
    scope = leaf.scope
    tracer = result.tracer
    assert tracer.lookup('s', scope) == 'a1[1, 2]'
    assert tracer.lookup('r', scope) == [0, 1, 2]


def test_trace_modules_across_files(project):
    result = trace(project, project.read_text())
    assert not result.partial
    names = [leaf.node.name for leaf in result.leaves]
    assert names.count('cylinder') == 4 and names.count('cube') == 1
    holes = [l for l in result.leaves if l.cutter]
    assert len(holes) == 2
    leds = [l for l in result.leaves if not l.cutter and l.node.name == 'cylinder']
    # world position of the second LED: translate([40, 20, 1.6])
    assert np.allclose(leds[1].matrix[:3, 3], [40, 20, 1.6])
    assert leds[0].color.values['c'] == 'red'
    assert leds[1].color.values['c'] == 'yellow'


def test_pick_and_explain_nested_color(project):
    result = trace(project, project.read_text())
    root = result.roots[1]
    led = result.pick([20 + 2.5, 20, 1.6 + 4], (root.start, root.end))
    assert led is not None and led.node.name == 'cylinder'
    groups = Explainer(result).explain(led)
    titles = [g.title for g in groups]
    assert any(t.startswith('led(') for t in titles)
    led_group = next(g for g in groups if g.title.startswith('led('))
    color_row = next(r for r in led_group.rows if r.name == 'c')
    assert color_row.value == 'red' and color_row.is_color
    target = color_row.targets[0]
    path, text = result.files[target.file]
    assert path.name == 'config.scad' and text[target.start:target.end] == '"red"'
    # per-call override of the default 'd' parameter
    d_row = next(r for r in led_group.rows if r.name == 'd')
    insert = next(t for t in d_row.targets if t.kind == 'insert')
    index, new_text = apply_edit(result.files, insert, '6')
    assert 'led(c = led_color, d = 6)' in new_text
    variables = next(g for g in groups if g.kind == 'variables')
    assert {'led_color', 'board_t'} <= {r.name for r in variables.rows}


def test_pick_hole_wall_in_board(project):
    result = trace(project, project.read_text())
    root = result.roots[0]
    hole = result.pick([4 + 1.6, 4, 0.8], (root.start, root.end))
    assert hole.cutter and hole.node.name == 'cylinder'
    groups = Explainer(result).explain(hole)
    primitive = next(g for g in groups if g.kind == 'primitive')
    d = next(r for r in primitive.rows if r.name == 'd')
    assert d.value == pytest.approx(3.2)
    path, text = result.files[d.targets[0].file]
    assert path.name == 'config.scad' and text[d.targets[0].start:d.targets[0].end] == '3.2'
    loop = next(g for g in groups if g.kind == 'for')
    assert loop.rows[0].value == [4, 4]


def test_default_parameter_resolves_to_definition(project):
    result = trace(project, project.read_text())
    root = result.roots[2]
    led = result.pick([40, 20 + 2.5, 1.6 + 4], (root.start, root.end))
    groups = Explainer(result).explain(led)
    led_group = next(g for g in groups if g.title.startswith('led('))
    c = next(r for r in led_group.rows if r.name == 'c')
    assert c.value == 'yellow'
    target = c.targets[0]
    path, text = result.files[target.file]
    assert path.name == 'parts.scad' and text[target.start:target.end] == '"yellow"'
    assert any(t.kind == 'insert' for t in c.targets)


def test_linear_extrude_circle_is_cylinder():
    result = trace(None, 'translate([5,0,0]) linear_extrude(height=4) translate([1,2]) circle(r=2);')
    leaf = result.leaves[0]
    assert leaf.kind == 'cylinder'
    assert leaf.distance(np.array([5 + 1 + 2, 2, 2])) == pytest.approx(0, abs=1e-6)


def test_color_literal_styles():
    assert color_literal((1, 0, 0, 1), '"red"') == '"#ff0000"'
    assert color_literal((1, 0, 0, 1), '[0, 1, 0]') == '[1, 0, 0]'
    assert fmt(2.0) == '2' and fmt(0.1) == '0.1'


def test_budget_partial():
    result = trace(None, 'for (i = [0:100000]) cube(i);', max_leaves=50)
    assert result.partial and len(result.leaves) == 50


def test_multi_variable_for_and_scale():
    src = '''module via(d=0.3) cylinder(d=d, h=2, $fn=8);
difference() {
  cube([20, 20, 1.6]);
  for (x=[0:2:18], y=[0:2:18]) translate([x, y, -0.2]) via();
}
'''
    result = trace(None, src)
    assert not result.partial and len(result.leaves) == 101
    hole = result.pick([4 + 0.15, 6, 0.8], (result.roots[0].start, result.roots[0].end))
    assert hole.cutter and hole.frame.parent.parent.parent.values == {'x': 4, 'y': 6}
