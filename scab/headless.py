"""SCAB without a window: what an AI agent (or a script / CI job) needs to design around KiCad boards.

Everything returns plain dicts / lists (JSON-ready). Used by `python -m scab.cli` and the
MCP server `python -m scab.mcp_server`. Nothing here needs a display.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import numpy as np

from .model import Document, SourceError
from .compiler import find_openscad
from .diagnostics import parse_log, PathMapper
from .kicad_import import (find_kicad_links, include_names, load_board, write_include, parse_board, board_id,
                           outline_loops, drill_holes)
from .boards import (read_pose, write_pose, add_link, remove_link, pose_matrix, link_option, rename_link,
                     IDENTITY_POSE)

CACHE = '.scab-cache'


def openscad(executable=None):
    found = executable or os.environ.get('OPENSCAD_BINARY') or find_openscad()
    if not found:
        raise RuntimeError('OpenSCAD not found: install it or set OPENSCAD_BINARY')
    return found


def _read(path):
    return Path(path).read_text(encoding='utf-8-sig')


def _write(path, text):
    Path(path).write_text(text, encoding='utf-8')


def _round(v, n=4):
    return [round(float(x), n) for x in v]


# ------------------------------------------------------------------- boards
def board_summary(pcb_path, parts=True):
    """A KiCad board as data: size, thickness, outline box, holes, mounting holes, parts with 3D boxes."""
    from .kicad_copper import stackup
    board = parse_board(pcb_path)
    outer, holes = outline_loops(board)
    pts = np.array(outer, dtype=float) if outer else np.zeros((0, 2))
    out = {
        'file': str(Path(pcb_path).resolve()),
        'title': board.title,
        'thickness': board.thickness,
        'outline_min': _round(pts.min(axis=0)) if len(pts) else None,
        'outline_max': _round(pts.max(axis=0)) if len(pts) else None,
        'size': _round(pts.max(axis=0) - pts.min(axis=0)) if len(pts) else None,
        'cutouts': len(holes),
        'copper_layers': list(stackup(board.root, board.thickness)[0]),
        'colors': board.colors,
        'holes': [{'at': _round((x, y)), 'diameter': d, 'plated': not npth, 'ref': ref}
                  for x, y, d, npth, ref in drill_holes(board)],
    }
    if parts:
        from .kicad_import import part_meshes
        meshes = part_meshes(board, Path(pcb_path).parent / CACHE / 'step')
        out['parts'] = []
        for p in meshes:
            fp = p.footprint
            lo, hi = p.box
            top = float(hi[2] - board.thickness) if fp.side == 'top' else float(-lo[2])
            out['parts'].append({'ref': p.ref, 'value': fp.value, 'footprint': fp.lib, 'side': fp.side,
                                 'position': _round(p.position), 'rotation': p.rotation,
                                 'box_min': _round(lo), 'box_max': _round(hi), 'height': round(top, 3),
                                 'has_3d_model': p.has_model})
    return out


def design_boards(scad_path, source=None):
    """[(id, pcb path)] linked from a SCAD file."""
    source = _read(scad_path) if source is None else source
    return find_kicad_links(source, Path(scad_path))


def info(scad_path):
    """Everything an agent needs to start editing a design: boards (with poses and include API), variables, objects."""
    scad_path = Path(scad_path).resolve()
    source = _read(scad_path)
    out = {'file': str(scad_path), 'boards': [], 'variables': {}, 'modules': [], 'objects': [], 'groups': []}
    try:
        doc = Document(source)
    except SourceError as error:
        out['syntax_error'] = {'message': str(error), 'line': source.count('\n', 0, error.position or 0) + 1}
        doc = None
    for ident, path in design_boards(scad_path, source):
        include, _ = include_names(ident, path.stem)
        entry = {'id': ident, 'pcb': str(path), 'exists': path.is_file(),
                 'pose': read_pose(source, ident) or IDENTITY_POSE,
                 'include': f'kicad/{include}', 'included': f'kicad/{include}' in source,
                 'color': link_option(source, ident, 'color'),
                 'api': board_api(ident)}
        if path.is_file():
            try:
                summary = board_summary(path, parts=True)
                entry.update({k: summary[k] for k in ('size', 'thickness', 'outline_min', 'outline_max', 'copper_layers')})
                entry['mounting_holes'] = [h for h in summary['holes'] if str(h['ref']).upper().startswith('H') or h['diameter'] >= 2.5]
                entry['parts'] = [{k: p[k] for k in ('ref', 'value', 'side', 'position', 'height', 'box_min', 'box_max')}
                                  for p in summary['parts']]
            except (OSError, ValueError) as error:
                entry['error'] = str(error)
        out['boards'].append(entry)
    if doc is not None:
        for name, a, b in doc.variables():
            out['variables'][name] = source[a:b].strip()
            m = re.search(r'^[ \t]*' + re.escape(name) + r'\s*=[^;]*;([^\n]*)', source, re.M)
            if m and 'SCAB group' in m.group(1):
                out['groups'].append({'name': name, 'value': source[a:b].strip(), 'comment': m.group(1).strip()})
        for node in doc.nodes:
            if node.name == 'module':
                tokens = doc.tokens_in(node.start, node.end)
                if len(tokens) > 1:
                    out['modules'].append({'name': tokens[1].value, 'line': source.count('\n', 0, node.start) + 1})
        for root in doc.roots:
            line = source.count('\n', 0, root.start) + 1
            first = source[root.start:root.end].split('\n', 1)[0]
            out['objects'].append({'line': line, 'kind': root.name, 'code': first[:100]})
    return out


def board_api(ident):
    """Names the generated include defines for a board id (all in board coordinates)."""
    P = ident + '_'
    return {
        f'{P}board_size': '[width, depth] in mm',
        f'{P}board_min / {P}board_max': 'outline bounding box corners [x, y]',
        f'{P}board_thickness': 'board thickness (Z = 0 is the bottom of the board)',
        f'{P}board_outline': 'outline polygon [[x, y], ...]',
        f'{P}board_cutouts': 'inner cutout polygons',
        f'{P}board_holes': '[[[x, y], diameter, plated, ref], ...] every drilled hole',
        f'{P}mounting_holes': '[[[x, y], diameter], ...] holes of H* footprints or >= 2.5 mm',
        f'{P}parts': '[ref, value, footprint, [x, y], rotation, side, [min x,y,z], [max x,y,z], has_3d_model] per part',
        f'{P}parts_top': 'Z of the tallest part top',
        f'{P}part("REF")': 'one part record; [3] = position, [6]/[7] = 3D box',
        f'{P}part_box("REF", margin, extend)': 'box around a part (cutouts); extend [x,y,z] stretches it outwards',
        f'{P}place() {{ ... }}': 'puts board-coordinate geometry where the board sits (its pose)',
        f'{P}pcb(parts = true)': 'the board (+ parts STL) as geometry, board coordinates',
        f'{P}pose': '[[x, y, z], [rx, ry, rz]] — defined in YOUR file, not in the include',
    }


def update_includes(scad_path, executable=None, quick=True):
    """(Re)write kicad/<id>.scad and kicad/<id>-parts.stl for every board the file links. Returns [paths]."""
    scad_path = Path(scad_path).resolve()
    out = []
    cache = scad_path.parent / CACHE
    for ident, path in design_boards(scad_path):
        model = load_board(path, executable, cache, meshes=not quick)
        out.append(str(write_include(model, scad_path.parent / 'kicad', ident)))
    return out


def link(scad_path, pcb_path, ident=None, pose=None, executable=None):
    """Link a KiCad board to a SCAD file (link comment, pose variable, include) and write its include."""
    scad_path = Path(scad_path).resolve()
    pcb_path = Path(pcb_path).resolve()
    if not pcb_path.is_file():
        raise FileNotFoundError(pcb_path)
    source = _read(scad_path) if scad_path.exists() else ''
    taken = {i for i, _ in design_boards(scad_path, source)}
    if ident is None:
        ident = 'kicad' if 'kicad' not in taken else board_id(pcb_path.stem, taken)
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', ident) or ident in taken:
        raise ValueError(f'board id {ident!r} is not a free identifier')
    relative = os.path.relpath(pcb_path, scad_path.parent).replace(os.sep, '/')
    include, _ = include_names(ident, pcb_path.stem)
    source = add_link(source, ident, relative, include)
    if pose is not None:
        source = write_pose(source, ident, pose[0], pose[1])
    _write(scad_path, source)
    model = load_board(pcb_path, executable, scad_path.parent / CACHE, meshes=False)
    write_include(model, scad_path.parent / 'kicad', ident)
    return {'id': ident, 'include': f'kicad/{include}', 'pose': read_pose(source, ident)}


def unlink(scad_path, ident):
    scad_path = Path(scad_path).resolve()
    source = _read(scad_path)
    path = dict(design_boards(scad_path, source)).get(ident)
    include = include_names(ident, path.stem)[0] if path else None
    _write(scad_path, remove_link(source, ident, include))
    return {'removed': ident}


def set_pose(scad_path, ident, position, angles):
    scad_path = Path(scad_path).resolve()
    source = _read(scad_path)
    if ident not in dict(design_boards(scad_path, source)):
        raise ValueError(f'no board {ident!r} in {scad_path.name}')
    _write(scad_path, write_pose(source, ident, position, angles))
    return {'id': ident, 'pose': [list(position), list(angles)]}


# ------------------------------------------------------------------ compile
def _chassis_source(source):
    """The design without the statements that only draw boards (`<id>_pcb()`), for clearance and STL export."""
    try:
        doc = Document(source)
    except SourceError:
        return source
    out = source
    for root in reversed(doc.roots):
        text = source[root.start:root.end]
        if re.search(r'\b\w+_pcb\s*\(', text) and not re.search(r'\b(?:difference|intersection)\s*\(', text):
            out = out[:root.start] + re.sub(r'[^\n]', ' ', text) + out[root.end:]
    return out


def compile_design(scad_path, output=None, executable=None, source=None, timeout=600):
    """Run OpenSCAD on the design; returns (ok, problems, log, output path)."""
    scad_path = Path(scad_path).resolve()
    executable = openscad(executable)
    source = _read(scad_path) if source is None else source
    tmpdir = Path(tempfile.mkdtemp(prefix='scab-cli-'))
    try:
        output = Path(output) if output else tmpdir / 'out.stl'
        handle, temp = tempfile.mkstemp(prefix='.scab-', suffix='.scad', dir=scad_path.parent)
        os.close(handle)
        Path(temp).write_text(source, encoding='utf-8')
        try:
            result = subprocess.run([executable, '-o', str(output), temp], cwd=scad_path.parent, capture_output=True,
                                    text=True, timeout=timeout, env=dict(os.environ, QT_QPA_PLATFORM='offscreen'))
        finally:
            Path(temp).unlink(missing_ok=True)
        log = (result.stdout or '') + (result.stderr or '')
        mapper = PathMapper(scad_path)
        problems = [{'severity': p.severity, 'message': p.message,
                     'file': str(p.file) if p.file else None,
                     'line': (p.line + 1) if p.line is not None else None, 'hint': p.hint}
                    for p in parse_log(log, mapper)]
        empty = 'Current top level object is empty' in log
        ok = output.exists() and not any(p['severity'] == 'error' for p in problems)
        return ok or (empty and not any(p['severity'] == 'error' for p in problems)), problems, log, (output if output.exists() else None)
    finally:
        if output is None or not str(output).startswith(str(tmpdir)):
            shutil.rmtree(tmpdir, ignore_errors=True)


def _boards_with_models(scad_path, source, executable):
    from .boards import BoardEntry
    entries = []
    for ident, path in design_boards(scad_path, source):
        model = load_board(path, executable, Path(scad_path).parent / CACHE, meshes=True)
        write_include(model, Path(scad_path).parent / 'kicad', ident)
        entries.append(BoardEntry(ident, path, model=model, pose=read_pose(source, ident) or IDENTITY_POSE))
    return entries


def clearance(scad_path, minimum=1.0, executable=None):
    """Compile the chassis (boards left out) and check every part of every board against it and the other boards."""
    from .clearance import check
    from .meshio import load_mesh
    scad_path = Path(scad_path).resolve()
    executable = openscad(executable)
    source = _read(scad_path)
    entries = _boards_with_models(scad_path, source, executable)
    tmp = Path(tempfile.mkdtemp(prefix='scab-clearance-'))
    try:
        ok, problems, log, stl = compile_design(scad_path, tmp / 'chassis.stl', executable, _chassis_source(source))
        meshes = [load_mesh(stl)] if stl else []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    world = {e.ident: e.world_parts() for e in entries}
    rows = []
    for entry in entries:
        model = entry.model
        offset = model.part_offset
        upside_down = (entry.matrix[:3, :3] @ np.array([0., 0., 1.]))[2] < 0
        side = lambda p: ('bottom' if p.footprint.side == 'top' else 'top') if upside_down else p.footprint.side
        parts = [(p.ref, world[entry.ident][offset + i][0], p.has_model, side(p)) for i, p in enumerate(model.parts)]
        others = [(v, f) for other, items in world.items() if other != entry.ident
                  for k, (v, f, _) in enumerate(items) if k not in (1, 2, 3) and len(f) > 1]
        for r in check(parts, meshes, model.board.thickness, obstacles=others):
            lo, hi = model.parts[r.index].box
            height = float(hi[2] - model.board.thickness) if model.parts[r.index].footprint.side == 'top' else float(-lo[2])
            rows.append({'board': entry.ident, 'ref': r.ref, 'status': r.status(minimum),
                         'height': round(height, 3),
                         'headroom': None if not np.isfinite(r.headroom) else round(float(r.headroom), 3),
                         'gap': None if not np.isfinite(r.gap) else round(float(r.gap), 3),
                         'has_3d_model': r.has_model})
    counts = {s: sum(1 for r in rows if r['status'] == s) for s in ('collision', 'tight', 'ok')}
    return {'compiled': ok, 'problems': problems, 'minimum': minimum, 'counts': counts,
            'parts': sorted(rows, key=lambda r: ({'collision': 0, 'tight': 1, 'ok': 2}[r['status']], r['board'], r['ref']))}


def check_design(scad_path, minimum=1.0, executable=None):
    """The agent's verification step: syntax, OpenSCAD errors / warnings, then clearance."""
    scad_path = Path(scad_path).resolve()
    source = _read(scad_path)
    report = {'file': str(scad_path)}
    try:
        Document(source)
    except SourceError as error:
        line = source.count('\n', 0, error.position or 0) + 1
        report.update({'ok': False, 'stage': 'syntax', 'problems': [{'severity': 'error', 'message': str(error), 'line': line}]})
        return report
    executable = openscad(executable)
    result = clearance(scad_path, minimum, executable)
    report['problems'] = result['problems']
    report['clearance'] = {'minimum': minimum, 'counts': result['counts'],
                           'problems': [r for r in result['parts'] if r['status'] != 'ok'],
                           'parts': result['parts']}
    errors = [p for p in result['problems'] if p['severity'] == 'error']
    report['ok'] = bool(result['compiled']) and not errors and not result['counts']['collision']
    report['stage'] = 'compile' if errors or not result['compiled'] else 'clearance'
    return report


