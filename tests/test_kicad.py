from pathlib import Path
import pytest
from scadforge.trace import trace
from scadforge.kicad import from_trace, write_kicad_pcb, write_placement_csv, outline_from_svg, balanced

LIB = Path(__file__).resolve().parent.parent / 'assets' / 'scadforge' / 'pcb.scad'

DESIGN = f'''include <{LIB}>
module sensor_row(n = 3) {{
    for (i = [0:n - 1]) pcb_part(ref = str("U", i + 1), footprint = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
                                 pos = [10 + i * 12, 20], rot = 90);
}}
translate([5, 5, 0]) {{
    pcb_board(size = [60, 40], radius = 3) {{
        pcb_mounting_hole("H1", [4, 4]);
        pcb_hole([30, 30], d = 2);
    }}
    sensor_row();
    pcb_connector(ref = "J1", footprint = "Connector_USB:USB_C_Receptacle", pos = [55, 20], rot = 270);
    pcb_part(ref = "C1", footprint = "Capacitor_SMD:C_0603_1608Metric", pos = [30, 10], side = "bottom", value = "100n");
}}
'''


def test_board_from_trace():
    result = trace(Path('/tmp/board.scad'), DESIGN)
    board = from_trace(result)
    refs = {f.ref: f for f in board.footprints}
    assert set(refs) == {'H1', 'U1', 'U2', 'U3', 'J1', 'C1'}
    assert refs['U2'].x == pytest.approx(5 + 22) and refs['U2'].y == pytest.approx(25)
    assert refs['U1'].rotation == pytest.approx(90)
    assert refs['C1'].side == 'bottom' and refs['C1'].value == '100n'
    assert refs['H1'].x == pytest.approx(9)
    assert len(board.lines) == 4 and len(board.arcs) == 4
    assert board.circles == [((35.0, 35.0), 1.0)]
    text = write_kicad_pcb(board)
    assert balanced(text)
    assert text.count('(footprint ') == 6 and text.count('Edge.Cuts') >= 9 + 1
    assert '"Connector_USB:USB_C_Receptacle"' in text
    csv_text = write_placement_csv(board)
    assert 'C1,100n,Capacitor_SMD:C_0603_1608Metric' in csv_text


def test_svg_outline():
    svg = '<path d="\nM 0,-0 L 30,-0 L 30,-20 L 0,-20 z\nM 3.5,-5 L 4,-6 L 5,-6.5 z\n" stroke="black"/>'
    polys = outline_from_svg(svg)
    assert len(polys) == 2 and polys[0][2] == (30.0, 20.0)
