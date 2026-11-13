import json
import io
import shutil
from pathlib import Path

import pytest

from scab import headless, mcp_server
from scab.compiler import find_openscad

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / 'examples' / 'kicad_chassis'


@pytest.fixture
def project(tmp_path):
    shutil.copytree(EXAMPLE / 'board', tmp_path / 'board')
    shutil.copytree(EXAMPLE / 'display', tmp_path / 'display')
    return tmp_path


def test_board_summary():
    s = headless.board_summary(EXAMPLE / 'board' / 'board.kicad_pcb')
    assert s['size'] == [70.0, 45.0] and s['thickness'] == 1.6
    assert s['copper_layers'] == ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']
    c1 = next(p for p in s['parts'] if p['ref'] == 'C1')
    assert c1['side'] == 'top' and c1['height'] > 7


def test_link_pose_info_unlink(project):
    design = project / 'case.scad'
    design.write_text('cube(1);\n')
    first = headless.link(design, project / 'board' / 'board.kicad_pcb')
    second = headless.link(design, project / 'display' / 'display.kicad_pcb', 'display', ([5, 5, 12], [0, 0, 90]))
    assert first['id'] == 'kicad' and second['pose'] == ([5.0, 5.0, 12.0], [0.0, 0.0, 90.0])
    assert (project / 'kicad' / 'board.scad').is_file() and (project / 'kicad' / 'display.scad').is_file()
    headless.set_pose(design, 'display', [1, 2, 3], [0, 0, 0])
    info = headless.info(design)
    assert [b['id'] for b in info['boards']] == ['kicad', 'display']
    display = info['boards'][1]
    assert display['pose'] == ([1.0, 2.0, 3.0], [0.0, 0.0, 0.0]) and display['included']
    assert len(info['boards'][0]['mounting_holes']) == 4 and info['objects'][0]['kind'] == 'cube'
    with pytest.raises(ValueError):
        headless.link(design, project / 'display' / 'display.kicad_pcb', 'display')
    headless.unlink(design, 'display')
    text = design.read_text()
    assert 'display' not in text and 'include <kicad/board.scad>' in text


def test_chassis_source_drops_board_drawing():
    source = 'cube(1);\nkicad_place() kicad_pcb();\ndifference() { cube(5); kicad_pcb(); }\n'
    out = headless._chassis_source(source)
    assert 'kicad_pcb' in out.split('\n')[2] and out.split('\n')[1].strip() == '' and len(out) == len(source)


def test_mcp_protocol(project):
    design = project / 'case.scad'
    design.write_text('')
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'scab_link_board', 'arguments': {
            'scad': str(design), 'pcb': str(project / 'board' / 'board.kicad_pcb')}}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'scab_info', 'arguments': {'scad': str(design)}}},
        {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'missing', 'arguments': {}}},
        {'jsonrpc': '2.0', 'id': 6, 'method': 'unknown/method'},
    ]
    out = io.StringIO()
    mcp_server.serve(io.StringIO('\n'.join(json.dumps(r) for r in requests) + '\n'), out)
    replies = {r['id']: r for r in map(json.loads, out.getvalue().splitlines())}
    assert set(replies) == {1, 2, 3, 4, 5, 6}
    assert replies[1]['result']['serverInfo']['name'] == 'scab'
    assert 'scab_check' in [t['name'] for t in replies[2]['result']['tools']]
    assert json.loads(replies[4]['result']['content'][0]['text'])['boards'][0]['id'] == 'kicad'
    assert replies[5]['result']['isError'] and replies[6]['error']['code'] == -32601


def test_cli_init(tmp_path):
    from scab.cli import main
    assert main(['init', str(tmp_path / 'p')]) == 0
    config = json.loads((tmp_path / 'p' / '.mcp.json').read_text())
    assert config['mcpServers']['scab']['args'][-1] == 'mcp'
    assert (tmp_path / 'p' / 'AGENTS.md').read_text().startswith('# SCAB for AI agents')


@pytest.mark.skipif(find_openscad() is None, reason='OpenSCAD not installed')
def test_check_example_enclosure():
    report = headless.check_design(ROOT / 'examples' / 'agent_enclosure' / 'enclosure.scad')
    assert report['ok'], report
    assert report['clearance']['counts']['collision'] == 0
