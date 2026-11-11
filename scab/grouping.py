"""Moving parts inside a design and grouping parts: plain OpenSCAD edits.

* A part nested in a transform can be moved on its own (a `translate` is added
  or an existing literal one is adjusted) or together with the group around it.
* Several parts can be grouped. Neighbours in the text get one
  `translate(<name>) { ... }` around them; parts spread over the file each get
  `translate(<name>)` with the same variable, so they move together:

      lid_and_standoffs_pos = [0, 0, 0];   // SCAB group: moves lid, standoffs together
"""
import re
import numpy as np
from .model import Document, TOKEN
from .parameters import Scope, evaluate, argument, rotation

TRANSFORMS = {'translate', 'rotate', 'scale', 'mirror', 'multmatrix', 'color', 'resize', 'offset'}
WRAPPERS = TRANSFORMS | {'group', 'union', 'render', 'if', 'for', 'let', 'intersection_for', 'hull', 'minkowski',
                         'difference', 'intersection', 'linear_extrude', 'rotate_extrude', 'projection'}
PRIMITIVES = {'cube', 'sphere', 'cylinder', 'polyhedron', 'square', 'circle', 'polygon', 'text', 'import', 'surface'}
STOP = {'the', 'a', 'an', 'of', 'for', 'on', 'in', 'to', 'and', 'with', 'from', 'under', 'over', 'its', 'at', 'by', 'this',
        'that', 'these', 'is', 'are', 'scab', 'todo', 'note'}
GROUP_MARK = '// SCAB group'
NUMBER = r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?'
LITERAL_VECTOR = re.compile(r'\s*\[\s*(' + NUMBER + r')\s*,\s*(' + NUMBER + r')\s*,\s*(' + NUMBER + r')\s*\]\s*')


def fmt(v):
    v = float(v)
    return '0' if abs(v) < 1e-9 else f'{v:.6g}'


def vector_text(values):
    return '[' + ', '.join(fmt(v) for v in values) + ']'


# ------------------------------------------------------------------ the tree
def node_path(doc, start, end):
    """Nodes from a top-level object down to the deepest node containing [start, end)."""
    path = []
    level = doc.roots
    while True:
        found = next((n for n in level if n.start <= start and end <= n.end), None)
        if found is None:
            return path
        path.append(found)
        level = found.children


def parts_in(node):
    """Geometry leaves (calls without children) under a node: how many parts a group holds."""
    if not node.children:
        return 1
    return sum(parts_in(c) for c in node.children)


def line_of(doc, position):
    return doc.source.count('\n', 0, position) + 1


def translate_variable(doc, node):
    """`name` when the node is `translate(name) ...` with a plain variable."""
    if node.name != 'translate':
        return None
    try:
        a, b = argument(doc, node, 'v', 0)
    except (TypeError, ValueError):
        return None
    text = doc.source[a:b].strip()
    return text if re.fullmatch(r'[A-Za-z_]\w*', text) else None


def variable_uses(doc, name):
    """Every `translate(name)` node in the design."""
    return [n for n in doc.walk() if translate_variable(doc, n) == name]


def group_label(doc, name):
    uses = variable_uses(doc, name)
    parts = sum(parts_in(u.children[0]) if u.children else 1 for u in uses)
    return f'Group {name} · {parts} part{"s" if parts != 1 else ""}'


def is_group_variable(doc, name):
    """A plain-vector variable used by several translate() calls, or one SCAB made for a group."""
    if variable_value(doc, name) is None:
        return False
    if len(variable_uses(doc, name)) > 1:
        return True
    m = re.search(r'^[ \t]*' + re.escape(name) + r'\s*=[^;]*;([^\n]*)', doc.source, re.M)
    return bool(m and GROUP_MARK in m.group(1))


