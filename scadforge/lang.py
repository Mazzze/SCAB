"""OpenSCAD parser producing an AST with exact source spans.

Every node carries ``file`` (an index into the parsing session's file table)
plus ``start``/``end`` character offsets.  The tracer uses the spans to tell
the user — and the inspector's editor — exactly where each value is written.
"""
import re


class ParseError(ValueError):
    def __init__(self, message, position=None, file=None):
        super().__init__(message)
        self.position = position
        self.file = file


TOKEN = re.compile(r'''
    (?P<ws>\s+|//[^\n]*|/\*[\s\S]*?\*/)
  | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<id>[$A-Za-z_][$\w]*)
  | (?P<op><=|>=|==|!=|&&|\|\||[-+*/%^!<>=?:;,.(){}\[\]#])
''', re.X)

ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\'}


def unescape(raw):
    out = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '\\' and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt in ESCAPES:
                out.append(ESCAPES[nxt])
                i += 2
                continue
            if nxt == 'x' and i + 3 < len(raw) + 1:
                try:
                    out.append(chr(int(raw[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
            if nxt in 'uU':
                width = 4 if nxt == 'u' else 6
                try:
                    out.append(chr(int(raw[i + 2:i + 2 + width], 16)))
                    i += 2 + width
                    continue
                except ValueError:
                    pass
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


class Node:
    __slots__ = ('file', 'start', 'end')
    fields = ()

    def __init__(self, file, start, end, *values):
        self.file, self.start, self.end = file, start, end
        for name, value in zip(self.fields, values):
            setattr(self, name, value)

    def __repr__(self):
        return f'{type(self).__name__}({", ".join(f"{f}={getattr(self, f)!r}" for f in self.fields)})'


def node(name, *fields):
    return type(name, (Node,), {'__slots__': fields, 'fields': fields})


# ----------------------------------------------------------------- expressions
Num = node('Num', 'value')
Str = node('Str', 'value')
Bool = node('Bool', 'value')
Undef = node('Undef')
Id = node('Id', 'name')
Vector = node('Vector', 'items')
Range = node('Range', 'begin', 'step', 'stop')
Unary = node('Unary', 'op', 'operand')
Binary = node('Binary', 'op', 'left', 'right')
Ternary = node('Ternary', 'cond', 'then', 'other')
Call = node('Call', 'func', 'args')
Index = node('Index', 'value', 'index')
Member = node('Member', 'value', 'name')
Let = node('Let', 'assignments', 'body')
AssertExpr = node('AssertExpr', 'args', 'body')
EchoExpr = node('EchoExpr', 'args', 'body')
FunctionLiteral = node('FunctionLiteral', 'params', 'body')
CompFor = node('CompFor', 'assignments', 'body')
CompForC = node('CompForC', 'init', 'cond', 'update', 'body')
CompIf = node('CompIf', 'cond', 'then', 'other')
CompLet = node('CompLet', 'assignments', 'body')
Each = node('Each', 'value')
Arg = node('Arg', 'name', 'value')          # name None for positional
Param = node('Param', 'name', 'default')    # default None when absent

# ------------------------------------------------------------------ statements
Assign = node('Assign', 'name', 'value')
ModuleDef = node('ModuleDef', 'name', 'params', 'body')
FunctionDef = node('FunctionDef', 'name', 'params', 'body')
Include = node('Include', 'path', 'use')
Instance = node('Instance', 'name', 'args', 'children', 'modifier', 'name_end')
Block = node('Block', 'body')
If = node('If', 'cond', 'then', 'other')
Empty = node('Empty')

LITERALS = (Num, Str, Bool, Undef)


def is_literal(expr):
    """Literal or vector of literals (with optional unary minus) — directly editable."""
    if isinstance(expr, LITERALS):
        return True
    if isinstance(expr, Unary) and expr.op in '-+' and isinstance(expr.operand, Num):
        return True
    if isinstance(expr, Vector):
        return all(is_literal(item) for item in expr.items)
    return False


class Parser:
    def __init__(self, text, file=0):
        self.text = text
        self.file = file
        self.tokens = []
        position = 0
        length = len(text)
        while position < length:
            m = TOKEN.match(text, position)
            if not m:
                raise ParseError(f'Unexpected character {text[position]!r}', position, file)
            kind = m.lastgroup
            if kind != 'ws':
                self.tokens.append((kind, m.group(), m.start(), m.end()))
            elif m.group().startswith('/*') and not m.group().endswith('*/'):
                raise ParseError('Unclosed /* comment', m.start(), file)
            position = m.end()
        self.tokens.append(('eof', '', length, length))
        self.i = 0

    # --------------------------------------------------------------- helpers
    @property
    def tok(self):
        return self.tokens[self.i]

    def peek(self, offset=1):
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def at(self, value, kind=None):
        k, v, _, _ = self.tokens[self.i]
        return v == value and (kind is None or k == kind) and k != 'str'

    def take(self, value=None):
        token = self.tokens[self.i]
        if value is not None and (token[1] != value or token[0] == 'str'):
            found = token[1] or 'end of file'
            raise ParseError(f'Expected {value!r} but found {found!r}', token[2], self.file)
        self.i += 1
        return token

    def last_end(self):
        return self.tokens[self.i - 1][3]

    def error(self, message):
        return ParseError(message, self.tok[2], self.file)

    # ------------------------------------------------------------ statements
    def parse_file(self):
        out = []
        while self.tok[0] != 'eof':
            stmt = self.statement()
            if stmt is not None:
                out.append(stmt)
        return out

    def statement(self):
        kind, value, start, _ = self.tok
        if kind == 'op' and value == ';':
            self.take()
            return Empty(self.file, start, self.last_end())
        if kind == 'op' and value == '{':
            self.take()
            body = []
            while not self.at('}'):
                if self.tok[0] == 'eof':
                    raise ParseError("Unclosed '{'", start, self.file)
                stmt = self.statement()
                if stmt is not None:
                    body.append(stmt)
            self.take('}')
            return Block(self.file, start, self.last_end(), body)
        if kind == 'id' and value in ('include', 'use') and self.text[self.tok[3]:].lstrip().startswith('<'):
            close = self.text.find('>', self.tok[3])
            open_ = self.text.find('<', self.tok[3])
            if close < 0:
                raise ParseError(f'Unclosed {value} <...>', start, self.file)
            path = self.text[open_ + 1:close]
            while self.tok[2] <= close and self.tok[0] != 'eof':
                self.i += 1
            if self.at(';'):
                self.take()
            return Include(self.file, start, close + 1, path, value == 'use')
        if kind == 'id' and value == 'module':
            self.take()
            name = self.take()
            if name[0] != 'id':
                raise ParseError('Expected module name', name[2], self.file)
            params = self.params()
            body = self.statement()
            return ModuleDef(self.file, start, self.last_end(), name[1], params, body)
        if kind == 'id' and value == 'function' and self.peek()[0] == 'id':
            self.take()
            name = self.take()[1]
            params = self.params()
            self.take('=')
            body = self.expr()
            self.take(';')
            return FunctionDef(self.file, start, self.last_end(), name, params, body)
        if kind == 'id' and self.peek()[1] == '=' and self.peek()[0] == 'op':
            self.take()
            self.take('=')
            value_expr = self.expr()
            if not self.at(';'):
                prev = self.tokens[self.i - 1]
                raise ParseError(f'Missing ";" after {prev[1]!r}', prev[3], self.file)
            self.take(';')
            return Assign(self.file, start, self.last_end(), value, value_expr)
        return self.instance()

    def instance(self):
        start = self.tok[2]
        modifier = ''
        while self.tok[0] == 'op' and self.tok[1] in '!#%*':
            modifier += self.take()[1]
        kind, name, name_start, name_end = self.tok
        if kind != 'id':
            raise self.error(f'Unexpected {name!r}' if name else 'Unexpected end of file')
        self.take()
        if name == 'if':
            self.take('(')
            cond = self.expr()
            self.take(')')
            then = self.child()
            other = None
            if self.at('else', 'id') or (self.tok[0] == 'id' and self.tok[1] == 'else'):
                self.take()
                other = self.child()
            return If(self.file, start, self.last_end(), cond, then, other)
        if not self.at('('):
            found = self.tok[1]
            hint = ' — missing "=" or ";"?' if self.tok[0] in ('id', 'num') else ''
            raise self.error(f'Expected "(" after {name!r}{hint}' if found else f'Missing ";" after {name!r}')
        args = self.args()
        children = self.child()
        return Instance(self.file, start, self.last_end(), name, args, children, modifier, name_end)

    def child(self):
        if self.at(';'):
            self.take()
            return []
        if self.tok[0] == 'eof':
            raise ParseError('Missing ";" at end of statement', self.tokens[self.i - 1][3], self.file)
        if self.at('{'):
            start = self.tok[2]
            self.take()
            body = []
            while not self.at('}'):
                if self.tok[0] == 'eof':
                    raise ParseError("Unclosed '{'", start, self.file)
                stmt = self.statement()
                if stmt is not None:
                    body.append(stmt)
            self.take('}')
            return body
        stmt = self.statement()
        return [stmt] if stmt is not None else []

    def params(self):
        self.take('(')
        params = []
        while not self.at(')'):
            kind, name, start, _ = self.take()
            if kind != 'id':
                raise ParseError(f'Expected parameter name, found {name!r}', start, self.file)
            default = None
            if self.at('='):
                self.take()
                default = self.expr()
            params.append(Param(self.file, start, self.last_end(), name, default))
            if not self.at(')'):
                self.take(',')
        self.take(')')
        return params

    def args(self):
        self.take('(')
        args = []
        while not self.at(')'):
            if self.at(','):
                self.take()
                continue
            start = self.tok[2]
            name = None
            if self.tok[0] == 'id' and self.peek()[1] == '=' and self.peek()[0] == 'op':
                name = self.take()[1]
                self.take('=')
            value = self.expr()
            args.append(Arg(self.file, start, self.last_end(), name, value))
            if not self.at(')'):
                self.take(',')
        self.take(')')
        return args

    def assignments(self):
        self.take('(')
        out = []
        while not self.at(')'):
            if self.at(','):
                self.take()
                continue
            kind, name, start, _ = self.take()
            if kind != 'id':
                raise ParseError(f'Expected name, found {name!r}', start, self.file)
            if self.at('='):
                self.take()
                value = self.expr()
            else:
                value = Undef(self.file, start, start)
            out.append(Assign(self.file, start, self.last_end(), name, value))
            if not self.at(')'):
                self.take(',')
        self.take(')')
        return out

    # ----------------------------------------------------------- expressions
    def expr(self):
        start = self.tok[2]
        if self.tok[0] == 'id' and self.tok[1] in ('let', 'assert', 'echo') and self.peek()[1] == '(':
            word = self.take()[1]
            if word == 'let':
                assignments = self.assignments()
                body = self.expr()
                return Let(self.file, start, self.last_end(), assignments, body)
            args = self.args()
            body = None if any(self.at(x) for x in (';', ')', ',', ']', ':', '}')) or self.tok[0] == 'eof' else self.expr()
            return (AssertExpr if word == 'assert' else EchoExpr)(self.file, start, self.last_end(), args, body)
        if self.tok[0] == 'id' and self.tok[1] == 'function' and self.peek()[1] == '(':
            self.take()
            params = self.params()
            body = self.expr()
            return FunctionLiteral(self.file, start, self.last_end(), params, body)
        cond = self.binary(0)
        if self.at('?'):
            self.take()
            then = self.expr()
            self.take(':')
            other = self.expr()
            return Ternary(self.file, start, self.last_end(), cond, then, other)
        return cond

    LEVELS = (('||',), ('&&',), ('==', '!='), ('<', '<=', '>', '>='), ('+', '-'), ('*', '/', '%'), ('^',))

    def binary(self, level):
        if level == len(self.LEVELS):
            return self.unary()
        start = self.tok[2]
        left = self.binary(level + 1)
        ops = self.LEVELS[level]
        while self.tok[0] == 'op' and self.tok[1] in ops:
            op = self.take()[1]
            right = self.binary(level + 1) if op != '^' else self.unary_power()
            left = Binary(self.file, start, self.last_end(), op, left, right)
        return left

    def unary_power(self):
        return self.binary(len(self.LEVELS) - 1)

    def unary(self):
        start = self.tok[2]
        if self.tok[0] == 'op' and self.tok[1] in '-+!':
            op = self.take()[1]
            operand = self.unary()
            if op == '-' and isinstance(operand, Num):
                return Num(self.file, start, self.last_end(), -operand.value)
            return Unary(self.file, start, self.last_end(), op, operand)
        return self.postfix()

    def postfix(self):
        start = self.tok[2]
        value = self.primary()
        while True:
            if self.at('('):
                args = self.args()
                value = Call(self.file, start, self.last_end(), value, args)
            elif self.at('['):
                self.take()
                index = self.expr()
                self.take(']')
                value = Index(self.file, start, self.last_end(), value, index)
            elif self.at('.') and self.peek()[0] == 'id':
                self.take()
                name = self.take()[1]
                value = Member(self.file, start, self.last_end(), value, name)
            else:
                return value

    def primary(self):
        kind, value, start, end = self.tok
        if kind == 'num':
            self.take()
            return Num(self.file, start, end, float(value))
        if kind == 'str':
            self.take()
            return Str(self.file, start, end, unescape(value[1:-1]))
        if kind == 'id':
            self.take()
            if value == 'true':
                return Bool(self.file, start, end, True)
            if value == 'false':
                return Bool(self.file, start, end, False)
            if value == 'undef':
                return Undef(self.file, start, end)
            return Id(self.file, start, end, value)
        if value == '(':
            self.take()
            inner = self.expr()
            self.take(')')
            return inner
        if value == '[':
            return self.vector()
        raise ParseError(f'Unexpected {value!r} in expression' if value else 'Unexpected end of file in expression', start, self.file)

    def vector(self):
        start = self.tok[2]
        self.take('[')
        if self.at(']'):
            self.take()
            return Vector(self.file, start, self.last_end(), [])
        first = self.element()
        if self.at(':') and not isinstance(first, (CompFor, CompForC, CompIf, CompLet, Each)):
            self.take()
            second = self.expr()
            if self.at(':'):
                self.take()
                third = self.expr()
                self.take(']')
                return Range(self.file, start, self.last_end(), first, second, third)
            self.take(']')
            return Range(self.file, start, self.last_end(), first, None, second)
        items = [first]
        while self.at(','):
            self.take()
            if self.at(']'):
                break
            items.append(self.element())
        if not self.at(']'):
            raise self.error(f'Expected "," or "]" in vector, found {self.tok[1]!r}')
        self.take(']')
        return Vector(self.file, start, self.last_end(), items)

    def element(self):
        """A vector element: an expression or a list-comprehension generator."""
        kind, value, start, _ = self.tok
        if kind == 'id' and self.peek()[1] == '(' and value in ('for', 'if', 'let'):
            self.take()
            if value == 'for':
                self.take('(')
                assigns = []
                while not self.at(')') and not self.at(';'):
                    if self.at(','):
                        self.take()
                        continue
                    name_tok = self.take()
                    self.take('=')
                    assigns.append(Assign(self.file, name_tok[2], self.last_end(), name_tok[1], self.expr()))
                    assigns[-1].end = self.last_end()
                if self.at(';'):
                    self.take()
                    cond = self.expr()
                    self.take(';')
                    update = []
                    while not self.at(')'):
                        if self.at(','):
                            self.take()
                            continue
                        name_tok = self.take()
                        self.take('=')
                        update.append(Assign(self.file, name_tok[2], self.last_end(), name_tok[1], self.expr()))
                    self.take(')')
                    body = self.element()
                    return CompForC(self.file, start, self.last_end(), assigns, cond, update, body)
                self.take(')')
                body = self.element()
                return CompFor(self.file, start, self.last_end(), assigns, body)
            if value == 'if':
                self.take('(')
                cond = self.expr()
                self.take(')')
                then = self.element()
                other = None
                if self.tok[0] == 'id' and self.tok[1] == 'else':
                    self.take()
                    other = self.element()
                return CompIf(self.file, start, self.last_end(), cond, then, other)
            assignments = self.assignments()
            body = self.element()
            return CompLet(self.file, start, self.last_end(), assignments, body)
        if kind == 'id' and value == 'each':
            self.take()
            return Each(self.file, start, self.last_end(), self.element())
        if kind == 'op' and value == '(' and self.peek()[0] == 'id' and self.peek()[1] in ('for', 'if', 'let', 'each'):
            self.take()
            inner = self.element()
            self.take(')')
            return inner
        return self.expr()


def parse(text, file=0):
    return Parser(text, file).parse_file()


def parse_expression(text, file=0):
    parser = Parser(text, file)
    value = parser.expr()
    if parser.tok[0] != 'eof':
        raise parser.error(f'Unexpected {parser.tok[1]!r}')
    return value


def walk(statements):
    """Yield every statement node, depth first."""
    for stmt in statements:
        yield stmt
        if isinstance(stmt, Instance):
            yield from walk(stmt.children)
        elif isinstance(stmt, Block):
            yield from walk(stmt.body)
        elif isinstance(stmt, If):
            yield from walk(stmt.then)
            if stmt.other:
                yield from walk(stmt.other)
        elif isinstance(stmt, ModuleDef) and stmt.body is not None:
            yield from walk([stmt.body])
