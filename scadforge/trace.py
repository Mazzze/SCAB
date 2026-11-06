"""Evaluate an OpenSCAD design far enough to know *where every primitive comes from*.

The tracer interprets the AST from :mod:`lang` the way OpenSCAD instantiates
it (include/use, modules, children(), for/if/let, functions, $-variables,
transforms, color) but produces no geometry.  For each primitive it records a
:class:`Leaf` with

* its world matrix and local bounding box / analytic shape (for picking),
* the chain of :class:`Frame` objects from the top-level statement down to the
  primitive call (module calls in any file, transforms, colors, loops),
* the scopes needed to explain where any argument value is written.

OpenSCAD itself remains the geometry authority; the trace is used to map a
point on the compiled mesh back to source and to edit parameters there.
"""
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import sys
import threading
import numpy as np
from . import lang
from .lang import (Num, Str, Bool, Undef, Id, Vector, Range, Unary, Binary, Ternary, Call, Index, Member, Let,
                   AssertExpr, EchoExpr, FunctionLiteral, CompFor, CompForC, CompIf, CompLet, Each, Assign,
                   ModuleDef, FunctionDef, Include, Instance, Block, If, Empty)

BUILTIN_PARAMS = {
    'cube': ['size', 'center'], 'sphere': ['r', 'd'], 'cylinder': ['h', 'r1', 'r2', 'center', 'r', 'd', 'd1', 'd2'],
    'polyhedron': ['points', 'faces', 'convexity', 'triangles'], 'square': ['size', 'center'], 'circle': ['r', 'd'],
    'polygon': ['points', 'paths', 'convexity'],
    'text': ['text', 'size', 'font', 'halign', 'valign', 'spacing', 'direction', 'language', 'script'],
    'import': ['file', 'convexity', 'layer', 'origin', 'scale', 'center', 'dpi', 'id'],
    'surface': ['file', 'center', 'invert', 'convexity'], 'translate': ['v'], 'rotate': ['a', 'v'], 'scale': ['v'],
    'mirror': ['v'], 'multmatrix': ['m'], 'resize': ['newsize', 'auto', 'convexity'], 'color': ['c', 'alpha'],
    'offset': ['r', 'delta', 'chamfer'],
    'linear_extrude': ['height', 'center', 'convexity', 'twist', 'slices', 'scale', 'segments'],
    'rotate_extrude': ['angle', 'convexity'], 'projection': ['cut'], 'union': [], 'difference': [],
    'intersection': [], 'hull': [], 'minkowski': ['convexity'], 'render': ['convexity'], 'group': [],
    'children': ['index'], 'echo': [], 'assert': ['condition', 'message'], 'let': [], 'for': [],
    'intersection_for': [], 'roof': ['method', 'convexity'], 'fill': [],
}
BUILTIN_DEFAULTS = {'cube': {'center': 'false'}, 'cylinder': {'center': 'false'}, 'square': {'center': 'false'}}
PRIMITIVES_3D = {'cube', 'sphere', 'cylinder', 'polyhedron', 'import', 'surface'}
PRIMITIVES_2D = {'square', 'circle', 'polygon', 'text'}
TRANSFORMS = {'translate', 'rotate', 'scale', 'mirror', 'multmatrix', 'resize'}
GROUPS = {'union', 'group', 'hull', 'minkowski', 'render', 'projection', 'offset', 'fill', 'roof', 'intersection'}


class Budget(Exception):
    """The trace exceeded its step / leaf budget; results are partial."""


class Unknown:
    """Value that could not be determined statically (behaves like undef)."""
    def __repr__(self):
        return 'unknown'


UNKNOWN = Unknown()


class RangeValue:
    __slots__ = ('begin', 'step', 'end')

    def __init__(self, begin, step, end):
        self.begin, self.step, self.end = begin, step, end

    def values(self, limit=100000):
        b, s, e = self.begin, self.step, self.end
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (b, s, e)):
            return []
        if s == 0:
            return []
        if (s > 0 and b > e) or (s < 0 and b < e):
            if self.step == 1 and b > e:
                b, e = e, b
            else:
                return []
        count = int(math.floor((e - b) / s + 1e-9)) + 1
        if count > limit:
            raise Budget(f'Range with {count} elements')
        return [b + i * s for i in range(count)]

    def iter_values(self):
        b, s, e = self.begin, self.step, self.end
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (b, s, e)) or s == 0:
            return
        if (s > 0 and b > e) or (s < 0 and b < e):
            if s == 1 and b > e:
                b, e = e, b
            else:
                return
        count = int(math.floor((e - b) / s + 1e-9)) + 1
        for i in range(count):
            yield b + i * s

    def __repr__(self):
        return f'[{fmt(self.begin)}:{fmt(self.step)}:{fmt(self.end)}]'


class FunctionValue:
    __slots__ = ('params', 'body', 'scope', 'name')

    def __init__(self, params, body, scope, name='function'):
        self.params, self.body, self.scope, self.name = params, body, scope, name


def fmt(value, digits=6):
    """Format a value the way OpenSCAD source would write it."""
    if value is None or isinstance(value, Unknown):
        return 'undef'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, float):
        if math.isinf(value):
            return 'inf' if value > 0 else '-inf'
        if math.isnan(value):
            return 'nan'
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return f'{value:.{digits}g}'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if isinstance(value, list):
        return '[' + ', '.join(fmt(v, digits) for v in value) + ']'
    if isinstance(value, RangeValue):
        return repr(value)
    if isinstance(value, FunctionValue):
        return 'function(...)'
    return str(value)


# ------------------------------------------------------------------ bindings
class Binding:
    """Where a name got its value: assignment, module argument, default, loop..."""
    __slots__ = ('name', 'expr', 'scope', 'kind', 'value', 'state', 'owner', 'module')

    def __init__(self, name, expr, scope, kind, owner=None, module=None, value=None, state=0):
        self.name, self.expr, self.scope, self.kind = name, expr, scope, kind
        self.owner, self.module = owner, module
        self.value, self.state = value, state     # state 0 lazy, 1 evaluating, 2 done