def move_options(doc, path):
    """What a click on the part at the end of `path` can move.

    [(label, kind, payload)] with kind 'node' (payload: the node) or 'variable'
    (payload: variable name shared by several translate() calls), most specific first.
    """
    if not path:
        return []
    out = []
    part = path[-1]
    out.append((f'Part · {describe(doc, part)} · L{line_of(doc, part.start)}', 'node', part))
    seen_vars = set()
    for depth in range(len(path) - 2, -1, -1):
        node = path[depth]
        name = translate_variable(doc, node)
        if name and name not in seen_vars and is_group_variable(doc, name):
            seen_vars.add(name)
            out.append((group_label(doc, name), 'variable', name))
        if depth == 0:
            out.append((f'Whole object · {describe(doc, node)} · L{line_of(doc, node.start)}', 'node', node))
        elif parts_in(node) > parts_in(path[depth + 1]):
            # name the group by the transform that holds it: climb single-child wrappers (not the top level)
            k = depth
            while k - 1 >= 1 and len(path[k - 1].children) == 1:
                k -= 1
            holder = path[k]
            out.append((f'Group · {describe_group(doc, holder)} · L{line_of(doc, holder.start)} · {parts_in(holder)} parts', 'node', holder))
    # the part's own shared variable (it may be the deepest node itself)
    name = translate_variable(doc, part)
    if name and name not in seen_vars and is_group_variable(doc, name):
        out.insert(1, (group_label(doc, name), 'variable', name))
    # a group variable used once already is "the group": drop the block / object options that mean the same
    for _, kind, name in [o for o in out if o[1] == 'variable']:
        uses = variable_uses(doc, name)
        if len(uses) == 1:
            same = {(uses[0].start, uses[0].end)} | {(c.start, c.end) for c in uses[0].children}
            out = [o for o in out if o[1] != 'node' or o is out[0] or (o[2].start, o[2].end) not in same]
    unique, spans = [], set()
    for option in out:
        key = (option[1], option[2] if option[1] == 'variable' else (option[2].start, option[2].end))
        if key not in spans:
            spans.add(key)
            unique.append(option)
    return unique


def describe_group(doc, node):
    if node.name == 'translate':
        try:
            a, b = argument(doc, node, 'v', 0)
            text = ' '.join(doc.source[a:b].split())
            return f'translate({text[:24]}{"…" if len(text) > 24 else ""})'
        except (TypeError, ValueError):
            pass
    return node.name if node.name != 'group' else '{ … }'


def describe(doc, node):
    """Short human name of what a node draws."""
    probe = node
    while probe.name in WRAPPERS and len(probe.children) == 1:
        probe = probe.children[0]
    return probe.name if probe.name not in ('group',) else node.name


# ----------------------------------------------------------- moving one node
def literal_translate(doc, node):
    """(span, [x, y, z]) of a translate() with a literal vector, else None."""
    if node is None or node.name != 'translate':
        return None
    try:
        a, b = argument(doc, node, 'v', 0)
    except (TypeError, ValueError):
        return None
    m = LITERAL_VECTOR.fullmatch(doc.source[a:b])
    if not m:
        return None
    return (a, b), [float(v) for v in m.groups()]


def nudge_source(doc, path, kind, axis, delta, pivot, context=None):
    """Move / rotate the node at the end of `path` (a part inside other transforms).

    context: the 4x4 world matrix in effect where the node sits (from the tracer), so a
    drag along world X stays world X inside rotated groups.
    """
    node = path[-1]
    parent = path[-2] if len(path) > 1 else None
    context = np.eye(4) if context is None else np.asarray(context, dtype=float)
    linear = context[:3, :3]
    try:
        inverse = np.linalg.inv(linear)
    except np.linalg.LinAlgError:
        inverse = np.eye(3)
    if kind == 'translate':
        world = np.zeros(3)
        world[axis] = float(delta)
        local = inverse @ world
        # adjust the node's own literal translate, or a literal translate that only wraps this node
        for candidate in (node, parent if parent is not None and len(parent.children) == 1 else None):
            found = literal_translate(doc, candidate)
            if found:
                # both act in the node's context frame (same linear part), so the local delta applies
                (a, b), value = found
                new = [value[i] + local[i] for i in range(3)]
                return doc.source[:a] + vector_text(new) + doc.source[b:]
        return doc.source[:node.start] + f'translate({vector_text(local)}) ' + doc.source[node.start:]
    if kind == 'rotate':
        axis_world = np.zeros(3)
        axis_world[axis] = 1.
        axis_local = inverse @ axis_world
        axis_local /= np.linalg.norm(axis_local) or 1.
        p = (np.linalg.inv(context) @ np.r_[np.asarray(pivot, dtype=float), 1.])[:3]
        if np.allclose(np.abs(axis_local), np.eye(3)[axis], atol=1e-9):
            angles = np.zeros(3)
            angles[axis] = float(delta) * float(np.sign(axis_local[axis]))
            turn = f'rotate({vector_text(angles)})'
        else:
            turn = f'rotate(a = {fmt(delta)}, v = {vector_text(axis_local)})'
        prefix = f'translate({vector_text(p)}) {turn} translate({vector_text(-p)}) '
        return doc.source[:node.start] + prefix + doc.source[node.start:]
    raise ValueError('Unknown transform')