VIEWS = {'iso': (55, 0, 25), 'top': (0, 0, 0), 'front': (90, 0, 0), 'right': (90, 0, 90), 'back': (90, 0, 180),
         'left': (90, 0, 270), 'bottom': (180, 0, 0)}


def render_png(scad_path, output, view='iso', size=(1200, 900), boards=True, executable=None):
    """A PNG of the design (OpenSCAD's renderer). boards=True also draws every linked board at its pose."""
    scad_path = Path(scad_path).resolve()
    executable = openscad(executable)
    source = _read(scad_path)
    if boards:
        update_includes(scad_path, executable)
        extra = ''.join(f'\n{ident}_place() {ident}_pcb();' for ident, _ in design_boards(scad_path, source)
                        if f'{ident}_pcb' not in source)
        source += extra
    rx, ry, rz = VIEWS.get(view, VIEWS['iso'])
    handle, temp = tempfile.mkstemp(prefix='.scab-', suffix='.scad', dir=scad_path.parent)
    os.close(handle)
    Path(temp).write_text(source, encoding='utf-8')
    try:
        command = [executable, '-o', str(Path(output).resolve()), '--imgsize', f'{size[0]},{size[1]}',
                   '--camera', f'0,0,0,{rx},{ry},{rz},0', '--viewall', '--autocenter', '--projection', 'p',
                   '--colorscheme', 'Tomorrow Night', temp]
        import sys
        if sys.platform.startswith('linux') and not os.environ.get('DISPLAY') and shutil.which('xvfb-run'):
            command = ['xvfb-run', '-a'] + command          # OpenSCAD's PNG export needs an OpenGL context
        if Path(output).exists():
            Path(output).unlink()
        result = subprocess.run(command, cwd=scad_path.parent, capture_output=True, text=True, timeout=600)
    finally:
        Path(temp).unlink(missing_ok=True)
    if not Path(output).exists() or Path(output).stat().st_size == 0:
        raise RuntimeError('OpenSCAD could not render a PNG (it needs OpenGL; on a headless Linux box run it under '
                           'xvfb-run):\n' + ((result.stdout or '') + (result.stderr or ''))[-2000:])
    return {'png': str(Path(output).resolve()), 'view': view}