class Scope:
    __slots__ = ('vars', 'parent', 'caller', 'modules', 'functions', 'children', 'file', 'label')

    def __init__(self, parent=None, caller=None, file=None, label=''):
        self.vars = {}
        self.parent, self.caller = parent, caller
        self.modules = None
        self.functions = None
        self.children = None
        self.file = file if file is not None else (parent.file if parent is not None else None)
        self.label = label

    def binding(self, name):
        s = self
        if name.startswith('$'):
            while s is not None:
                b = s.vars.get(name)
                if b is not None:
                    return b
                s = s.caller if s.caller is not None else s.parent
            return None
        while s is not None:
            b = s.vars.get(name)
            if b is not None:
                return b
            s = s.parent
        return None

    def module(self, name):
        s = self
        while s is not None:
            if s.modules and name in s.modules:
                return s.modules[name]
            s = s.parent
        return None

    def function(self, name):
        s = self
        while s is not None:
            if s.functions and name in s.functions:
                return s.functions[name]
            s = s.parent
        return None

    def children_context(self):
        s = self
        while s is not None:
            if s.children is not None:
                return s.children
            s = s.parent
        return None


# ------------------------------------------------------------- trace records
class Frame:
    """One step of the instantiation path (linked to its parent)."""
    __slots__ = ('kind', 'node', 'scope', 'parent', 'inner', 'module', 'values', 'matrix', 'depth')

    def __init__(self, kind, node, scope, parent, inner=None, module=None, values=None, matrix=None):
        self.kind, self.node, self.scope, self.parent = kind, node, scope, parent
        self.inner, self.module, self.values, self.matrix = inner, module, values, matrix
        self.depth = parent.depth + 1 if parent is not None else 0

    def chain(self):
        out = []
        f = self
        while f is not None:
            out.append(f)
            f = f.parent
        out.reverse()
        return out

    @property
    def name(self):
        return getattr(self.node, 'name', self.kind)


