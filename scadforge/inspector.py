"""Explain a picked primitive: its call chain, parameters, colors and variables,
and compute source edits that change them — wherever they are written.
"""
from dataclasses import dataclass, field
from pathlib import Path
import re
from . import lang
from .lang import Id, Vector, Str, Num, Instance, If, is_literal
from .trace import BUILTIN_PARAMS, BUILTIN_DEFAULTS, fmt, Leaf, UNKNOWN, Unknown

COLOR_NAMES = None


@dataclass
class EditTarget:
    file: int
    start: int
    end: int
    kind: str            # literal | expression | insert
    label: str
    insert_name: str = ''

    def describe(self, files):
        path = files[self.file][0]
        text = files[self.file][1]
        line = text.count('\n', 0, self.start) + 1
        return f'{self.label} · {path.name if path else "untitled"}:{line}'


@dataclass
class Row:
    group: str
    name: str
    value: object
    expression: str
    origin: str
    targets: list = field(default_factory=list)
    is_color: bool = False
    editable: bool = True
    note: str = ''
    frame_file: int = None
    frame_pos: int = None

    @property
    def kind(self):
        v = self.value
        if self.is_color:
            return 'color'
        if isinstance(v, bool):
            return 'bool'
        if isinstance(v, (int, float)):
            return 'number'
        if isinstance(v, str):
            return 'string'
        if isinstance(v, list) and v and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            return 'vector'
        return 'other'


@dataclass
class Group:
    title: str
    file: int
    position: int
    rows: list = field(default_factory=list)
    kind: str = ''
    library: bool = False


def location(files, file, position):
    path, text = files[file]
    line = text.count('\n', 0, position or 0) + 1
    return f'{path.name if path else "untitled"}:{line}'


def source(files, node):
    return files[node.file][1][node.start:node.end]


def ids_in(expr):
    out = []
    stack = [expr]
    while stack:
        e = stack.pop()
        if e is None:
            continue
        if isinstance(e, Id):
            out.append(e)
            continue
        if isinstance(e, lang.Node):
            for name in e.fields:
                value = getattr(e, name, None)
                if isinstance(value, lang.Node):
                    stack.append(value)
                elif isinstance(value, list):
                    stack.extend(v for v in value if isinstance(v, lang.Node))
    return out


