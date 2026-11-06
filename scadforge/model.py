"""Lossless lexical source ranges. OpenSCAD remains the geometry authority."""
from dataclasses import dataclass, field
import re

TOKEN = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\s+|[$A-Za-z_][$\w]*|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|.', re.UNICODE)

@dataclass
class Token:
    value: str
    start: int
    end: int

@dataclass
class Node:
    start: int
    end: int
    name: str
    kind: str = 'geometry'
    children: list = field(default_factory=list)

class SourceError(ValueError):
    pass

class Document:
    def __init__(self, source):
        self.source = source
        self.tokens = [Token(m[0], m.start(), m.end()) for m in TOKEN.finditer(source)
                       if not m[0].isspace() and not m[0].startswith(('//', '/*'))]
        self.pos = 0
        self.nodes = []
        while self.pos < len(self.tokens):
            self.nodes.append(self.statement())
        self.roots = [n for n in self.nodes if n.kind == 'geometry']

    def at(self, offset=0):
        p = self.pos + offset
        return self.tokens[p].value if p < len(self.tokens) else ''

    def balanced(self, opening, closing):
        if self.at() != opening:
            raise SourceError(f'Expected {opening}')
        self.pos += 1
        pairs = {'(': ')', '[': ']', '{': '}'}
        stack = [closing]
        while self.pos < len(self.tokens):
            v = self.at()
            self.pos += 1
            if opening == '<':
                if v == '>': return
                continue
            if v in pairs:
                stack.append(pairs[v])
            elif v in (')', ']', '}'):
                if v != stack[-1]:
                    raise SourceError(f'Expected {stack[-1]}, found {v}')
                stack.pop()
                if not stack: return
        raise SourceError(f'Missing {closing}')

    def until_semicolon(self):
        while self.pos < len(self.tokens):
            v = self.at()
            if v == ';':
                self.pos += 1
                return
            if v in ('(', '['):
                self.balanced(v, ')' if v == '(' else ']')
            else:
                self.pos += 1
        raise SourceError('Missing semicolon')

    def statement(self):
        if not self.at():
            raise SourceError('Expected statement')
        start = self.tokens[self.pos].start
        name = self.at()
        children = []
        kind = 'geometry'
        if name == '}':
            raise SourceError('Unexpected }')
        if name in ('include', 'use'):
            self.pos += 1
            self.balanced('<', '>')
            if self.at() == ';': self.pos += 1
            kind = 'definition'
        elif name == 'function' or self.at(1) == '=':
            self.until_semicolon()
            kind = 'definition'
        elif name == 'module':
            self.pos += 1
            if not self.at(): raise SourceError('Expected module name')
            self.pos += 1
            self.balanced('(', ')')
            children = [self.statement()]
            kind = 'definition'
        elif name == '{':
            self.pos += 1
            while self.at() and self.at() != '}':
                children.append(self.statement())
            if self.at() != '}': raise SourceError('Missing }')
            self.pos += 1
            name = 'group'
        elif name == ';':
            self.pos += 1
            kind = 'definition'
        else:
            while self.at() in ('!', '#', '%', '*'):
                self.pos += 1
            name = self.at()
            if not re.fullmatch(r'[$A-Za-z_][$\w]*', name):
                raise SourceError(f'Unexpected token {name!r}')
            self.pos += 1
            self.balanced('(', ')')
            if self.at() == ';':
                self.pos += 1
            else:
                children.append(self.statement())
            if name == 'if' and self.at() == 'else':
                self.pos += 1
                children.append(self.statement())
        if name in ('echo','assert') and not children:kind='definition'
        return Node(start, self.tokens[self.pos-1].end, name, kind, children)

    def isolated(self, root):
        # Blank other roots while preserving line numbers, definitions and their scopes.
        result = self.source
        for n in reversed(self.roots):
            if n is not root:
                result = result[:n.start] + re.sub(r'[^\n]', ' ', result[n.start:n.end]) + result[n.end:]
        return result

    def variables(self):
        out = []
        for n in self.nodes:
            ts = [t for t in self.tokens if n.start <= t.start < n.end]
            if len(ts) >= 3 and ts[1].value == '=':
                out.append((ts[0].value, ts[1].end, ts[-1].start))
        return out

    def walk(self):
        def visit(n):
            yield n
            for c in n.children: yield from visit(c)
        for n in self.roots: yield from visit(n)

    def wrap(self, node, prefix):
        return self.source[:node.start] + prefix + ' {\n' + self.source[node.start:node.end] + '\n}' + self.source[node.end:]

    def duplicate(self, node, offset='[10, 0, 0]'):
        return self.source[:node.end] + '\ntranslate(' + offset + ') {\n' + self.source[node.start:node.end] + '\n}\n' + self.source[node.end:]

    def pattern(self, node, count, spacing, radial=False):
        # Fresh loop identifier avoids variable capture in user models.
        i = '__forge_i'
        while re.search(r'\b' + i + r'\b', self.source): i += '_'
        if radial:
            prefix = f'for ({i} = [0:{count - 1}]) rotate([0, 0, {i} * 360 / {count}])'
        else:
            prefix = f'for ({i} = [0:{count - 1}]) translate([{i} * ({spacing}), 0, 0])'
        return self.wrap(node, prefix)

    def section(self, axis, position, extent):
        # Real, portable solid intersection, distinct from the GPU inspection plane.
        mins = [-extent]*3
        sizes = [2*extent]*3
        a = 'XYZ'.index(axis)
        mins[a] = f'({position}) - {2*extent}'
        code = self.source
        for n in reversed(self.roots):
            code = code[:n.start] + 'intersection() {\n' + code[n.start:n.end] + f'\ntranslate([{", ".join(map(str, mins))}]) cube({sizes});\n}}' + code[n.end:]
        return code