def export(scad_path, output, executable=None):
    """.stl: the chassis (boards left out). .step: chassis + every board with its 3D parts, at their poses."""
    scad_path = Path(scad_path).resolve()
    output = Path(output).resolve()
    executable = openscad(executable)
    source = _read(scad_path)
    if output.suffix.lower() == '.stl':
        update_includes(scad_path, executable)
        ok, problems, log, stl = compile_design(scad_path, output, executable, _chassis_source(source))
        return {'ok': ok, 'file': str(output), 'problems': problems}
    if output.suffix.lower() in ('.step', '.stp'):
        from .meshio import load_mesh
        from .stepwrite import write_step
        entries = _boards_with_models(scad_path, source, executable)
        tmp = Path(tempfile.mkdtemp(prefix='scab-step-'))
        try:
            ok, problems, log, stl = compile_design(scad_path, tmp / 'chassis.stl', executable, _chassis_source(source))
            bodies = []
            if stl:
                v, f = load_mesh(stl)
                bodies.append((scad_path.stem, v, f, (.72, .74, .78, 1.)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        for entry in entries:
            world = entry.world_parts()
            v, f, c = world[0]
            if len(f) > 1:
                bodies.append((f'{entry.ident}-board', v, f, tuple(c[0])))
            for i, part in enumerate(entry.model.parts):
                v, f, c = world[entry.model.part_offset + i]
                bodies.append((f'{entry.ident}-{part.ref}', v, f, tuple(c[0]) if len(c) else (.5, .5, .5, 1.)))
        write_step(output, bodies, name=scad_path.stem)
        return {'ok': ok, 'file': str(output), 'bodies': len(bodies), 'problems': problems}
    raise ValueError('export to .stl or .step')
