"""Minimal VRML 2.0 / 97 reader for KiCad 3D models (.wrl).

Supports Transform (translation, rotation, scale, center, scaleOrientation),
Group / children, Shape with Material (diffuseColor, transparency) and
IndexedFaceSet (coord Coordinate point, coordIndex, polygons fan-triangulated),
DEF / USE and Inline (relative url). Returns meshes grouped by color.
"""
from pathlib import Path
import re
import numpy as np

TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[\[\]{}]|[^\s\[\]{},#"]+|#[^\n]*', re.S)
DEFAULT_COLOR = (.7, .7, .7, 1.)


def _tokens(text):
    for m in TOKEN.finditer(text):
        t = m.group(0)
        if not t.startswith('#'):
            yield t


class Parser:
    def __init__(self, text):
        self.tokens = list(_tokens(text))
        self.i = 0
        self.defs = {}

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self):
        token = self.peek()
        self.i += 1
        return token

    def numbers(self):
        """Read a number list: either a [ ... ] block or consecutive bare numbers."""
        values = []
        if self.peek() == '[':
            self.take()
            while self.peek() not in (']', None):
                try:
                    values.append(float(self.take()))
                except ValueError:
                    pass
            self.take()
            return values
        while self.peek() is not None:
            try:
                values.append(float(self.peek()))
                self.i += 1
            except ValueError:
                break
        return values

    def node(self):
        token = self.take()
        if token == 'DEF':
            name = self.take()
            value = self.node()
            self.defs[name] = value
            return value
        if token == 'USE':
            return self.defs.get(self.take())
        if token is None:
            return None
        if self.peek() != '{':
            return None
        self.take()
        node = {'type': token}
        while self.peek() not in ('}', None):
            field = self.take()
            if field in ('children', 'addChildren'):
                kids = []
                if self.peek() == '[':
                    self.take()
                    while self.peek() not in (']', None):
                        child = self.node()
                        if child is not None:
                            kids.append(child)
                    self.take()
                else:
                    child = self.node()
                    if child is not None:
                        kids.append(child)
                node['children'] = kids
            elif field in ('appearance', 'material', 'geometry', 'coord', 'normal', 'color', 'texCoord', 'texture'):
                node[field] = self.node()
            elif field in ('url',):
                if self.peek() == '[':
                    self.take()
                    urls = []
                    while self.peek() not in (']', None):
                        urls.append(self.take().strip('"'))
                    self.take()
                    node['url'] = urls
                else:
                    node['url'] = [self.take().strip('"')]
            elif field in ('point', 'coordIndex', 'diffuseColor', 'transparency', 'translation', 'rotation', 'scale',
                           'center', 'scaleOrientation', 'emissiveColor', 'specularColor', 'shininess', 'ambientIntensity',
                           'creaseAngle', 'colorIndex', 'normalIndex', 'vector'):
                node[field] = self.numbers()
            elif field in ('solid', 'ccw', 'convex', 'colorPerVertex', 'normalPerVertex', 'title', 'info', 'name', 'string'):
                if self.peek() == '[':
                    depth = 0
                    while True:
                        t = self.take()
                        depth += t == '['
                        depth -= t == ']'
                        if depth == 0 or t is None:
                            break
                else:
                    self.take()
            else:
                # unknown field: skip a value (node, list or scalar)
                if self.peek() == '[':
                    depth = 0
                    while True:
                        t = self.take()
                        depth += t == '['
                        depth -= t == ']'
                        if depth == 0 or t is None:
                            break
                elif self.i + 1 < len(self.tokens) and self.tokens[self.i + 1] == '{':
                    self.node()
                else:
                    self.take()
        self.take()
        return node

    def parse(self):
        nodes = []
        while self.peek() is not None:
            before = self.i
            node = self.node()
            if node is not None:
                nodes.append(node)
            if self.i == before:
                self.i += 1
        return nodes


def axis_angle(values):
    if len(values) < 4:
        return np.eye(4)
    x, y, z, angle = values[:4]
    axis = np.array([x, y, z], dtype=float)
    n = np.linalg.norm(axis)
    m = np.eye(4)
    if n < 1e-12 or abs(angle) < 1e-12:
        return m
    axis /= n
    c, s = np.cos(angle), np.sin(angle)
    kx, ky, kz = axis
    k = np.array([[0, -kz, ky], [kz, 0, -kx], [-ky, kx, 0]])
    m[:3, :3] = np.eye(3) * c + (1 - c) * np.outer(axis, axis) + s * k
    return m