SIGNATURES = {
 'multmatrix': 'multmatrix([[m11,m12,m13,m14], ...]) children();',
 'screw': 'screw("M6x1", length=12, tolerance="6g", shaft_undersize=0) · BOSL2',
 'screw_hole': 'screw_hole("M6x1", thread=true, length=12, tolerance="6H", hole_oversize=0) · BOSL2',
 'cube': 'cube(size = [x, y, z], center = false)',
 'sphere': 'sphere(r = radius)  ·  sphere(d = diameter)',
 'cylinder': 'cylinder(h = height, r1 = radius, r2 = radius, center = false)',
 'translate': 'translate([x, y, z]) children();',
 'rotate': 'rotate([x_degrees, y_degrees, z_degrees]) children();',
 'scale': 'scale([x, y, z]) children();',
 'color': 'color("name")  ·  color([r, g, b, alpha]) children();',
 'difference': 'difference() { base(); cutters(); }',
 'intersection': 'intersection() { first(); second(); }',
 'union': 'union() { children(); }',
 'hull': 'hull() { children(); }',
 'minkowski': 'minkowski() { children(); }',
 'linear_extrude': 'linear_extrude(height, center = false, twist = 0, scale = 1)',
 'rotate_extrude': 'rotate_extrude(angle = 360, convexity = 2)',
 'circle': 'circle(r = radius)  ·  circle(d = diameter)',
 'square': 'square(size = [x, y], center = false)',
 'polygon': 'polygon(points = [[x, y], ...], paths = undef)',
 'mirror': 'mirror([x, y, z]) children();',
 'offset': 'offset(r = radius)  ·  offset(delta = distance)',
 'text': 'text("text", size = 10, font = "Liberation Sans")',
 'import': 'import("file.stl", convexity = 10)',
 'projection': 'projection(cut = false) children();',
 'sin': 'sin(angle_in_degrees)', 'cos': 'cos(angle_in_degrees)',
 'for': 'for (i = [start:step:end]) children();',
}
