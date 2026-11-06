"""Turn OpenSCAD output and parser failures into located diagnostics.

OpenSCAD reports, for example::

    ERROR: Parser error: syntax error in file syntax.scad, line 4
    WARNING: Ignoring unknown variable 'y' in file lib.scad, line 3
    ERROR: Assertion '(a > 0)' failed: "a must be positive" in file a.scad, line 1
    TRACE: called by 'm' in file assert.scad, line 3

SCAD Forge compiles temporary copies (``.scadforge-*.scad`` next to the source,
or a staged project directory). Those names are mapped back to the file the
user is editing so markers land in the right editor tab.
"""
from dataclasses import dataclass, field
from pathlib import Path
import re

LOCATED = re.compile(
    r'^(?P<kind>ERROR|WARNING|DEPRECATED|TRACE|ECHO)\s*:\s*(?P<message>.*?)[,\s]*'
    r'\bin file\s+"?(?P<file>[^"\n]*?)"?\s*,\s*line\s+(?P<line>\d+)\s*[.:]?\s*(?P<tail>.*)$')
UNLOCATED = re.compile(r'^(?P<kind>ERROR|WARNING|DEPRECATED)\s*:\s*(?P<message>.+)$')
SEVERITY = {'ERROR': 'error', 'WARNING': 'warning', 'DEPRECATED': 'warning', 'TRACE': 'info', 'ECHO': 'info'}

HINTS = (
    (re.compile(r'syntax error', re.I),
     'Check this line and the line above: a missing ";" , ")" , "]" or "}" is usually reported on the next statement.'),
    (re.compile(r'unknown module', re.I),
     'The module is not defined here. Is the file that defines it included with include <...> or use <...>, and is the name spelled right?'),
    (re.compile(r'unknown variable', re.I),
     'The variable is not defined in this scope. Variables from a use <...> file are not visible; use include <...> or pass it as a module parameter.'),
    (re.compile(r'unknown function', re.I),
     'The function is not defined here. Check include/use and the spelling.'),
    (re.compile(r'not specified as parameter', re.I),
     'This named argument does not exist on the called module/function. Check the parameter name.'),
    (re.compile(r"Can't open (?:include|library|import) file", re.I),
     'The file path is resolved relative to the including file and the OpenSCAD library path.'),
    (re.compile(r'Assertion', re.I), 'An assert() in the model failed; the TRACE entries show the call chain.'),
    (re.compile(r'Recursion detected|stack', re.I), 'Recursive module/function without a terminating condition.'),
    (re.compile(r'Object may not be a valid 2-manifold|not closed', re.I),
     'Touching/coincident faces: let cutters overlap by a small epsilon (e.g. 0.01 mm).'),
)


@dataclass
class Problem:
    severity: str            # error | warning | info
    message: str
    file: Path = None        # None: not tied to a source file
    line: int = None         # 0-based
    column: int = None
    trace: list = field(default_factory=list)   # [(file, line, text)]
    hint: str = ''

    @property
    def location(self):
        if self.file is None or self.line is None:
            return ''
        return f'{Path(self.file).name}:{self.line + 1}'

    def summary(self):
        where = self.location
        return (where + '  ' if where else '') + self.message


def hint_for(message):
    for pattern, hint in HINTS:
        if pattern.search(message):
            return hint
    return ''


class PathMapper:
    """Map compiler file names back to editable source paths."""
    def __init__(self, source_path=None, directory=None, stage_root=None, project_root=None):
        self.source_path = Path(source_path) if source_path else None
        self.directory = Path(directory) if directory else (self.source_path.parent if self.source_path else None)
        self.stage_root = Path(stage_root) if stage_root else None
        self.project_root = Path(project_root) if project_root else None

    def __call__(self, name):
        if not name:
            return None
        path = Path(name)
        base = path.name
        if base.startswith(('.scadforge-', '.forge-')) and base.endswith('.scad'):
            return self.source_path or Path('<current>')
        if base.endswith('.csg'):
            return None
        if not path.is_absolute() and self.directory is not None:
            path = self.directory / path
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if self.stage_root is not None and self.project_root is not None:
            try:
                return self.project_root / resolved.relative_to(self.stage_root.resolve())
            except ValueError:
                pass
        return resolved


def parse_log(log, mapper=None, include_info=False):
    """Return a list of :class:`Problem` from OpenSCAD's combined output."""
    mapper = mapper or PathMapper()
    problems = []
    last = None
    seen = set()
    for raw in (log or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        m = LOCATED.match(line)
        if m:
            kind = m['kind']
            message = (m['message'] + (' ' + m['tail'] if m['tail'] else '')).strip().rstrip(',.')
            message = re.sub(r'^Parser error:\s*', 'Parser error: ', message)
            file = mapper(m['file'])
            number = int(m['line']) - 1
            if kind == 'TRACE':
                if last is not None:
                    last.trace.append((file, number, message))
                continue
            if kind == 'ECHO' and not include_info:
                continue
            key = (kind, message, str(file), number)
            if key in seen:
                last = None
                continue
            seen.add(key)
            last = Problem(SEVERITY[kind], message, file, number, None, [], hint_for(message))
            problems.append(last)
            continue
        m = UNLOCATED.match(line)
        if m:
            message = m['message'].strip()
            key = (m['kind'], message)
            if key in seen:
                continue
            seen.add(key)
            last = Problem(SEVERITY[m['kind']], message, None, None, None, [], hint_for(message))
            problems.append(last)
            continue
        if line.startswith("Can't parse file") and problems:
            continue
        if line.startswith('Execution aborted'):
            problems.append(Problem('error', line, hint=''))
    for problem in problems:
        # An error raised inside a library file is easier to fix from the call
        # site in the user's file: keep the trace so the panel can offer both.
        if problem.file is not None and problem.trace:
            problem.trace = [t for t in problem.trace if t[0] is not None]
    return problems


def source_problems(error, source, path=None):
    """Problems for a lexical parse failure (SourceError with an offset)."""
    position = getattr(error, 'position', None)
    if position is None:
        return [Problem('error', str(error), path, None, None, [], hint_for('syntax error'))]
    line = source.count('\n', 0, position)
    column = position - (source.rfind('\n', 0, position) + 1)
    return [Problem('error', str(error), path, line, column, [], hint_for('syntax error'))]


def count_text(problems):
    errors = sum(p.severity == 'error' for p in problems)
    warnings = sum(p.severity == 'warning' for p in problems)
    parts = []
    if errors:
        parts.append(f'{errors} error' + ('s' if errors != 1 else ''))
    if warnings:
        parts.append(f'{warnings} warning' + ('s' if warnings != 1 else ''))
    return ', '.join(parts) or 'no problems'