def transform_matrix(node):
    t = np.eye(4)
    if node.get('translation'):
        t[:3, 3] = node['translation'][:3]
    c = np.eye(4)
    ci = np.eye(4)
    if node.get('center'):
        c[:3, 3] = node['center'][:3]
        ci[:3, 3] = [-v for v in node['center'][:3]]
    r = axis_angle(node.get('rotation', []))
    s = np.eye(4)
    if node.get('scale'):
        s[0, 0], s[1, 1], s[2, 2] = node['scale'][:3]
    so = axis_angle(node.get('scaleOrientation', []))
    return t @ c @ r @ so @ s @ np.linalg.inv(so) @ ci


def load(path, _depth=0):
    """Read a .wrl file -> list of (vertices (n,3) float32, faces (m,3) uint32, rgba)."""
    path = Path(path)
    text = path.read_text(encoding='utf-8', errors='replace')
    parser = Parser(text)
    nodes = parser.parse()
    groups = {}

    def material_color(shape):
        appearance = shape.get('appearance') or {}
        material = appearance.get('material') or {}
        diffuse = material.get('diffuseColor') or DEFAULT_COLOR[:3]
        alpha = 1. - (material.get('transparency') or [0.])[0]
        return tuple(round(float(v), 4) for v in list(diffuse[:3]) + [alpha])

    def visit(node, matrix):
        if node is None:
            return
        kind = node.get('type')
        if kind == 'Transform':
            matrix = matrix @ transform_matrix(node)
        if kind == 'Inline' and _depth < 4:
            for url in node.get('url', []):
                target = (path.parent / url)
                if target.is_file():
                    for v, f, color in load(target, _depth + 1):
                        placed = (np.c_[v, np.ones(len(v))] @ matrix.T)[:, :3]
                        groups.setdefault(color, []).append((placed, f))
                    break
            return
        if kind == 'Shape':
            geometry = node.get('geometry') or {}
            if geometry.get('type') == 'IndexedFaceSet':
                points = np.asarray((geometry.get('coord') or {}).get('point') or [], dtype=float)
                if len(points) % 3:
                    points = points[:len(points) - len(points) % 3]
                points = points.reshape(-1, 3)
                index = [int(v) for v in geometry.get('coordIndex') or []]
                faces = []
                polygon = []
                for value in index + [-1]:
                    if value < 0:
                        for k in range(1, len(polygon) - 1):
                            faces.append((polygon[0], polygon[k], polygon[k + 1]))
                        polygon = []
                    elif value < len(points):
                        polygon.append(value)
                if faces and len(points):
                    placed = (np.c_[points, np.ones(len(points))] @ matrix.T)[:, :3]
                    groups.setdefault(material_color(node), []).append((placed, np.asarray(faces, dtype=np.int64)))
            return
        for child in node.get('children', []):
            visit(child, matrix)

    for node in nodes:
        visit(node, np.eye(4))
    out = []
    for color, parts in groups.items():
        vertices, faces, offset = [], [], 0
        for v, f in parts:
            vertices.append(v)
            faces.append(f + offset)
            offset += len(v)
        out.append((np.vstack(vertices).astype(np.float32), np.vstack(faces).astype(np.uint32), color))
    return out


def write(path, shapes, scale=1 / 2.54):
    """Write [(vertices_mm, faces, rgba)] as a KiCad-style VRML model (0.1 inch units)."""
    lines = ['#VRML V2.0 utf8', '# generated by SCAB', 'Transform {', f'  scale {scale:.6f} {scale:.6f} {scale:.6f}', '  children [']
    for vertices, faces, color in shapes:
        r, g, b, a = color
        lines.append('    Shape {')
        lines.append(f'      appearance Appearance {{ material Material {{ diffuseColor {r:.3f} {g:.3f} {b:.3f} '
                     f'specularColor 0.2 0.2 0.2 shininess 0.3 transparency {1 - a:.3f} }} }}')
        lines.append('      geometry IndexedFaceSet {')
        lines.append('        coord Coordinate { point [ ' + ', '.join(f'{x:.4f} {y:.4f} {z:.4f}' for x, y, z in vertices) + ' ] }')
        lines.append('        coordIndex [ ' + ', '.join(f'{a_}, {b_}, {c_}, -1' for a_, b_, c_ in faces) + ' ]')
        lines.append('      }')
        lines.append('    }')
    lines += ['  ]', '}']
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')