# ---------------------------------------------------------------- variables
def variable_value(doc, name):
    """(span of the initializer, [x, y, z]) of `name = [x, y, z];`, else None."""
    for var, a, b in doc.variables():
        if var == name:
            m = LITERAL_VECTOR.fullmatch(doc.source[a:b])
            if m:
                return (a, b), [float(v) for v in m.groups()]
            try:
                value = evaluate(doc.source[a:b], Scope(doc.source, {}))
                if isinstance(value, (list, tuple)) and len(value) == 3:
                    return (a, b), [float(v) for v in value]
            except (ValueError, TypeError, SyntaxError, KeyError, ZeroDivisionError):
                return None
    return None


def move_variable_source(doc, name, delta):
    """Add a vector to a group variable's initializer (it must be a plain vector)."""
    found = variable_value(doc, name)
    if found is None:
        raise ValueError(f'{name} is not a plain [x, y, z] vector, so it cannot be moved here')
    (a, b), value = found
    new = [value[i] + float(delta[i]) for i in range(3)]
    return doc.source[:a] + ' ' + vector_text(new) + doc.source[b:]


# ------------------------------------------------------------------ grouping
def _comment_above(doc, node):
    line_start = doc.source.rfind('\n', 0, node.start) + 1
    if doc.source[line_start:node.start].strip():
        return None                      # not the first thing on its line
    previous_end = line_start - 1
    if previous_end <= 0:
        return None
    previous_start = doc.source.rfind('\n', 0, previous_end) + 1
    line = doc.source[previous_start:previous_end].strip()
    if line.startswith('//') and not line.startswith(GROUP_MARK):
        return line[2:].strip()
    return None


def _words(text):
    return [w for w in re.findall(r'[a-z][a-z0-9]*', text.lower()) if w not in STOP]


def part_name(doc, node):
    """A short name for one grouped node: comment above it, module called, loop list or primitive."""
    comment = _comment_above(doc, node)
    if comment:
        words = _words(comment.split(':')[0])
        if words:
            return words[0]
    probe = node
    loop = None
    while True:
        if probe.name in ('for', 'intersection_for') and loop is None:
            m = re.search(r'=\s*([A-Za-z_]\w*)\s*\)', doc.source[probe.start:probe.end])
            if m:
                loop = m.group(1)
        if probe.name in WRAPPERS and probe.children:
            # descend into the first geometry child
            probe = probe.children[0]
            continue
        break
    if probe.name not in PRIMITIVES and probe.name not in WRAPPERS and re.fullmatch(r'[A-Za-z_]\w*', probe.name):
        return probe.name
    if loop:
        return loop
    return probe.name if re.fullmatch(r'[A-Za-z_]\w*', probe.name) else 'part'


def plural(word):
    if word.endswith('s'):
        return word
    if word.endswith('y') and len(word) > 2 and word[-2] not in 'aeiou':
        return word[:-1] + 'ies'
    return word + 's'


def suggest_group_name(doc, nodes):
    """`<what>_pos`: e.g. lid_and_standoffs_pos, cylinders_pos, base_lid_etc_pos; never an existing name."""
    names = []
    for node in nodes:
        name = part_name(doc, node)
        if name not in names:
            names.append(name)
    if len(names) == 1:
        base = plural(names[0]) if len(nodes) > 1 else names[0]
    elif len(names) == 2:
        base = f'{names[0]}_and_{names[1]}'
    else:
        base = f'{names[0]}_{names[1]}_etc'
    base = re.sub(r'[^A-Za-z0-9_]', '_', base).strip('_').lower() or 'group'
    if base[0].isdigit():
        base = 'g_' + base
    taken = set(re.findall(r'[A-Za-z_]\w*', doc.source))
    name, k = f'{base}_pos', 2
    while name in taken:
        name = f'{base}{k}_pos'
        k += 1
    return name


