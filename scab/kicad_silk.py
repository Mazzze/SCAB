"""Exact silkscreen from KiCad: `kicad-cli pcb export svg` per side, parsed into strokes.

KiCad draws text with its own stroke font; exporting the layer and turning
the strokes into thin 3D plates reproduces it exactly (positions, sizes,
justification, mirroring, knockout-free text and graphics).
"""
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET


@dataclass
class SilkLayer:
    strokes: list = field(default_factory=list)    # (points [(x, y), ...] KiCad mm, width)
    circles: list = field(default_factory=list)    # (cx, cy, r, width, filled)
    fills: list = field(default_factory=list)      # [(x, y), ...] filled polygons


def find_cli():
    configured = os.environ.get('SCAB_KICAD')
    if configured:
        for name in ('kicad-cli', 'kicad-cli.exe'):
            for base in (Path(configured), Path(configured) / 'bin'):
                if (base / name).is_file():
                    return str(base / name)
    found = shutil.which('kicad-cli')
    if found:
        return found
    from .kicad_launch import VERSIONS
    candidates = [Path('/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli')]
    for root in (os.environ.get('ProgramFiles', r'C:\Program Files'),):
        for version in VERSIONS:
            candidates.append(Path(root) / 'KiCad' / version / 'bin' / 'kicad-cli.exe')
    return next((str(p) for p in candidates if p.is_file()), None)


def _length_mm(value):
    m = re.match(r'\s*([-\d.eE]+)\s*(mm|cm|in|px|pt)?', value or '')
    if not m:
        return None
    number = float(m.group(1))
    return number * {'mm': 1., 'cm': 10., 'in': 25.4, 'px': 25.4 / 96, 'pt': 25.4 / 72, None: 25.4 / 96}[m.group(2)]


def _style(element, inherited):
    style = dict(inherited)
    for part in (element.get('style') or '').replace('\n', ' ').split(';'):
        if ':' in part:
            key, value = part.split(':', 1)
            style[key.strip()] = value.strip()
    for key in ('stroke-width', 'fill-opacity', 'fill', 'stroke'):
        if element.get(key) is not None:
            style[key] = element.get(key)
    return style


def _arc_points(x1, y1, rx, ry, phi, large, sweep, x2, y2, step=10.):
    """SVG elliptical arc (KiCad only writes circular ones) -> points."""
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    r = rx
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    d2 = dx * dx + dy * dy
    r = max(r, math.sqrt(d2))
    factor = math.sqrt(max(0., r * r - d2) / d2) if d2 else 0.
    if large == sweep:
        factor = -factor
    cx = (x1 + x2) / 2 + factor * dy
    cy = (y1 + y2) / 2 - factor * dx
    a1 = math.atan2(y1 - cy, x1 - cx)
    a2 = math.atan2(y2 - cy, x2 - cx)
    delta = a2 - a1
    if sweep and delta < 0:
        delta += 2 * math.pi
    elif not sweep and delta > 0:
        delta -= 2 * math.pi
    n = max(2, int(abs(math.degrees(delta)) / step) + 1)
    return [(cx + r * math.cos(a1 + delta * k / n), cy + r * math.sin(a1 + delta * k / n)) for k in range(1, n + 1)]