class Explainer:
    def __init__(self, result):
        self.result = result
        self.files = result.files
        self.tracer = getattr(result, 'tracer', None)

    def evaluate(self, expr, scope):
        if self.tracer is None or expr is None:
            return UNKNOWN
        try:
            return self.tracer.eval(expr, scope)
        except Exception:
            return UNKNOWN

    def resolve(self, expr, scope, chain=None, depth=0):
        """EditTargets for an expression, deepest definition first."""
        chain = chain or []
        if expr is None or depth > 30:
            return []
        if is_literal(expr):
            return [EditTarget(expr.file, expr.start, expr.end, 'literal', ' → '.join(chain + ['value']) if chain else 'value here')]
        if isinstance(expr, Id):
            binding = scope.binding(expr.name)
            if binding is None or binding.expr is None or binding.kind in ('special', 'missing'):
                return []
            if binding.kind == 'loop':
                return []
            if binding.kind == 'arg':
                step = f'argument {expr.name}'
            elif binding.kind == 'default':
                module = binding.module.name if binding.module is not None else 'module'
                step = f'default of {module}({expr.name})'
            elif binding.kind == 'let':
                step = f'let {expr.name}'
            else:
                step = f'variable {expr.name}'
            deeper = self.resolve(binding.expr, binding.scope, chain + [step], depth + 1)
            if deeper:
                return deeper
            target = binding.expr
            return [EditTarget(target.file, target.start, target.end, 'expression', ' → '.join(chain + [step, 'expression']))]
        return [EditTarget(expr.file, expr.start, expr.end, 'expression', ' → '.join(chain + ['expression']) if chain else 'expression here')]

    def targets(self, expr, scope):
        """Definition target(s) plus an 'only here' override of the immediate expression."""
        out = self.resolve(expr, scope)
        if expr is not None and not is_literal(expr):
            local = EditTarget(expr.file, expr.start, expr.end, 'expression', 'only this call (replace expression)')
            if not any(t.file == local.file and t.start == local.start and t.end == local.end for t in out):
                out.append(local)
        return out

    def origin(self, expr, scope):
        if expr is None:
            return ''
        if isinstance(expr, Id):
            binding = scope.binding(expr.name)
            if binding is None:
                return 'unknown name'
            if binding.kind == 'loop':
                return 'loop variable'
            if binding.kind == 'special':
                return 'built-in'
            if binding.expr is not None:
                return location(self.files, binding.expr.file, binding.expr.start)
        return location(self.files, expr.file, expr.start)

    # -------------------------------------------------------------- explain
    def explain(self, leaf):
        groups = []
        frames = leaf.frame.chain()
        variables = {}
        seen_groups = set()
        for frame in reversed(frames):
            node = frame.node
            if frame.kind == 'children':
                continue
            key = (id(node), id(frame.scope))
            if key in seen_groups:
                continue
            seen_groups.add(key)
            if frame.kind == 'module':
                group = Group(f'{node.name}(…)  module call · {location(self.files, node.file, node.start)}',
                              node.file, node.start, kind='module')
                for param in frame.module.params:
                    binding = (frame.values or {}).get(param.name) or frame.inner.vars.get(param.name)
                    if binding is None:
                        continue
                    value = self.evaluate(binding.expr, binding.scope) if binding.expr is not None else binding.value
                    expr = binding.expr
                    targets = self.targets(expr, binding.scope) if expr is not None else []
                    if binding.kind == 'default':
                        # Defaults live in the definition (shared by all calls); offer a per-call override.
                        targets.append(EditTarget(node.file, node.start, node.end, 'insert',
                                                  f'only this call: add {param.name}= argument', insert_name=param.name))
                        origin = f'default · {location(self.files, param.file, param.start)}'
                    elif binding.kind == 'arg':
                        origin = f'argument · {location(self.files, expr.file, expr.start)}'
                    else:
                        origin = 'not set (undef)'
                        targets.append(EditTarget(node.file, node.start, node.end, 'insert',
                                                  f'add {param.name}= argument', insert_name=param.name))
                    row = Row(group.title, param.name, value, source(self.files, expr) if expr is not None else 'undef',
                              origin, targets, is_color=self.looks_like_color(param.name, value),
                              frame_file=node.file, frame_pos=node.start)
                    group.rows.append(row)
                    if expr is not None:
                        self.collect_vars(expr, binding.scope, variables)
                groups.append(group)
            elif frame.kind == 'builtin':
                name = node.name
                label = {'color': 'color', 'translate': 'move', 'rotate': 'rotate', 'scale': 'scale'}.get(name, name)
                group = Group(f'{name}(…)  {label} · {location(self.files, node.file, node.start)}', node.file, node.start,
                              kind='primitive' if frame is frames[-1] else name)
                names = BUILTIN_PARAMS.get(name, [])
                position = 0
                given = set()
                for arg in node.args:
                    if arg.name is None:
                        pname = names[position] if position < len(names) else f'arg{position}'
                        position += 1
                    else:
                        pname = arg.name
                    given.add(pname)
                    value = self.evaluate(arg.value, frame.scope)
                    row = Row(group.title, pname, value, source(self.files, arg.value), self.origin(arg.value, frame.scope),
                              self.targets(arg.value, frame.scope), is_color=(name == 'color' and pname == 'c'),
                              frame_file=node.file, frame_pos=node.start)
                    group.rows.append(row)
                    self.collect_vars(arg.value, frame.scope, variables)
                for pname, default in BUILTIN_DEFAULTS.get(name, {}).items():
                    if pname not in given:
                        group.rows.append(Row(group.title, pname, default == 'true', default, 'default',
                                              [EditTarget(node.file, node.start, node.end, 'insert', f'add {pname}= argument', pname)],
                                              frame_file=node.file, frame_pos=node.start))
                if name in ('sphere', 'cylinder', 'circle') and '$fn' not in given:
                    group.rows.append(Row(group.title, '$fn', None, '(inherited)', 'resolution',
                                          [EditTarget(node.file, node.start, node.end, 'insert', 'add $fn= argument', '$fn')],
                                          frame_file=node.file, frame_pos=node.start))
                if group.rows or frame is frames[-1]:
                    groups.append(group)
            elif frame.kind == 'for':
                group = Group(f'for loop · {location(self.files, node.file, node.start)}', node.file, node.start, kind='for')
                for arg in node.args:
                    if not arg.name:
                        continue
                    current = (frame.values or {}).get(arg.name)
                    group.rows.append(Row(group.title, arg.name + ' (this instance)', current, fmt(current), 'loop value',
                                          [], editable=False, frame_file=node.file, frame_pos=node.start))
                    group.rows.append(Row(group.title, arg.name + ' range', self.evaluate(arg.value, frame.scope.parent or frame.scope),
                                          source(self.files, arg.value), self.origin(arg.value, frame.scope),
                                          self.targets(arg.value, frame.scope.parent or frame.scope),
                                          frame_file=node.file, frame_pos=node.start))
                    self.collect_vars(arg.value, frame.scope, variables)
                groups.append(group)
            elif frame.kind == 'if':
                cond = node.cond
                group = Group(f'if · {location(self.files, node.file, node.start)}', node.file, node.start, kind='if')
                group.rows.append(Row(group.title, 'condition', (frame.values or {}).get('condition'), source(self.files, cond),
                                      self.origin(cond, frame.scope), self.targets(cond, frame.scope),
                                      frame_file=node.file, frame_pos=node.start))
                self.collect_vars(cond, frame.scope, variables)
                groups.append(group)
        for group in groups:
            group.library = self.is_library(group.file)
        if variables:
            group = Group('Variables used by this object', -1, 0, kind='variables')
            for (_, name), (binding, depth) in sorted(variables.items(), key=lambda kv: (kv[1][1], kv[0][1])):
                expr = binding.expr
                value = self.evaluate(expr, binding.scope)
                group.rows.append(Row(group.title, name, value, source(self.files, expr),
                                      location(self.files, expr.file, expr.start),
                                      self.resolve(expr, binding.scope) or [EditTarget(expr.file, expr.start, expr.end, 'expression', 'expression')],
                                      is_color=self.looks_like_color(name, value),
                                      note=f'used {self.usage_count(name)}× in the project',
                                      frame_file=expr.file, frame_pos=expr.start))
            groups.append(group)
        return groups

    def is_library(self, file):
        """Files that belong to a third-party library rather than the user's design."""
        if file is None or file < 0 or file >= len(self.files):
            return False
        path = self.files[file][0]
        main = self.files[0][0]
        if path is None or file == 0:
            return False
        parts = {p.lower() for p in path.parts}
        if parts & {'lib', 'libs', 'libraries', 'bosl2', 'bosl', 'nopscadlib', 'mcad', 'node_modules'}:
            return True
        if main is not None:
            try:
                path.resolve().relative_to(main.resolve().parent)
            except ValueError:
                return True
        return False

    def collect_vars(self, expr, scope, out, depth=0):
        if depth > 4:
            return
        for ident in ids_in(expr):
            binding = scope.binding(ident.name)
            if binding is None or binding.kind != 'assign' or binding.expr is None:
                continue
            if binding.scope.label not in ('file', 'use'):
                continue
            key = (id(binding), ident.name)
            if key not in out:
                out[key] = (binding, depth)
                self.collect_vars(binding.expr, binding.scope, out, depth + 1)

    def usage_count(self, name):
        pattern = re.compile(r'(?<![\w$])' + re.escape(name) + r'(?![\w$])')
        return sum(len(pattern.findall(text)) for _, text in self.files) - 1

    @staticmethod
    def looks_like_color(name, value):
        if isinstance(value, str):
            return bool(re.fullmatch(r'#[0-9a-fA-F]{3,8}', value)) or value.lower() in css_colors() or 'col' in name.lower()
        if isinstance(value, list) and len(value) in (3, 4) and all(isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 1 for x in value):
            return 'col' in name.lower() or name in ('c', 'rgb', 'rgba')
        return False

    def effective_color(self, leaf):
        frame = leaf.color
        if frame is None:
            return None, None
        return (frame.values or {}).get('c'), frame