def _siblings_adjacent(doc, nodes):
    """True when the nodes are consecutive children of one parent (or consecutive top-level statements)."""
    ordered = sorted(nodes, key=lambda n: n.start)
    for a, b in zip(ordered, ordered[1:]):
        between = doc.source[a.end:b.start]
        stripped = ''.join(m[0] for m in TOKEN.finditer(between) if not m[0].isspace() and not m[0].startswith(('//', '/*')))
        if stripped:
            return False
    # same parent: one must not contain another, and they must share the containing path
    paths = [tuple(id(n) for n in node_path(doc, x.start, x.end)[:-1]) for x in ordered]
    return len(set(paths)) == 1


def group_source(doc, nodes, name, value=(0., 0., 0.)):
    """Wrap the nodes in translate(name) (one block if they are neighbours) and define `name = value;`.

    Returns the new source.
    """
    if not nodes:
        raise ValueError('Nothing selected to group')
    ordered = sorted({(n.start, n.end): n for n in nodes}.values(), key=lambda n: n.start)
    for a, b in zip(ordered, ordered[1:]):
        if b.start < a.end:
            raise ValueError('One selected part contains another; select either the group or its parts')
    source = doc.source
    labels = []
    for node in ordered:
        label = part_name(doc, node)
        if label not in labels:
            labels.append(label)
    if len(ordered) > 1 and _siblings_adjacent(doc, ordered):
        first, last = ordered[0], ordered[-1]
        line_start = source.rfind('\n', 0, first.start) + 1
        indent = source[line_start:first.start] if not source[line_start:first.start].strip() else ''
        body = source[first.start:last.end]
        body = '\n'.join(('    ' + l if l.strip() else l) for l in body.split('\n'))
        source = source[:first.start] + f'translate({name}) {{\n{indent}{body}\n{indent}}}' + source[last.end:]
    else:
        for node in reversed(ordered):
            source = source[:node.start] + f'translate({name}) ' + source[node.start:]
    # the variable goes on its own line above the first top-level statement involved
    roots = Document(source).roots
    first_root = min((r for r in roots if r.end > ordered[0].start), key=lambda r: r.start, default=None)
    at = source.rfind('\n', 0, first_root.start) + 1 if first_root is not None else 0
    # keep a comment that belongs to that statement attached to it
    while at > 0:
        previous = source.rfind('\n', 0, at - 1) + 1
        if source[previous:at].strip().startswith('//') and not source[previous:at].strip().startswith(GROUP_MARK):
            at = previous
        else:
            break
    what = ', '.join(labels[:4]) + (' …' if len(labels) > 4 else '')
    line = f'{name} = {vector_text(value)};   {GROUP_MARK}: moves {what} together\n'
    return source[:at] + line + source[at:]


def ungroup_source(doc, name):
    """Remove a group variable: apply its value to every member as a literal translate, drop the definition."""
    found = variable_value(doc, name)
    value = found[1] if found else [0., 0., 0.]
    source = doc.source
    for node in sorted(variable_uses(doc, name), key=lambda n: n.start, reverse=True):
        a, b = argument(doc, node, 'v', 0)
        replacement = vector_text(value)
        if np.allclose(value, 0) and len(node.children) == 1:
            child = node.children[0]
            inner = source[child.start:child.end]
            if child.name == 'group':                # translate(name) { ... } -> its contents
                inner = source[child.start + 1:child.end - 1].strip('\n')
                inner = '\n'.join(l[4:] if l.startswith('    ') else l for l in inner.split('\n'))
            source = source[:node.start] + inner + source[node.end:]
        else:
            source = source[:a] + replacement + source[b:]
    source = re.sub(r'^[ \t]*' + re.escape(name) + r'\s*=[^;]*;[^\n]*\n', '', source, count=1, flags=re.M)
    return source
