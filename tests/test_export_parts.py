import shutil
from pathlib import Path
import pytest
from scab.model import Document
from scab.export_parts import chassis_parts, export

CHASSIS = Path(__file__).resolve().parent.parent / 'examples' / 'kicad_chassis' / 'chassis.scad'


def test_names_and_skips():
    doc = Document('''// Base: floor and walls
color("grey") cube(10);

// Lid
translate([0, 0, 20]) cube(10);
kicad_pcb();
%cube(3);
standoff();
standoff();
''')
    names = [name for _, name in chassis_parts(doc)]
    assert names == ['base', 'lid', 'standoff', 'standoff-2']


def test_example_chassis_names():
    doc = Document(CHASSIS.read_text())
    assert [name for _, name in chassis_parts(doc)] == ['base', 'lid']


@pytest.mark.skipif(shutil.which('openscad') is None, reason='OpenSCAD not installed')
def test_export_writes_one_stl_per_part(tmp_path):
    doc = Document(CHASSIS.read_text())
    results = export(doc, str(CHASSIS.parent), shutil.which('openscad'), tmp_path, 'chassis-')
    assert [(name, path.name) for name, path, _ in results] == [('base', 'chassis-base.stl'), ('lid', 'chassis-lid.stl')]
    assert all(path.stat().st_size > 1000 for _, path, _ in results)