def css_colors():
    global COLOR_NAMES
    if COLOR_NAMES is None:
        COLOR_NAMES = set('''aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet
        brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan
        darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred
        darksalmon darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue
        dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray
        green greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen
        lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink
        lightsalmon lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen
        linen magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue
        mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy
        oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip
        peachpuff peru pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown
        seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal
        thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen transparent'''.split())
    return COLOR_NAMES


def apply_edit(files, target, new_text):
    """Return (file_index, new_file_text) for an EditTarget and replacement text."""
    text = files[target.file][1]
    if target.kind == 'insert':
        call = text[target.start:target.end]
        open_paren = call.find('(')
        if open_paren < 0:
            raise ValueError('Cannot find the argument list')
        # matching close paren of the argument list
        depth = 0
        close = None
        in_string = False
        i = open_paren
        while i < len(call):
            ch = call[i]
            if in_string:
                if ch == '\\':
                    i += 2
                    continue
                if ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch in '([{':
                depth += 1
            elif ch in ')]}':
                depth -= 1
                if depth == 0:
                    close = i
                    break
            i += 1
        if close is None:
            raise ValueError('Unbalanced argument list')
        inside = call[open_paren + 1:close]
        piece = f'{target.insert_name} = {new_text}'
        insertion = piece if not inside.strip() else ', ' + piece
        at = target.start + close
        if inside.strip():
            # keep trailing whitespace/newline before ')' in place
            stripped = inside.rstrip()
            at = target.start + open_paren + 1 + len(stripped)
        return target.file, text[:at] + insertion + text[at:]
    return target.file, text[:target.start] + new_text + text[target.end:]


def format_value(value, like=None):
    """SCAD source text for a new value, matching the style of the literal it replaces."""
    if isinstance(value, str) and like is not None and not like.strip().startswith('"'):
        return value
    return fmt(value)


def color_literal(rgba, original_text):
    """Write a color in the same notation as the original literal (string or vector)."""
    r, g, b, a = rgba
    text = (original_text or '').strip()
    if text.startswith('['):
        values = [r, g, b] + ([a] if text.count(',') >= 3 or a < 1 else [])
        return '[' + ', '.join(f'{v:.3g}' for v in values) + ']'
    hex_color = '#{:02x}{:02x}{:02x}'.format(round(r * 255), round(g * 255), round(b * 255))
    if a < 1:
        hex_color += '{:02x}'.format(round(a * 255))
    return '"' + hex_color + '"'
