"""Export each top-level object of a design to its own STL (for printing).

Objects that draw the KiCad board (kicad_pcb()) and objects disabled with the
% or * modifier are skipped: the files contain only the chassis parts.
"""
from pathlib import Path
import re
import threading

SKIP_CALLS = ('kicad_pcb(',)


def slug(text):
    text = re.sub(r'[^A-Za-z0-9]+', '-', text.strip().lower()).strip('-')
    return text[:40] or 'part'


def part_name(source, node, index):
    """Name from the comment right above the object, else its module call, else a number."""
    before = source[:node.start].rstrip(' \t')
    lines = before.split('\n')
    comment = None
    # the comment block directly above (no blank line in between)
    for line in reversed(lines[:-1] if lines and not lines[-1].strip() else lines):
        stripped = line.strip()
        if stripped.startswith('//'):
            comment = stripped.lstrip('/').strip()
            continue
        break
    if comment:
        head = re.split(r'[:.,;(—–-]', comment, maxsplit=1)[0]
        words = head.split()
        if 0 < len(words) <= 4:
            return slug(head)
    text = source[node.start:node.end]
    calls = [m.group(1) for m in re.finditer(r'([A-Za-z_]\w*)\s*\(', text)]
    builtin = {'color', 'translate', 'rotate', 'scale', 'mirror', 'multmatrix', 'difference', 'union', 'intersection',
               'hull', 'minkowski', 'linear_extrude', 'rotate_extrude', 'for', 'if', 'let', 'render', 'offset', 'cube',
               'sphere', 'cylinder', 'polyhedron', 'square', 'circle', 'polygon', 'text', 'import', 'projection', 'resize'}
    user = next((c for c in calls if c not in builtin), None)
    return slug(user) if user else f'part-{index + 1}'


def chassis_parts(doc):
    """[(node, name)] for every exportable top-level object."""
    out = []
    used = set()
    for index, node in enumerate(doc.roots):
        text = doc.source[node.start:node.end]
        if text.lstrip().startswith(('%', '*')) or any(call in text for call in SKIP_CALLS):
            continue
        name = part_name(doc.source, node, index)
        base, n = name, 2
        while name in used:
            name = f'{base}-{n}'
            n += 1
        used.add(name)
        out.append((node, name))
    return out


def export(doc, directory, executable, folder, prefix='', t=0.0, cancel=None, progress=None):
    """Run OpenSCAD for each part; returns [(name, path or None, message)]."""
    from .compiler import run_openscad, Cancelled
    cancel = cancel or threading.Event()
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    parts = chassis_parts(doc)
    for i, (node, name) in enumerate(parts):
        if cancel.is_set():
            break
        target = folder / f'{prefix}{name}.stl'
        if progress:
            progress(i, len(parts), name)
        try:
            run_openscad(doc.isolated(node), directory, executable, target, t, cancel, timeout=1800)
            results.append((name, target if target.exists() else None, '' if target.exists() else 'empty object'))
        except Cancelled:
            break
        except RuntimeError as error:
            results.append((name, None, str(error).strip().splitlines()[-1] if str(error).strip() else 'failed'))
    return results