def _path(d):
    """Polylines from an SVG path made of M / L / H / V / A / Z (absolute or relative)."""
    tokens = re.findall(r'[MmLlHhVvAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', d)
    lines, current, closed = [], [], []
    x = y = 0.
    i = 0
    command = None
    while i < len(tokens):
        token = tokens[i]
        if re.match(r'[A-Za-z]', token):
            command = token
            i += 1
            if command in 'Zz':
                if current:
                    current.append(current[0])
                    lines.append(current)
                    closed.append(True)
                    x, y = current[0]
                current = []
            continue
        relative = command.islower()
        c = command.upper()
        if c in 'ML':
            nx, ny = float(tokens[i]), float(tokens[i + 1])
            i += 2
            if relative:
                nx, ny = x + nx, y + ny
            if c == 'M':
                if len(current) > 1:
                    lines.append(current)
                    closed.append(False)
                current = [(nx, ny)]
                command = 'l' if relative else 'L'
            else:
                current.append((nx, ny))
            x, y = nx, ny
        elif c in 'HV':
            value = float(tokens[i])
            i += 1
            if c == 'H':
                x = x + value if relative else value
            else:
                y = y + value if relative else value
            current.append((x, y))
        elif c == 'A':
            rx, ry, phi, large, sweep, nx, ny = (float(t) for t in tokens[i:i + 7])
            i += 7
            if relative:
                nx, ny = x + nx, y + ny
            if not current:
                current = [(x, y)]
            current += _arc_points(x, y, rx, ry, phi, int(large), int(sweep), nx, ny)
            x, y = nx, ny
        else:
            i += 1
    if len(current) > 1:
        lines.append(current)
        closed.append(False)
    return lines, closed


def parse_svg(text):
    """SilkLayer in KiCad millimetre coordinates from kicad-cli's SVG."""
    root = ET.fromstring(text)
    ns = '{http://www.w3.org/2000/svg}'
    view = [float(v) for v in (root.get('viewBox') or '0 0 1 1').split()]
    width_mm = _length_mm(root.get('width')) or view[2]
    scale = width_mm / view[2] if view[2] else 1.
    layer = SilkLayer()

    def mm(x, y):
        return ((x - view[0]) * scale, (y - view[1]) * scale)

    def visit(element, style):
        style = _style(element, style)
        tag = element.tag.replace(ns, '')
        width = float(re.match(r'[-\d.eE]+', style.get('stroke-width', '0')).group(0)) * scale if style.get('stroke-width') else 0.
        filled = style.get('fill', 'none') != 'none' and float(style.get('fill-opacity', '1') or 1) > 0
        if tag == 'path':
            lines, closed = _path(element.get('d', ''))
            for points, is_closed in zip(lines, closed):
                points = [mm(px, py) for px, py in points]
                if filled and is_closed and len(points) > 3:
                    layer.fills.append(points)
                    if width > 0:
                        layer.strokes.append((points, width))
                elif width > 0:
                    layer.strokes.append((points, width))
        elif tag == 'circle':
            cx, cy = mm(float(element.get('cx', 0)), float(element.get('cy', 0)))
            layer.circles.append((cx, cy, float(element.get('r', 0)) * scale, width, filled))
        elif tag in ('polyline', 'polygon'):
            numbers = [float(v) for v in re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', element.get('points', ''))]
            points = [mm(a, b) for a, b in zip(numbers[::2], numbers[1::2])]
            if tag == 'polygon' and points:
                points.append(points[0])
                if filled:
                    layer.fills.append(points)
            if width > 0 and len(points) > 1:
                layer.strokes.append((points, width))
        if tag == 'text':
            return                      # invisible copy of stroked text
        for child in element:
            visit(child, style)
    visit(root, {})
    return layer


def export(board_path, cache_dir=None, timeout=60):
    """{'top': SilkLayer, 'bottom': SilkLayer} from kicad-cli, or None when unavailable."""
    cli = find_cli()
    if cli is None:
        return None
    board_path = Path(board_path)
    digest = hashlib.sha256(board_path.read_bytes() + cli.encode()).hexdigest()[:20]
    cache = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / 'scab-silk'
    cache.mkdir(parents=True, exist_ok=True)
    out = {}
    for side, layer in (('top', 'F.SilkS'), ('bottom', 'B.SilkS')):
        target = cache / f'silk-{digest}-{side}.svg'
        if not target.exists():
            partial = target.with_suffix('.part.svg')
            result = subprocess.run([cli, 'pcb', 'export', 'svg', '--layers', layer, '--exclude-drawing-sheet',
                                     '--black-and-white', '-o', str(partial), str(board_path)],
                                    capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0 or not partial.exists():
                return None
            partial.replace(target)
        try:
            out[side] = parse_svg(target.read_text(encoding='utf-8', errors='replace'))
        except ET.ParseError:
            return None
    return out