class Leaf:
    __slots__ = ('kind', 'shape', 'matrix', 'box', 'frame', 'cutter', 'root', 'node', 'scope', '_inverse', 'color')

    def __init__(self, kind, shape, matrix, box, frame, cutter, root, node, scope, color):
        self.kind, self.shape, self.matrix, self.box = kind, shape, matrix, box
        self.frame, self.cutter, self.root, self.node, self.scope = frame, cutter, root, node, scope
        self.color = color
        self._inverse = None

    @property
    def inverse(self):
        if self._inverse is None:
            try:
                self._inverse = np.linalg.inv(self.matrix)
            except np.linalg.LinAlgError:
                self._inverse = np.eye(4)
        return self._inverse

    def world_box(self):
        if self.box is None:
            return None
        lo, hi = self.box
        corners = np.array([[x, y, z, 1.] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        world = corners @ self.matrix.T
        return world[:, :3].min(axis=0), world[:, :3].max(axis=0)

    def distance(self, point):
        """Approximate unsigned distance from a world point to this primitive's surface."""
        if self.box is None:
            return float('inf')
        local = (self.inverse @ np.r_[point, 1.])[:3]
        scale = abs(np.linalg.det(self.matrix[:3, :3])) ** (1 / 3) or 1.
        kind, shape = self.kind, self.shape
        if kind == 'sphere':
            d = abs(float(np.linalg.norm(local)) - shape['r'])
        elif kind == 'cylinder':
            r1, r2, h, z0 = shape['r1'], shape['r2'], shape['h'], shape['z0']
            cx, cy = shape.get('cx', 0.), shape.get('cy', 0.)
            z = local[2]
            t = min(1., max(0., (z - z0) / h)) if h > 0 else 0.
            radius = r1 + (r2 - r1) * t
            dr = math.hypot(local[0] - cx, local[1] - cy) - radius
            dz = max(z0 - z, z - (z0 + h))
            outside = math.hypot(max(dr, 0.), max(dz, 0.))
            d = abs(outside + min(max(dr, dz), 0.))
        else:
            lo, hi = self.box
            center = (np.asarray(lo) + np.asarray(hi)) / 2
            half = (np.asarray(hi) - np.asarray(lo)) / 2
            q = np.abs(local - center) - half
            outside = float(np.linalg.norm(np.maximum(q, 0.)))
            inside = min(float(np.max(q)), 0.)
            d = abs(outside + inside)
        return d * scale


@dataclass
class TraceResult:
    leaves: list = field(default_factory=list)
    calls: list = field(default_factory=list)      # module-call Frames (with world matrix)
    files: list = field(default_factory=list)      # [(Path or None, text)]
    roots: list = field(default_factory=list)      # top-level statements of the main file
    problems: list = field(default_factory=list)   # [(file_index, position, message)]
    partial: bool = False
    steps: int = 0

    def leaves_for_root(self, start, end):
        return [leaf for leaf in self.leaves if leaf.root is not None and leaf.root.file == 0
                and start <= leaf.root.start and leaf.root.end <= end]

    def boxes(self, root_span=None):
        """(leaves, lo[N,3], hi[N,3]) world boxes, cached per root."""
        cache = self.__dict__.setdefault('_box_cache', {})
        key = tuple(root_span) if root_span else None
        if key not in cache:
            leaves = self.leaves_for_root(*root_span) if root_span else list(self.leaves)
            kept, lows, highs = [], [], []
            for leaf in leaves:
                wb = leaf.world_box()
                if wb is None:
                    continue
                kept.append(leaf)
                lows.append(wb[0])
                highs.append(wb[1])
            lo = np.array(lows).reshape(-1, 3)
            hi = np.array(highs).reshape(-1, 3)
            cache[key] = (kept, lo, hi)
        return cache[key]

    def pick(self, point, root_span=None, tolerance=None):
        """The leaf whose surface is closest to a world-space hit point."""
        leaves, lo, hi = self.boxes(root_span)
        if not leaves:
            return None
        point = np.asarray(point, dtype=float)
        margin = np.maximum(1e-3, np.max(hi - lo, axis=1) * .02)[:, None]
        inside = np.all((point >= lo - margin) & (point <= hi + margin), axis=1)
        scored = []
        for index in np.nonzero(inside)[0]:
            leaf = leaves[index]
            scored.append((leaf.distance(point), index, leaf))
        if not scored:
            return None
        best_distance = min(d for d, _, _ in scored)
        # Among near-ties prefer the smaller (more specific) primitive.
        tie = tolerance if tolerance is not None else max(1e-4, best_distance * .25 + 1e-4)
        volume = np.prod(np.maximum(hi - lo, 1e-6), axis=1)
        near = [(d, index, leaf) for d, index, leaf in scored if d <= best_distance + tie]
        return min(near, key=lambda item: (round(item[0] / max(tie, 1e-9)), volume[item[1]]))[2]


# ------------------------------------------------------------------ helpers
def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _vec3(v, fill=0.):
    if _num(v):
        return [float(v)] * 3
    if isinstance(v, list):
        out = [float(x) if _num(x) else fill for x in v[:3]]
        while len(out) < 3:
            out.append(fill)
        return out
    return None


def rotation_matrix(a, v=None):
    m = np.eye(4)
    if _num(a):
        axis = _vec3(v) if v is not None else [0., 0., 1.]
        if axis is None or not any(axis):
            axis = [0., 0., 1.]
        axis = np.asarray(axis) / np.linalg.norm(axis)
        x, y, z = axis
        rad = math.radians(a)
        c, s = math.cos(rad), math.sin(rad)
        skew = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
        m[:3, :3] = np.eye(3) * c + (1 - c) * np.outer(axis, axis) + s * skew
        return m
    angles = _vec3(a) or [0., 0., 0.]
    rx, ry, rz = (math.radians(x) for x in angles)
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    X = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Y = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Z = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    m[:3, :3] = Z @ Y @ X
    return m


def truthy(v):
    if v is None or isinstance(v, Unknown):
        return False
    if isinstance(v, bool):
        return v
    if _num(v):
        return v != 0
    if isinstance(v, (str, list)):
        return len(v) > 0
    return True


def default_library_paths():
    paths = []
    env = os.environ.get('OPENSCADPATH')
    if env:
        paths += [p for p in env.split(os.pathsep) if p]
    home = Path.home()
    paths += [home / '.local/share/OpenSCAD/libraries', home / 'Documents/OpenSCAD/libraries',
              Path('/usr/share/openscad/libraries'), Path('/usr/local/share/openscad/libraries'),
              Path(r'C:\Program Files\OpenSCAD\libraries'), Path('/Applications/OpenSCAD.app/Contents/Resources/libraries')]
    return [Path(p) for p in paths]


# ------------------------------------------------------------------- tracer
class Tracer:
    def __init__(self, loader=None, library_paths=None, max_steps=4_000_000, max_leaves=150_000, t=0.0,
                 overrides=None, cancel=None):
        self.loader = loader or (lambda path: Path(path).read_text(encoding='utf-8-sig'))
        self.library_paths = list(library_paths) if library_paths is not None else default_library_paths()
        self.max_steps, self.max_leaves = max_steps, max_leaves
        self.t = t
        self.overrides = overrides or {}
        self.cancel = cancel
        self.result = TraceResult()
        self.steps = 0
        self.file_index = {}
        self.file_asts = []
        self.use_scopes = {}
        self.depth = 0
        self.root = None
        self.cutter_depth = 0

    # ------------------------------------------------------------- files
    def add_file(self, path, text):
        key = str(Path(path).resolve()) if path is not None else f'<memory:{len(self.result.files)}>'
        if key in self.file_index:
            return self.file_index[key]
        index = len(self.result.files)
        self.file_index[key] = index
        self.result.files.append((Path(path) if path is not None else None, text))
        try:
            ast = lang.parse(text, index)
        except lang.ParseError as error:
            self.result.problems.append((index, error.position, str(error)))
            ast = []
        self.file_asts.append(ast)
        return index

    def resolve_include(self, name, from_file):
        base = self.result.files[from_file][0]
        candidates = []
        if base is not None:
            candidates.append(base.parent / name)
        else:
            candidates.append(Path.cwd() / name)
        candidates += [p / name for p in self.library_paths]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None

    def load(self, name, from_file, position):
        path = self.resolve_include(name, from_file)
        if path is None:
            self.result.problems.append((from_file, position, f"Can't open include/library file '{name}'"))
            return None
        try:
            text = self.loader(path)
        except (OSError, UnicodeError) as error:
            self.result.problems.append((from_file, position, f'{name}: {error}'))
            return None
        return self.add_file(path, text)

    # --------------------------------------------------------------- run
    def run(self, path, text):
        main = self.add_file(path, text)
        self.result.tracer = self
        scope = Scope(file=main, label='file')
        scope.vars['$t'] = Binding('$t', None, scope, 'special', value=float(self.t), state=2)
        for name, value in (('$fn', 0.), ('$fa', 12.), ('$fs', 2.), ('$preview', False), ('$children', 0.)):
            scope.vars[name] = Binding(name, None, scope, 'special', value=value, state=2)
        for name, value in (('PI', math.pi),):
            scope.vars[name] = Binding(name, None, scope, 'special', value=value, state=2)
        statements = self.collect(self.file_asts[main], scope, set())
        for name, expr in self.overrides.items():
            b = scope.vars.get(name)
            if b is not None:
                b.value, b.state = expr, 2
        roots = [s for s in statements if not isinstance(s, (Assign, ModuleDef, FunctionDef, Include, Empty))]
        self.result.roots = [s for s in roots if s.file == main]
        identity = np.eye(4)
        try:
            for stmt in roots:
                self.root = stmt if stmt.file == main else self.root_for_included(stmt)
                self.exec_stmt(stmt, scope, identity, None, None, False)
        except Budget:
            self.result.partial = True
        except RecursionError:
            self.result.partial = True
            self.result.problems.append((main, None, 'Recursion too deep while tracing'))
        self.result.steps = self.steps
        return self.result

    def root_for_included(self, stmt):
        return stmt

    def collect(self, statements, scope, seen):
        """Register definitions in ``scope``; return statements with includes inlined."""
        out = []
        for stmt in statements:
            if isinstance(stmt, Assign):
                previous = scope.vars.get(stmt.name)
                eval_scope = scope
                if previous is not None and previous.kind != 'assign':
                    # `l = f(l)` inside a module: the right-hand side sees the
                    # parameter (or outer special variable), not itself.
                    eval_scope = Scope(parent=scope.parent, caller=scope.caller, file=scope.file, label=scope.label)
                    eval_scope.vars = dict(scope.vars)
                    eval_scope.modules, eval_scope.functions, eval_scope.children = scope.modules, scope.functions, scope.children
                scope.vars[stmt.name] = Binding(stmt.name, stmt.value, eval_scope, 'assign', owner=stmt)
                out.append(stmt)
            elif isinstance(stmt, ModuleDef):
                if scope.modules is None:
                    scope.modules = {}
                scope.modules[stmt.name] = (stmt, scope)
            elif isinstance(stmt, FunctionDef):
                if scope.functions is None:
                    scope.functions = {}
                scope.functions[stmt.name] = (stmt, scope)
            elif isinstance(stmt, Include):
                index = self.load(stmt.path, stmt.file, stmt.start)
                if index is None:
                    continue
                if stmt.use:
                    used = self.use_scope(index)
                    if used.modules:
                        if scope.modules is None:
                            scope.modules = {}
                        for name, value in used.modules.items():
                            scope.modules.setdefault(name, value)
                    if used.functions:
                        if scope.functions is None:
                            scope.functions = {}
                        for name, value in used.functions.items():
                            scope.functions.setdefault(name, value)
                else:
                    if index in seen:
                        continue
                    out.extend(self.collect(self.file_asts[index], scope, seen | {index}))
            else:
                out.append(stmt)
        return out

    def use_scope(self, index):
        if index in self.use_scopes:
            return self.use_scopes[index]
        scope = Scope(file=index, label='use')
        self.use_scopes[index] = scope
        for name, value in (('$fn', 0.), ('$fa', 12.), ('$fs', 2.), ('$t', float(self.t)), ('PI', math.pi), ('$preview', False)):
            scope.vars[name] = Binding(name, None, scope, 'special', value=value, state=2)
        self.collect(self.file_asts[index], scope, {index})
        return scope

    # --------------------------------------------------------- statements
    def tick(self):
        self.steps += 1
        if self.steps > self.max_steps:
            raise Budget('step budget')
        if self.cancel is not None and (self.steps & 4095) == 0 and self.cancel.is_set():
            raise Budget('cancelled')

    def body_scope(self, statements, parent, label=''):
        """A child scope for a statement list that defines names; else the parent."""
        if not any(isinstance(s, (Assign, ModuleDef, FunctionDef)) for s in statements):
            return parent, statements
        scope = Scope(parent=parent, label=label)
        return scope, self.collect(statements, scope, set())

    def exec_list(self, statements, scope, matrix, frame, color, cutter, extrude=None, only=None):
        scope, statements = self.body_scope(statements, scope)
        geometry = [s for s in statements if not isinstance(s, (Assign, ModuleDef, FunctionDef, Include, Empty))]
        for index, stmt in enumerate(geometry):
            if only is not None and index not in only:
                continue
            self.exec_stmt(stmt, scope, matrix, frame, color, cutter, extrude)

    def exec_stmt(self, stmt, scope, matrix, frame, color, cutter, extrude=None):
        self.tick()
        if isinstance(stmt, Block):
            self.exec_list(stmt.body, scope, matrix, frame, color, cutter, extrude)
            return
        if isinstance(stmt, If):
            cond = self.eval(stmt.cond, scope)
            branch = stmt.then if truthy(cond) else (stmt.other or [])
            child = Frame('if', stmt, scope, frame, values={'condition': cond}, matrix=matrix)
            self.exec_list(branch, scope, matrix, child, color, cutter, extrude)
            return
        if not isinstance(stmt, Instance):
            return
        if '*' in stmt.modifier or '%' in stmt.modifier:
            return
        name = stmt.name
        if name in ('for', 'intersection_for'):
            self.exec_for(stmt, scope, matrix, frame, color, cutter, extrude)
            return
        if name == 'let':
            inner = Scope(parent=scope, label='let')
            for arg in stmt.args:
                if arg.name:
                    inner.vars[arg.name] = Binding(arg.name, arg.value, scope if False else inner, 'let', owner=stmt)
            child = Frame('let', stmt, inner, frame, matrix=matrix)
            self.exec_list(stmt.children, inner, matrix, child, color, cutter, extrude)
            return
        if name in ('echo', 'assert'):
            if name == 'assert' and stmt.args:
                cond = self.eval(stmt.args[0].value, scope)
                if cond is False or (cond is not UNKNOWN and not truthy(cond) and cond is not None):
                    self.result.problems.append((stmt.file, stmt.start, 'assert() failed while tracing'))
            self.exec_list(stmt.children, scope, matrix, frame, color, cutter, extrude)
            return
        if name == 'children':
            self.exec_children(stmt, scope, matrix, frame, color, cutter, extrude)
            return
        if name in BUILTIN_PARAMS and scope.module(name) is None:
            self.exec_builtin(stmt, scope, matrix, frame, color, cutter, extrude)
            return
        found = scope.module(name)
        if found is None:
            self.result.problems.append((stmt.file, stmt.start, f"Unknown module '{name}'"))
            return
        definition, def_scope = found
        self.depth += 1
        if self.depth > 300:
            self.depth -= 1
            raise Budget('module recursion')
        try:
            inner = Scope(parent=def_scope, caller=scope, label='module ' + name)
            self.bind(definition.params, stmt.args, inner, scope, stmt, definition)
            children = [c for c in stmt.children if not isinstance(c, (Assign, ModuleDef, FunctionDef, Include, Empty))]
            inner.vars['$children'] = Binding('$children', None, inner, 'special', value=float(len(children)), state=2)
            inner.children = (stmt.children, scope, frame)
            params = {p.name: inner.vars[p.name] for p in definition.params if p.name in inner.vars}
            call = Frame('module', stmt, scope, frame, inner=inner, module=definition, matrix=matrix, values=params)
            self.result.calls.append(call)
            body = definition.body
            if body is None:
                return
            statements = body.body if isinstance(body, Block) else [body]
            statements = self.collect(statements, inner, set())
            for s in statements:
                if not isinstance(s, (Assign, ModuleDef, FunctionDef, Include, Empty)):
                    self.exec_stmt(s, inner, matrix, call, color, cutter, extrude)
        finally:
            self.depth -= 1

    def bind(self, params, args, inner, caller, call_node, definition):
        names = [p.name for p in params]
        positional = [a for a in args if a.name is None]
        named = {a.name: a for a in args if a.name is not None}
        for index, param in enumerate(params):
            arg = named.get(param.name)
            if arg is None and index < len(positional):
                arg = positional[index]
            if arg is not None:
                inner.vars[param.name] = Binding(param.name, arg.value, caller, 'arg', owner=call_node, module=definition)
            elif param.default is not None:
                inner.vars[param.name] = Binding(param.name, param.default, inner, 'default', owner=param, module=definition)
            else:
                inner.vars[param.name] = Binding(param.name, None, inner, 'missing', owner=call_node, module=definition, value=None, state=2)
        for name, arg in named.items():
            if name not in names and name.startswith('$'):
                inner.vars[name] = Binding(name, arg.value, caller, 'arg', owner=call_node, module=definition)

    def exec_children(self, stmt, scope, matrix, frame, color, cutter, extrude):
        context = scope.children_context()
        if context is None:
            return
        statements, caller_scope, caller_frame = context
        only = None
        if stmt.args:
            value = self.eval(stmt.args[0].value, scope)
            if _num(value):
                only = {int(value)}
            elif isinstance(value, list):
                only = {int(v) for v in value if _num(v)}
            elif isinstance(value, RangeValue):
                only = {int(v) for v in value.values()}
        # Children run in the caller's scope but under the module's transforms;
        # $-variables set by the module are visible to them (dynamic scope).
        child_scope = Scope(parent=caller_scope, caller=scope, label='children')
        marker = Frame('children', stmt, scope, frame, matrix=matrix)
        saved = self.depth
        self.exec_list(statements, child_scope, matrix, marker, color, cutter, extrude, only)
        self.depth = saved

    def exec_for(self, stmt, scope, matrix, frame, color, cutter, extrude):
        assignments = [(a.name, a.value) for a in stmt.args if a.name]

        def loop(index, current):
            if index == len(assignments):
                child = Frame('for', stmt, current, frame,
                              values={n: current.binding(n).value for n, _ in assignments}, matrix=matrix)
                self.exec_list(stmt.children, current, matrix, child, color, cutter, extrude)
                return
            name, expr = assignments[index]
            values = self.iterate(self.eval(expr, current))
            for value in values:
                self.tick()
                inner = Scope(parent=current, label='for')
                inner.vars[name] = Binding(name, expr, current, 'loop', owner=stmt, value=value, state=2)
                loop(index + 1, inner)
        loop(0, scope)

    def iterate(self, value):
        if isinstance(value, RangeValue):
            return value.iter_values()
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            return list(value)
        if value is None or isinstance(value, Unknown):
            return []
        return [value]

    def builtin_args(self, stmt, scope):
        names = BUILTIN_PARAMS.get(stmt.name, [])
        values = {}
        position = 0
        for arg in stmt.args:
            if arg.name is None:
                if position < len(names):
                    values[names[position]] = self.eval(arg.value, scope)
                position += 1
            else:
                values[arg.name] = self.eval(arg.value, scope)
        return values

    def exec_builtin(self, stmt, scope, matrix, frame, color, cutter, extrude):
        name = stmt.name
        values = self.builtin_args(stmt, scope)
        # $fn etc passed as named args are visible to children (dynamic scope)
        special = {k: v for k, v in values.items() if k.startswith('$')}
        if special:
            inner = Scope(parent=scope, caller=scope, label=name)
            for arg in stmt.args:
                if arg.name and arg.name.startswith('$'):
                    inner.vars[arg.name] = Binding(arg.name, arg.value, scope, 'arg', owner=stmt)
            scope = inner
        if name in PRIMITIVES_3D or name in PRIMITIVES_2D:
            self.add_leaf(stmt, scope, values, matrix, frame, color, cutter, extrude)
            return
        local = None
        if name == 'translate':
            v = _vec3(values.get('v'))
            if v is not None:
                local = np.eye(4)
                local[:3, 3] = v
        elif name == 'rotate':
            local = rotation_matrix(values.get('a', 0.) if values.get('a') is not None else 0., values.get('v'))
        elif name == 'scale':
            v = _vec3(values.get('v'), 1.)
            if v is not None:
                local = np.diag(v + [1.])
        elif name == 'mirror':
            v = _vec3(values.get('v'))
            if v is not None and any(v):
                n = np.asarray(v) / np.linalg.norm(v)
                local = np.eye(4)
                local[:3, :3] = np.eye(3) - 2 * np.outer(n, n)
        elif name == 'multmatrix':
            m = values.get('m')
            try:
                rows = np.asarray(m, dtype=float)
                local = np.eye(4)
                local[:rows.shape[0], :rows.shape[1]] = rows[:4, :4]
            except (TypeError, ValueError):
                local = None
        frame = Frame('builtin', stmt, scope, frame, values=values, matrix=matrix)
        if local is not None:
            matrix = matrix @ local
        if name == 'color':
            color = frame if color is None else color          # outermost color wins
        if name == 'difference':
            children = [c for c in stmt.children if not isinstance(c, (Assign, ModuleDef, FunctionDef, Include, Empty))]
            inner_scope, statements = self.body_scope(stmt.children, scope)
            geometry = [s for s in statements if not isinstance(s, (Assign, ModuleDef, FunctionDef, Include, Empty))]
            for index, child in enumerate(geometry):
                self.exec_stmt(child, inner_scope, matrix, frame, color, cutter or index > 0, extrude)
            return
        if name == 'linear_extrude':
            h = values.get('height', 100.)
            h = float(h) if _num(h) else 100.
            center = truthy(values.get('center'))
            grow = values.get('scale', 1.)
            grow = max(1., max(_vec3(grow, 1.)[:2])) if grow is not None and (_num(grow) or isinstance(grow, list)) else 1.
            extrude = ('linear', matrix, (-h / 2, h / 2) if center else (0., h), grow)
            self.exec_list(stmt.children, scope, np.eye(4), frame, color, cutter, extrude)
            return
        if name == 'rotate_extrude':
            extrude = ('rotate', matrix, None, 1.)
            self.exec_list(stmt.children, scope, np.eye(4), frame, color, cutter, extrude)
            return
        if name == 'offset' and extrude is not None:
            pass
        self.exec_list(stmt.children, scope, matrix, frame, color, cutter, extrude)

    def add_leaf(self, stmt, scope, values, matrix, frame, color, cutter, extrude):
        if len(self.result.leaves) >= self.max_leaves:
            raise Budget('leaf budget')
        name = stmt.name
        kind, shape, box = 'box', {}, None

        def f(key, default):
            v = values.get(key)
            return float(v) if _num(v) else default
        if name == 'cube':
            size = _vec3(values.get('size', 1.), 1.) or [1., 1., 1.]
            if truthy(values.get('center')):
                box = ([-s / 2 for s in size], [s / 2 for s in size])
            else:
                box = ([0., 0., 0.], size)
        elif name == 'sphere':
            r = f('d', None) / 2 if f('d', None) is not None else f('r', 1.)
            kind, shape, box = 'sphere', {'r': r}, ([-r] * 3, [r] * 3)
        elif name == 'cylinder':
            h = f('h', 1.)
            r = f('d', None) / 2 if f('d', None) is not None else f('r', 1.)
            r1 = f('d1', None) / 2 if f('d1', None) is not None else f('r1', r)
            r2 = f('d2', None) / 2 if f('d2', None) is not None else f('r2', r)
            z0 = -h / 2 if truthy(values.get('center')) else 0.
            rm = max(r1, r2)
            kind, shape, box = 'cylinder', {'r1': r1, 'r2': r2, 'h': h, 'z0': z0}, ([-rm, -rm, z0], [rm, rm, z0 + h])
        elif name == 'polyhedron':
            points = values.get('points')
            try:
                arr = np.asarray(points, dtype=float)[:, :3]
                box = (arr.min(axis=0).tolist(), arr.max(axis=0).tolist())
            except (TypeError, ValueError, IndexError):
                box = None
        elif name == 'square':
            size = values.get('size', 1.)
            sx, sy = (float(size), float(size)) if _num(size) else (
                (float(size[0]), float(size[1])) if isinstance(size, list) and len(size) >= 2 and _num(size[0]) and _num(size[1]) else (1., 1.))
            box = ([-sx / 2, -sy / 2, 0.], [sx / 2, sy / 2, 0.]) if truthy(values.get('center')) else ([0., 0., 0.], [sx, sy, 0.])
        elif name == 'circle':
            r = f('d', None) / 2 if f('d', None) is not None else f('r', 1.)
            kind, shape, box = 'circle', {'r': r}, ([-r, -r, 0.], [r, r, 0.])
        elif name == 'polygon':
            try:
                arr = np.asarray(values.get('points'), dtype=float)[:, :2]
                box = (arr.min(axis=0).tolist() + [0.], arr.max(axis=0).tolist() + [0.])
            except (TypeError, ValueError, IndexError):
                box = None
        elif name == 'text':
            text = values.get('text', '')
            size = f('size', 10.)
            width = .62 * size * len(str(text)) if text is not None else 0.
            halign, valign = values.get('halign', 'left'), values.get('valign', 'baseline')
            x0 = -width / 2 if halign == 'center' else -width if halign == 'right' else 0.
            y0 = -size / 2 if valign == 'center' else -size if valign == 'top' else (-.2 * size if valign == 'baseline' else 0.)
            box = ([x0, y0, 0.], [x0 + width, y0 + size, 0.])
        else:
            box = None     # import/surface: geometry only known to OpenSCAD
        if extrude is not None and box is not None:
            mode, outer, zr, grow = extrude
            lo, hi = np.asarray(box[0], dtype=float), np.asarray(box[1], dtype=float)
            corners = np.array([[x, y, 0., 1.] for x in (lo[0], hi[0]) for y in (lo[1], hi[1])]) @ matrix.T
            xy_lo, xy_hi = corners[:, :2].min(axis=0), corners[:, :2].max(axis=0)
            if mode == 'linear':
                if grow > 1:
                    c = (xy_lo + xy_hi) / 2
                    xy_lo, xy_hi = c + (xy_lo - c) * grow, c + (xy_hi - c) * grow
                pure = np.allclose(matrix[:3, :3], np.eye(3) * matrix[0, 0]) and kind == 'circle'
                if pure:
                    center = matrix[:2, 3]
                    r = shape['r'] * matrix[0, 0]
                    kind, shape = 'cylinder', {'r1': r, 'r2': r * grow, 'h': zr[1] - zr[0], 'z0': zr[0],
                                               'cx': float(center[0]), 'cy': float(center[1])}
                else:
                    kind, shape = 'box', {}
                box = ([xy_lo[0], xy_lo[1], zr[0]], [xy_hi[0], xy_hi[1], zr[1]])
            else:
                radius = max(abs(xy_lo[0]), abs(xy_hi[0]))
                kind, shape = 'box', {}
                box = ([-radius, -radius, xy_lo[1]], [radius, radius, xy_hi[1]])
            matrix = outer
        elif box is not None and kind == 'circle':
            kind = 'box'
        leaf_frame = Frame('builtin', stmt, scope, frame, values=values, matrix=matrix)
        self.result.leaves.append(Leaf(kind, shape, matrix, box, leaf_frame, cutter, self.root, stmt, scope, color))

    # -------------------------------------------------------- expressions
    def value_of(self, binding):
        if binding.state == 2:
            return binding.value
        if binding.state == 1:
            return UNKNOWN
        binding.state = 1
        try:
            value = self.eval(binding.expr, binding.scope) if binding.expr is not None else None
        finally:
            binding.state = 2
        binding.value = value
        return value

    def lookup(self, name, scope):
        binding = scope.binding(name)
        if binding is None:
            return None
        return self.value_of(binding)

    def eval(self, e, scope):
        self.tick()
        t = type(e)
        if t is Num:
            return e.value
        if t is Str:
            return e.value
        if t is Bool:
            return e.value
        if t is Undef:
            return None
        if t is Id:
            binding = scope.binding(e.name)
            if binding is None:
                found = scope.function(e.name)
                if found is not None:
                    definition, def_scope = found
                    return FunctionValue(definition.params, definition.body, def_scope, e.name)
                return None
            return self.value_of(binding)
        if t is Vector:
            out = []
            for item in e.items:
                self.element(item, scope, out)
            return out
        if t is Range:
            b = self.eval(e.begin, scope)
            s = self.eval(e.step, scope) if e.step is not None else 1.
            stop = self.eval(e.stop, scope)
            return RangeValue(b, s, stop)
        if t is Unary:
            v = self.eval(e.operand, scope)
            if e.op == '!':
                return not truthy(v)
            if e.op == '-':
                if _num(v):
                    return -v
                if isinstance(v, list):
                    return [(-x if _num(x) else None) for x in v]
                return None
            return v if _num(v) or isinstance(v, list) else None
        if t is Binary:
            if e.op == '&&':
                return truthy(self.eval(e.left, scope)) and truthy(self.eval(e.right, scope))
            if e.op == '||':
                return truthy(self.eval(e.left, scope)) or truthy(self.eval(e.right, scope))
            return binary(e.op, self.eval(e.left, scope), self.eval(e.right, scope))
        if t is Ternary:
            return self.eval(e.then if truthy(self.eval(e.cond, scope)) else e.other, scope)
        if t is Index:
            value = self.eval(e.value, scope)
            index = self.eval(e.index, scope)
            if isinstance(value, (list, str)) and _num(index):
                i = int(index)
                return value[i] if 0 <= i < len(value) else None
            if isinstance(value, RangeValue) and _num(index):
                values = value.values(limit=1000000)
                i = int(index)
                return values[i] if 0 <= i < len(values) else None
            return None
        if t is Member:
            value = self.eval(e.value, scope)
            index = {'x': 0, 'y': 1, 'z': 2}.get(e.name)
            if isinstance(value, list) and index is not None and index < len(value):
                return value[index]
            return None
        if t is Call:
            return self.call(e, scope)
        if t is Let:
            inner = Scope(parent=scope, label='let')
            for a in e.assignments:
                inner.vars[a.name] = Binding(a.name, a.value, inner, 'let', owner=a)
                self.value_of(inner.vars[a.name])
            return self.eval(e.body, inner)
        if t in (AssertExpr, EchoExpr):
            return self.eval(e.body, scope) if e.body is not None else None
        if t is FunctionLiteral:
            return FunctionValue(e.params, e.body, scope)
        return None

    def element(self, item, scope, out):
        t = type(item)
        if t is CompFor:
            def loop(index, current):
                if index == len(item.assignments):
                    self.element(item.body, current, out)
                    return
                a = item.assignments[index]
                for value in self.iterate(self.eval(a.value, current)):
                    self.tick()
                    inner = Scope(parent=current, label='for')
                    inner.vars[a.name] = Binding(a.name, a.value, current, 'loop', value=value, state=2)
                    loop(index + 1, inner)
            loop(0, scope)
        elif t is CompForC:
            current = Scope(parent=scope, label='for')
            for a in item.init:
                current.vars[a.name] = Binding(a.name, a.value, scope, 'loop', value=self.eval(a.value, scope), state=2)
            guard = 0
            while truthy(self.eval(item.cond, current)):
                guard += 1
                if guard > 100000:
                    raise Budget('loop')
                self.element(item.body, current, out)
                nxt = Scope(parent=scope, label='for')
                for a in item.update:
                    nxt.vars[a.name] = Binding(a.name, a.value, current, 'loop', value=self.eval(a.value, current), state=2)
                for name, b in current.vars.items():
                    nxt.vars.setdefault(name, b)
                current = nxt
        elif t is CompIf:
            if truthy(self.eval(item.cond, scope)):
                self.element(item.then, scope, out)
            elif item.other is not None:
                self.element(item.other, scope, out)
        elif t is CompLet:
            inner = Scope(parent=scope, label='let')
            for a in item.assignments:
                inner.vars[a.name] = Binding(a.name, a.value, inner, 'let', owner=a)
            self.element(item.body, inner, out)
        elif t is Each:
            value = self.eval(item.value, scope) if not isinstance(item.value, (CompFor, CompIf, CompLet, CompForC)) else None
            if isinstance(item.value, (CompFor, CompIf, CompLet, CompForC)):
                inner = []
                self.element(item.value, scope, inner)
                for v in inner:
                    out.extend(self.iterate(v))
            else:
                out.extend(self.iterate(value))
        else:
            out.append(self.eval(item, scope))

    def call(self, e, scope):
        func = e.func
        if type(func) is Id:
            binding = scope.binding(func.name)
            target = None
            if binding is not None:
                value = self.value_of(binding)
                if isinstance(value, FunctionValue):
                    target = value
            if target is None:
                found = scope.function(func.name)
                if found is not None:
                    definition, def_scope = found
                    target = FunctionValue(definition.params, definition.body, def_scope, func.name)
            if target is None:
                builtin = BUILTIN_FUNCTIONS.get(func.name)
                if builtin is None:
                    return None
                args = [self.eval(a.value, scope) for a in e.args if a.name is None]
                named = [a for a in e.args if a.name is not None]
                if named:
                    names = BUILTIN_FUNCTION_PARAMS.get(func.name, [])
                    for a in named:
                        if a.name in names:
                            index = names.index(a.name)
                            while len(args) <= index:
                                args.append(None)
                            args[index] = self.eval(a.value, scope)
                try:
                    return builtin(*args)
                except (TypeError, ValueError, ZeroDivisionError, OverflowError, IndexError, AttributeError):
                    return None
        else:
            target = self.eval(func, scope)
            if not isinstance(target, FunctionValue):
                return None
        self.depth += 1
        if self.depth > 2000:
            self.depth -= 1
            raise Budget('function recursion')
        try:
            inner = Scope(parent=target.scope, caller=scope, label='function')
            positional = [a for a in e.args if a.name is None]
            named = {a.name: a for a in e.args if a.name}
            for index, param in enumerate(target.params):
                arg = named.get(param.name) or (positional[index] if index < len(positional) else None)
                if arg is not None:
                    inner.vars[param.name] = Binding(param.name, None, scope, 'arg', value=self.eval(arg.value, scope), state=2)
                elif param.default is not None:
                    inner.vars[param.name] = Binding(param.name, param.default, inner, 'default')
                else:
                    inner.vars[param.name] = Binding(param.name, None, inner, 'missing', value=None, state=2)
            return self.eval(target.body, inner)
        finally:
            self.depth -= 1


def binary(op, a, b):
    try:
        if op in ('+', '-'):
            if _num(a) and _num(b):
                return a + b if op == '+' else a - b
            if isinstance(a, list) and isinstance(b, list):
                n = min(len(a), len(b))
                return [binary(op, a[i], b[i]) for i in range(n)]
            return None
        if op == '*':
            if _num(a) and _num(b):
                return a * b
            if _num(a) and isinstance(b, list):
                return [binary('*', a, x) for x in b]
            if isinstance(a, list) and _num(b):
                return [binary('*', x, b) for x in a]
            if isinstance(a, list) and isinstance(b, list):
                arr_a, arr_b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
                if arr_a.ndim == 1 and arr_b.ndim == 1:
                    return float(arr_a @ arr_b)
                result = arr_a @ arr_b
                return result.tolist() if isinstance(result, np.ndarray) else float(result)
            return None
        if op == '/':
            if _num(a) and _num(b):
                return a / b if b else (math.copysign(math.inf, a) if a else math.nan)
            if isinstance(a, list) and _num(b):
                return [binary('/', x, b) for x in a]
            if _num(a) and isinstance(b, list):
                return [binary('/', a, x) for x in b]
            return None
        if op == '%':
            if _num(a) and _num(b):
                return math.fmod(a, b) if b else math.nan
            return None
        if op == '^':
            return a ** b if _num(a) and _num(b) else None
        if op == '==':
            return a == b
        if op == '!=':
            return a != b
        if op in ('<', '<=', '>', '>='):
            if (_num(a) and _num(b)) or (isinstance(a, str) and isinstance(b, str)):
                return {'<': a < b, '<=': a <= b, '>': a > b, '>=': a >= b}[op]
            return False
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def _deg(f):
    return lambda x: math.degrees(f(x))


def _str(*values):
    return ''.join(v if isinstance(v, str) else fmt(v) for v in values)


def _len(v):
    if isinstance(v, (list, str)):
        return float(len(v))
    if isinstance(v, RangeValue):
        return float(len(v.values()))
    return None


def _concat(*values):
    out = []
    for v in values:
        if isinstance(v, list):
            out.extend(v)
        else:
            out.append(v)
    return out


def _lookup(key, table):
    pairs = [p for p in table if isinstance(p, list) and len(p) >= 2 and _num(p[0])]
    if not pairs or not _num(key):
        return None
    pairs.sort(key=lambda p: p[0])
    if key <= pairs[0][0]:
        return pairs[0][1]
    if key >= pairs[-1][0]:
        return pairs[-1][1]
    for (k0, v0), (k1, v1) in zip(pairs, pairs[1:]):
        if k0 <= key <= k1:
            t = (key - k0) / (k1 - k0) if k1 != k0 else 0
            return v0 + (v1 - v0) * t
    return None


def _minmax(f):
    def inner(*values):
        if len(values) == 1 and isinstance(values[0], list):
            values = values[0]
        nums = [v for v in values if _num(v)]
        return float(f(nums)) if nums else None
    return inner


def _norm(v):
    return float(np.linalg.norm(np.asarray(v, dtype=float))) if isinstance(v, list) else None


def _cross(a, b):
    return np.cross(np.asarray(a, dtype=float), np.asarray(b, dtype=float)).tolist()


def _search(match, target, num=1, col=0):
    """OpenSCAD search(match_value, string_or_vector, num_returns_per_match, index_col_num)."""
    num = int(num) if _num(num) else 1
    col = int(col) if _num(col) else None     # undef: compare whole entries
    if not isinstance(target, (list, str)):
        return []

    def key(entry):
        if col is None:
            return entry
        if isinstance(entry, list):
            return entry[col] if col < len(entry) else None
        return entry if col == 0 else None

    def hits(value):
        if isinstance(target, str):
            return [float(i) for i, ch in enumerate(target) if ch == value]
        return [float(i) for i, entry in enumerate(target) if key(entry) == value]

    def limit(found):
        return found if num == 0 else found[:num]
    if _num(match) or isinstance(match, bool):
        return limit(hits(match))
    if isinstance(match, str):
        out = []
        for ch in match:
            found = hits(ch)
            if num == 1:
                if found:
                    out.append(found[0])
            else:
                out.append(limit(found))
        return out
    if isinstance(match, list):
        out = []
        for value in match:
            found = hits(value)
            if num == 1:
                out.append(found[0] if found else [])
            else:
                out.append(limit(found))
        return out
    return []


BUILTIN_FUNCTION_PARAMS = {
    'search': ['match_value', 'string_or_vector', 'num_returns_per_match', 'index_col_num'],
    'rands': ['min_value', 'max_value', 'value_count', 'seed_value'],
    'lookup': ['key', 'table'], 'atan2': ['y', 'x'], 'pow': ['base', 'exponent'], 'log': ['x', 'b'],
}


BUILTIN_FUNCTIONS = {
    'abs': abs, 'sign': lambda x: float((x > 0) - (x < 0)), 'sin': lambda x: math.sin(math.radians(x)),
    'cos': lambda x: math.cos(math.radians(x)), 'tan': lambda x: math.tan(math.radians(x)),
    'asin': _deg(math.asin), 'acos': _deg(math.acos), 'atan': _deg(math.atan),
    'atan2': lambda y, x: math.degrees(math.atan2(y, x)), 'floor': lambda x: float(math.floor(x)),
    'ceil': lambda x: float(math.ceil(x)), 'round': lambda x: float(math.floor(x + .5)) if x >= 0 else -float(math.floor(-x + .5)),
    'ln': math.log, 'log': lambda x, b=None: math.log10(x) if b is None else math.log(x, b), 'exp': math.exp,
    'pow': lambda a, b: a ** b, 'sqrt': math.sqrt, 'min': _minmax(min), 'max': _minmax(max), 'norm': _norm,
    'cross': _cross, 'len': _len, 'concat': _concat, 'lookup': _lookup, 'str': _str, 'chr': lambda *c: ''.join(chr(int(x)) for x in c),
    'ord': lambda s: float(ord(s)) if isinstance(s, str) and len(s) == 1 else None,
    'search': _search, 'version': lambda: [2021., 1., 0.], 'version_num': lambda: 20210100.,
    'is_undef': lambda v: v is None or isinstance(v, Unknown), 'is_num': lambda v: _num(v) and not math.isnan(v),
    'is_string': lambda v: isinstance(v, str), 'is_bool': lambda v: isinstance(v, bool),
    'is_list': lambda v: isinstance(v, list), 'is_function': lambda v: isinstance(v, FunctionValue),
    'rands': lambda lo, hi, n, seed=0: list(np.random.default_rng(int(seed)).uniform(lo, hi, int(n))),
}


def trace(path, text, loader=None, library_paths=None, t=0.0, max_steps=4_000_000, max_leaves=150_000, cancel=None):
    """Run a trace on a large stack (deeply recursive libraries such as BOSL2)."""
    result = {}

    def target():
        old = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(max(old, 30000))
            result['value'] = Tracer(loader, library_paths, max_steps, max_leaves, t, cancel=cancel).run(path, text)
        except Exception as error:   # keep the UI alive; report as a problem
            r = TraceResult(partial=True)
            r.problems.append((0, None, f'Tracer failed: {error}'))
            result['value'] = r
        finally:
            sys.setrecursionlimit(old)
    size = threading.stack_size()
    try:
        threading.stack_size(256 * 1024 * 1024)
        worker = threading.Thread(target=target, name='scad-trace', daemon=True)
        worker.start()
    finally:
        threading.stack_size(size)
    worker.join()
    return result['value']
