"""Vim emulation engine, independent of Qt.

The engine edits a buffer through three calls (``text``, ``replace``, ``undo``
/``redo``) and talks to the application through a :class:`Host`.  Keys are
strings: printable characters are themselves, special keys use Vim notation
(``<Esc>``, ``<CR>``, ``<BS>``, ``<C-r>``, ``<Up>`` ...).

Supported (counts work everywhere Vim accepts them):

* motions   h j k l w W b B e E ge gE 0 ^ $ g_ _ | + - <CR> gg G H M L { } ( )
            % [( [{ ]) ]} f F t T ; , / ? n N * # g* g# ` ' gj gk
            Ctrl-d Ctrl-u Ctrl-f Ctrl-b Ctrl-e Ctrl-y, Ctrl-o / Ctrl-i jumps
* operators d c y < > = g~ gu gU gc (comment) with any motion / text object
* objects   iw aw iW aW is as ip ap i( a( ib ab i{ a{ iB aB i[ a[ i< a< i" a" i' a' i` a`
* commands  x X s S r R J gJ D C Y p P gp gP ~ i a I A o O gi gI v V Ctrl-v gv
            u Ctrl-r . q @ @@ m Ctrl-a Ctrl-x & g& zz zt zb ZZ ZQ gd gD Ctrl-g
* registers "a-"z "A-"Z (append) "0-"9 "- "" "_ "+ "* "/ ": ". (clipboard=unnamedplus)
* insert    Ctrl-w Ctrl-u Ctrl-r{reg} Ctrl-o Ctrl-t Ctrl-d Ctrl-v(paste) auto-indent,
            electric braces, counts (3ifoo<Esc>)
* ex        :w :q :wq :x :{range}s/pat/rep/gi :& :g/pat/cmd :v/pat/cmd :d :y :m :t :co
            :j :> :< :normal :sort :noh :set :reg :marks :delm :pu :u :red :{line}
"""
from bisect import bisect_right
import re

SPECIAL = {'Esc', 'CR', 'BS', 'Del', 'Tab', 'S-Tab', 'Up', 'Down', 'Left', 'Right',
           'Home', 'End', 'PageUp', 'PageDown', 'Space', 'lt', 'Bar', 'Bslash', 'NL'}
OPERATORS = ('d', 'c', 'y', '<', '>', '=', 'g~', 'gu', 'gU', 'gc', '!')
CHANGES = set('xXsSrRJDCpP~iaIAoO.&') | {'gJ', 'gp', 'gP', '<C-a>', '<C-x>', 'g&'}
BRACKETS = {'(': ('(', ')'), ')': ('(', ')'), 'b': ('(', ')'), '{': ('{', '}'), '}': ('{', '}'),
            'B': ('{', '}'), '[': ('[', ']'), ']': ('[', ']'), '<': ('<', '>'), '>': ('<', '>')}
OPTION_ALIASES = {'ic': 'ignorecase', 'scs': 'smartcase', 'hls': 'hlsearch', 'is': 'incsearch',
                  'ws': 'wrapscan', 'sw': 'shiftwidth', 'ts': 'tabstop', 'et': 'expandtab',
                  'cb': 'clipboard', 'ai': 'autoindent', 'nu': 'number', 'rnu': 'relativenumber',
                  'sts': 'softtabstop'}


def parse_keys(text):
    """Turn ``"dw<Esc>"`` style notation into a key list."""
    keys = []
    i = 0
    while i < len(text):
        if text[i] == '<':
            j = text.find('>', i)
            name = text[i + 1:j] if j > 0 else ''
            if name in SPECIAL or re.fullmatch(r'[CS]-.', name or '') or re.fullmatch(r'C-S-.', name or ''):
                key = {'lt': '<', 'Space': ' ', 'Bar': '|', 'Bslash': '\\', 'NL': '<CR>'}.get(name)
                if key is None:
                    key = '<%s>' % (name[:2] + name[2:].lower() if name.startswith('C-') and len(name) == 3 else name)
                keys.append(key)
                i = j + 1
                continue
        keys.append('<CR>' if text[i] == '\n' else text[i])
        i += 1
    return keys


def keys_to_text(keys):
    return ''.join('<lt>' if k == '<' else k for k in keys)


def vim_regex(pattern, ignorecase=True, smartcase=True):
    """Translate a Vim (magic) pattern to a compiled Python regex."""
    flags = 0
    case = None
    if '\\c' in pattern:
        case, pattern = False, pattern.replace('\\c', '')
    if '\\C' in pattern:
        case, pattern = True, pattern.replace('\\C', '')
    very_magic = pattern.startswith('\\v')
    if very_magic:
        pattern = pattern[2:]
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == '\\' and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            i += 2
            if nxt in '<>':
                out.append(r'\b')
            elif not very_magic and nxt in '(){}|+?=':
                out.append({'=': '?'}.get(nxt, nxt))
            elif very_magic and nxt in '(){}|+?=<>':
                out.append('\\' + nxt)
            elif nxt == 'n':
                out.append('\n')
            elif nxt == 'e':
                out.append('\x1b')
            elif nxt in 'dDwWsSbBtrnfv.^$*[]\\/':
                out.append('\\' + nxt)
            elif nxt == 'a':
                out.append('[A-Za-z]')
            elif nxt == 'l':
                out.append('[a-z]')
            elif nxt == 'u':
                out.append('[A-Z]')
            elif nxt == 'x':
                out.append('[0-9A-Fa-f]')
            elif nxt == 'h':
                out.append('[A-Za-z_]')
            elif nxt.isdigit():
                out.append('\\' + nxt)
            else:
                out.append(re.escape(nxt))
            continue
        if very_magic:
            if ch in '<>':
                out.append(r'\b')
            elif ch == '=':
                out.append('?')
            else:
                out.append(ch)
        elif ch in '(){}|+?':
            out.append('\\' + ch)
        elif ch == '~':
            out.append('~')
        else:
            out.append(ch)
        i += 1
    if case is None:
        case = not ignorecase or (smartcase and any(c.isupper() for c in re.sub(r'\\.', '', pattern)))
    if not case:
        flags |= re.IGNORECASE
    return re.compile(''.join(out), flags | re.MULTILINE)


def vim_replacement(text):
    """Return a function implementing Vim's :s replacement string."""
    parts = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\\' and i + 1 < len(text):
            nxt = text[i + 1]
            i += 2
            if nxt.isdigit():
                parts.append(('group', int(nxt)))
            elif nxt in 'nr':
                parts.append(('text', '\n'))
            elif nxt == 't':
                parts.append(('text', '\t'))
            elif nxt in 'uUlLeE':
                parts.append(('case', nxt))
            else:
                parts.append(('text', nxt))
            continue
        if ch == '&':
            parts.append(('group', 0))
        elif ch == '~':
            parts.append(('text', '~'))
        else:
            parts.append(('text', ch))
        i += 1

    def apply(match):
        out = []
        mode = None
        once = None
        for kind, value in parts:
            if kind == 'case':
                if value in 'uU' and value == 'u':
                    once = 'u'
                elif value == 'l':
                    once = 'l'
                else:
                    mode = {'U': 'U', 'L': 'L'}.get(value)
                continue
            piece = (match.group(value) or '') if kind == 'group' else value
            if mode == 'U':
                piece = piece.upper()
            elif mode == 'L':
                piece = piece.lower()
            if once and piece:
                piece = (piece[0].upper() if once == 'u' else piece[0].lower()) + piece[1:]
                once = None
            out.append(piece)
        return ''.join(out)
    return apply


class Register:
    __slots__ = ('text', 'kind')

    def __init__(self, text='', kind='char'):
        self.text, self.kind = text, kind

    def __repr__(self):
        return f'Register({self.text!r}, {self.kind!r})'


class Host:
    """Callbacks from the engine to the application.  Every method is optional."""
    def clipboard_get(self): return None
    def clipboard_set(self, text): pass
    def message(self, text, error=False): pass
    def ex_command(self, command): return False
    def visible_lines(self): return (0, 39)
    def scroll(self, kind, amount=0): pass
    def goto_definition(self, word): return False
    def option_changed(self, name, value): pass
    def search_preview(self, start, end): pass
    def complete(self, forward=True): pass


class TextBuffer:
    """Plain Python buffer with snapshot undo, used by tests and headless tools."""
    def __init__(self, text=''):
        self._text = text
        self.undo_stack, self.redo_stack = [], []

    def text(self):
        return self._text

    def replace(self, a, b, s, join=False):
        if not (join and self.undo_stack):
            self.undo_stack.append((self._text, a))
        self.redo_stack.clear()
        self._text = self._text[:a] + s + self._text[b:]

    def undo(self):
        if not self.undo_stack:
            return None
        text, pos = self.undo_stack.pop()
        self.redo_stack.append((self._text, pos))
        self._text = text
        return pos

    def redo(self):
        if not self.redo_stack:
            return None
        text, pos = self.redo_stack.pop()
        self.undo_stack.append((self._text, pos))
        self._text = text
        return pos


class Abort(Exception):
    """A motion or command failed (Vim would beep); stops macros and repeats."""


def char_class(ch, big=False):
    if ch in ' \t\r\n\f\v':
        return 0
    if big:
        return 1
    return 2 if (ch.isalnum() or ch in '_$') else 1


class Vim:
    def __init__(self, buffer, host=None):
        self.buf = buffer
        self.host = host or Host()
        self.mode = 'NORMAL'
        self.pos = 0
        self.anchor = 0
        self.want_col = 0
        self.keys = []
        self.registers = {}
        self.marks = {}
        self.anchors = []
        self.options = dict(ignorecase=True, smartcase=True, hlsearch=True, incsearch=True, wrapscan=True,
                            shiftwidth=4, tabstop=4, softtabstop=4, expandtab=True, clipboard='unnamedplus',
                            autoindent=True, number=True, relativenumber=False)
        self.last_search = None
        self.search_forward = True
        self.highlight = True
        self.last_find = None
        self.last_change = None
        self.change = None
        self.recording = None
        self.macro_keys = []
        self.last_macro = None
        self.cmdline = ''
        self.cmdtype = None
        self.cmd_history = {':': [], '/': []}
        self.history_index = None
        self.jumps = []
        self.jump_index = 0
        self.insert = None
        self.last_insert = None
        self.join = False
        self.last_visual = None
        self.last_sub = None
        self.pending_search_op = None
        self.oneshot = False
        self.depth = 0
        self.message = ''
        self._t = None
        self._ls = None
        self.dirty_view = True

    # ------------------------------------------------------------------ text
    @property
    def t(self):
        if self._t is None:
            self._t = self.buf.text()
            self._ls = None
        return self._t

    def refresh(self):
        self._t = None
        self._ls = None

    def lines(self):
        if self._ls is None:
            t = self.t
            ls = [0]
            find = t.find
            i = find('\n')
            while i >= 0:
                ls.append(i + 1)
                i = find('\n', i + 1)
            self._ls = ls
        return self._ls

    def line_of(self, p):
        return bisect_right(self.lines(), p) - 1

    def line_start(self, n):
        return self.lines()[n]

    def line_end(self, n):
        ls = self.lines()
        return ls[n + 1] - 1 if n + 1 < len(ls) else len(self.t)

    def line_text(self, n):
        return self.t[self.line_start(n):self.line_end(n)]

    def last_line(self):
        n = len(self.lines()) - 1
        if n > 0 and self.t.endswith('\n'):
            n -= 1
        return n

    def col(self, p):
        return p - self.line_start(self.line_of(p))

    def first_nonblank(self, n):
        t, p, e = self.t, self.line_start(n), self.line_end(n)
        while p < e and t[p] in ' \t':
            p += 1
        return p

    def indent_of(self, n):
        return self.t[self.line_start(n):self.first_nonblank(n)]

    def clamp(self, p, insert=False):
        t = self.t
        p = max(0, min(p, len(t)))
        if insert or self.mode in ('INSERT', 'REPLACE'):
            return p
        n = self.line_of(p)
        if n > self.last_line():
            n = self.last_line()
            p = self.line_start(n)
        s, e = self.line_start(n), self.line_end(n)
        if e > s and p >= e:
            return e - 1
        return max(s, p)

    def col_pos(self, n, col=None):
        s, e = self.line_start(n), self.line_end(n)
        col = self.want_col if col is None else col
        limit = e - s if self.mode in ('INSERT', 'REPLACE') else max(0, e - s - 1)
        return s + int(min(col, limit))

    def replace(self, a, b, s):
        if a == b and not s:
            return
        old = self.t
        if old[a:b] == s:
            return
        self.buf.replace(a, b, s, self.join)
        self.join = True
        self._t = old[:a] + s + old[b:]
        self._ls = None
        delta = len(s) - (b - a)

        def shift(v):
            if v >= b:
                return v + delta
            if v > a:
                return a
            return v
        for k in list(self.marks):
            self.marks[k] = shift(self.marks[k])
        self.anchors = [shift(v) for v in self.anchors]
        self.jumps = [shift(v) for v in self.jumps]
        self.marks['['] = a
        self.marks[']'] = max(a, a + len(s) - 1)
        self.marks['.'] = a

    # ------------------------------------------------------------ registers
    def get_register(self, name=None):
        if name in (None, '"'):
            if self.options['clipboard'] and self.host:
                clip = self.host.clipboard_get()
                own = self.registers.get('"')
                if clip is not None and (own is None or clip != own.text):
                    return Register(clip, 'line' if clip.endswith('\n') else 'char')
            return self.registers.get('"')
        if name in '+*':
            clip = self.host.clipboard_get()
            if clip is None:
                return self.registers.get(name)
            own = self.registers.get(name)
            if own is not None and own.text == clip:
                return own
            return Register(clip, 'line' if clip.endswith('\n') else 'char')
        if name == '/':
            return Register(self.last_search or '')
        if name == ':':
            return Register(self.cmd_history[':'][-1] if self.cmd_history[':'] else '')
        if name == '.':
            return Register(self.last_insert or '')
        return self.registers.get(name.lower())

    def store(self, name, text, kind, op):
        if name == '_':
            return
        reg = Register(text, kind)
        if name and name.isalpha() and name.isupper():
            old = self.registers.get(name.lower())
            if old:
                if old.kind == 'line' or kind == 'line':
                    reg = Register(old.text.rstrip('\n') + '\n' + text if old.kind != 'line' else old.text + text, 'line')
                    if not reg.text.endswith('\n'):
                        reg.text += '\n'
                else:
                    reg = Register(old.text + text, 'char')
            self.registers[name.lower()] = reg
        elif name and name not in '"':
            self.registers[name] = reg
            if name in '+*':
                self.host.clipboard_set(text)
        else:
            if op == 'y':
                self.registers['0'] = reg
            elif kind == 'line' or '\n' in text:
                for i in range(9, 1, -1):
                    if str(i - 1) in self.registers:
                        self.registers[str(i)] = self.registers[str(i - 1)]
                self.registers['1'] = reg
            else:
                self.registers['-'] = reg
            if self.options['clipboard']:
                self.host.clipboard_set(text)
        self.registers['"'] = reg

    # ----------------------------------------------------------------- input
    def feed_keys(self, keys):
        if isinstance(keys, str):
            keys = parse_keys(keys)
        for key in keys:
            self.feed(key)

    def feed(self, key):
        self.refresh()
        self.message = ''
        if self.recording is not None:
            self.macro_keys.append(key)
        try:
            if self.mode in ('INSERT', 'REPLACE'):
                self.insert_key(key)
            elif self.mode == 'COMMAND':
                self.command_key(key)
            else:
                self.keys.append(key)
                self.normal()
        except Abort as error:
            self.keys = []
            if str(error):
                self.msg(str(error), True)
            if self.depth:
                raise
        self.dirty_view = True

    def msg(self, text, error=False):
        self.message = text
        self.host.message(text, error)

    def set_mode(self, mode):
        if self.mode in ('VISUAL', 'VISUAL_LINE', 'VISUAL_BLOCK') and mode not in ('VISUAL', 'VISUAL_LINE', 'VISUAL_BLOCK'):
            self.last_visual = (self.mode, self.anchor, self.pos)
            a, b = sorted((self.anchor, self.pos))
            self.marks['<'], self.marks['>'] = a, b
        self.mode = mode

    @property
    def mode_label(self):
        if self.mode == 'COMMAND':
            return self.cmdtype + self.cmdline
        label = {'VISUAL_LINE': 'V-LINE', 'VISUAL_BLOCK': 'V-BLOCK'}.get(self.mode, self.mode)
        if self.mode in ('INSERT', 'REPLACE') and self.oneshot:
            label = '(insert)'
        if self.recording is not None:
            label += ' · recording @' + self.recording
        pending = ''.join(self.keys)
        return label + ('  ' + pending if pending else '')

    # ----------------------------------------------------------- parsing
    @staticmethod
    def read_count(keys, i):
        digits = ''
        while i < len(keys) and len(keys[i]) == 1 and keys[i].isdigit() and (keys[i] != '0' or digits):
            digits += keys[i]
            i += 1
        return (min(int(digits), 99999) if digits else None), i

    def read_cmd(self, keys, i, pending=False):
        """Return ((name, arg), next) | (None, i) if incomplete; raise Abort if invalid."""
        if i >= len(keys):
            return None, i
        k = keys[i]
        visual = self.mode.startswith('VISUAL')
        if k == 'q' and self.recording is not None and not pending:
            return ('q', None), i + 1
        if k in ('f', 'F', 't', 'T', 'r', 'm', "'", '`', 'q', '@'):
            if k in 'qr' and pending:
                raise Abort()
            if i + 1 >= len(keys):
                return None, i
            arg = keys[i + 1]
            if arg == '<Esc>':
                raise Abort()
            if arg == '<CR>' and k in 'r':
                arg = '\n'
            elif arg == '<Tab>':
                arg = '\t'
            elif len(arg) != 1:
                raise Abort()
            return (k, arg), i + 2
        if k in ('g', 'z', 'Z', '[', ']') or ((pending or visual) and k in ('i', 'a')):
            if i + 1 >= len(keys):
                return None, i
            nxt = keys[i + 1]
            if k == 'g' and nxt in ('~', 'u', 'U', 'c', '?') and pending and False:
                pass
            return (k + nxt, None), i + 2
        return (k, None), i + 1

    def normal(self):
        keys = self.keys
        i = 0
        reg = None
        if keys[0] == '"':
            if len(keys) < 2:
                return
            reg = keys[1]
            if len(reg) != 1:
                raise Abort()
            i = 2
        count, i = self.read_count(keys, i)
        start_cmd = i
        cmd, i = self.read_cmd(keys, i)
        if cmd is None:
            return
        name, arg = cmd
        visual = self.mode.startswith('VISUAL')
        if not visual and name in OPERATORS:
            count2, j = self.read_count(keys, i)
            if j >= len(keys):
                return
            if keys[j] in ('v', 'V', '<C-v>'):
                force = keys[j]
                j += 1
            else:
                force = None
            if j >= len(keys):
                return
            # doubled operator: dd cc yy >> << == g~~ g~g~ guu gUU gcc
            doubled = None
            if keys[j] == name[-1] and (len(name) == 1 or keys[j] in '~uUc'):
                doubled, j = True, j + 1
            elif len(name) == 2 and j + 1 < len(keys) and keys[j] + keys[j + 1] == name:
                doubled, j = True, j + 2
            elif len(name) == 2 and keys[j] == 'g' and j + 1 >= len(keys):
                return
            total = (count or 1) * (count2 or 1)
            given = count is not None or count2 is not None
            if doubled:
                self.keys = []
                self.begin()
                n = self.line_of(self.pos)
                last = min(self.last_line(), n + total - 1)
                self.apply_operator(name, self.line_start(n), self.line_start(last), 'line', reg)
                self.record(reg, total if given else None, [name, name[-1]] if len(name) == 2 else [name, name], name)
                return
            mcmd, k = self.read_cmd(keys, j, pending=True)
            if mcmd is None:
                return
            self.keys = []
            self.begin()
            mname, marg = mcmd
            if mname in ('/', '?'):
                self.pending_search_op = (name, reg, total, force, keys[start_cmd:j])
                self.start_cmdline(mname)
                return
            result = self.motion(mname, marg, total if given else None, name)
            if result is None:
                raise Abort()
            self.operate(name, result, reg, force, mname)
            self.record(reg, total if given else None, keys[start_cmd:i] + keys[j:k], name)
            return
        self.keys = []
        self.begin()
        if visual:
            self.visual_command(name, arg, count, reg)
            return
        if self.motion_known(name):
            result = self.motion(name, arg, count, None)
            if result is None:
                raise Abort()
            self.move_to(result, name)
            if self.oneshot:
                self.finish_oneshot()
            return
        self.command(name, arg, count, reg, keys[start_cmd:])
        if self.oneshot and self.mode == 'NORMAL':
            self.finish_oneshot()

    def begin(self):
        self.join = False

    def record(self, reg, count, keys, name):
        if name == 'y' or self.depth_replaying_dot():
            pass
        if name == 'y':
            return
        change = dict(reg=reg, count=count, keys=list(keys))
        if self.mode in ('INSERT', 'REPLACE') and self.insert is not None:
            self.insert['change'] = change
        else:
            self.last_change = change

    def depth_replaying_dot(self):
        return False

    # ------------------------------------------------------------- motions
    MOTIONS = set('hjklwWbBeE0^$_|+-GHML{}()%;,nN*#`\'fFtT') | {
        '<Left>', '<Right>', '<Up>', '<Down>', '<BS>', ' ', '<CR>', '<Home>', '<End>', 'gg', 'ge', 'gE',
        'g_', 'gj', 'gk', 'g*', 'g#', '[(', '[{', '])', ']}', 'g0', 'g$', 'gm', '<C-n>', '<C-p>', '<C-j>',
        'gd', 'gD', 'go'}

    def motion_known(self, name):
        return name in self.MOTIONS or (len(name) == 2 and name[0] in 'ia' and self.mode.startswith('VISUAL'))

    def motion(self, name, arg, count, op):
        """Return (start, end, kind) where kind is excl / incl / line; or None."""
        t = self.t
        p = self.pos
        n = count or 1
        cur = self.line_of(p)
        if len(name) == 2 and name[0] in 'ia' and (op or self.mode.startswith('VISUAL')):
            return self.text_object(name[1], name[0] == 'i', n)
        if name in ('h', '<Left>', '<BS>', '<C-h>'):
            if name == '<BS>' and op is None:
                q = p
                for _ in range(n):
                    if q == 0:
                        break
                    q -= 1
                return (p, q, 'excl')
            s = self.line_start(cur)
            if p == s and op is None:
                return None
            return (p, max(s, p - n), 'excl')
        if name in ('l', '<Right>', ' '):
            e = self.line_end(cur)
            if name == ' ' and op is None:
                return (p, min(len(t), p + n), 'excl')
            limit = e if (op or self.mode.startswith('VISUAL')) else max(self.line_start(cur), e - 1)
            if p >= limit and op is None:
                return None
            return (p, min(limit, p + n), 'excl')
        if name in ('j', '<Down>', 'gj', '<C-n>', '<C-j>'):
            if cur + n > self.last_line():
                if cur >= self.last_line():
                    return None
                n = self.last_line() - cur
            return (p, self.col_pos(cur + n), 'line')
        if name in ('k', '<Up>', 'gk', '<C-p>'):
            if cur - n < 0:
                if cur == 0:
                    return None
                n = cur
            return (p, self.col_pos(cur - n), 'line')
        if name in ('0', '<Home>', 'g0'):
            return (p, self.line_start(cur), 'excl')
        if name == '^':
            return (p, self.first_nonblank(cur), 'excl')
        if name in ('$', '<End>', 'g$'):
            target = min(self.last_line(), cur + n - 1)
            e = self.line_end(target)
            s = self.line_start(target)
            return (p, max(s, e - 1) if op is None or e > s else e, 'incl' if e > s else 'excl')
        if name == 'g_':
            target = min(self.last_line(), cur + n - 1)
            s, e = self.line_start(target), self.line_end(target)
            while e > s and t[e - 1] in ' \t':
                e -= 1
            return (p, max(s, e - 1), 'incl')
        if name == 'gm':
            s, e = self.line_start(cur), self.line_end(cur)
            return (p, s + (e - s) // 2, 'excl')
        if name == '|':
            s, e = self.line_start(cur), self.line_end(cur)
            return (p, min(s + n - 1, max(s, e - 1)), 'excl')
        if name in ('_',):
            target = min(self.last_line(), cur + n - 1)
            return (p, self.first_nonblank(target), 'line')
        if name in ('+', '<CR>'):
            if cur + n > self.last_line():
                return None
            return (p, self.first_nonblank(cur + n), 'line')
        if name == '-':
            if cur - n < 0:
                return None
            return (p, self.first_nonblank(cur - n), 'line')
        if name == 'G':
            target = self.last_line() if count is None else min(self.last_line(), max(0, n - 1))
            self.push_jump()
            return (p, self.first_nonblank(target), 'line')
        if name == 'gg':
            target = 0 if count is None else min(self.last_line(), max(0, n - 1))
            self.push_jump()
            return (p, self.first_nonblank(target), 'line')
        if name == 'go':
            return (p, min(len(t), max(0, n - 1)), 'excl')
        if name in ('H', 'L', 'M'):
            first, last = self.host.visible_lines()
            last = min(last, self.last_line())
            first = min(first, last)
            if name == 'H':
                target = min(last, first + n - 1)
            elif name == 'L':
                target = max(first, last - n + 1)
            else:
                target = (first + last) // 2
            self.push_jump()
            return (p, self.first_nonblank(target), 'line')
        if name in ('w', 'W'):
            q = p
            for _ in range(n):
                if q >= len(t):
                    if op is None:
                        return None if q == p else (p, q, 'excl')
                    break
                q = self.word_forward(q, name == 'W')
            if op is None:
                q = min(q, len(t))
                if q >= len(t):
                    q = max(0, len(t) - 1)
            return (p, q, 'excl')
        if name in ('b', 'B'):
            if p == 0:
                return None
            q = p
            for _ in range(n):
                q = self.word_backward(q, name == 'B')
            return (p, q, 'excl')
        if name in ('e', 'E'):
            q = p
            for _ in range(n):
                q = self.word_end(q, name == 'E')
            if q >= len(t):
                return None
            return (p, q, 'incl')
        if name in ('ge', 'gE'):
            q = p
            for _ in range(n):
                q = self.word_end_backward(q, name == 'gE')
            return (p, q, 'incl')
        if name in ('f', 'F', 't', 'T', ';', ','):
            if name in (';', ','):
                if not self.last_find:
                    return None
                kind, ch = self.last_find
                if name == ',':
                    kind = kind.swapcase()
                repeat = True
            else:
                kind, ch = name, arg
                self.last_find = (kind, ch)
                repeat = False
            return self.find_char(kind, ch, n, repeat)
        if name in ('}', '{'):
            q = cur
            last = self.last_line()
            for _ in range(n):
                if name == '}':
                    while q < last and self.line_start(q) == self.line_end(q):
                        q += 1
                    while q < last and self.line_start(q) != self.line_end(q):
                        q += 1
                else:
                    while q > 0 and self.line_start(q) == self.line_end(q):
                        q -= 1
                    while q > 0 and self.line_start(q) != self.line_end(q):
                        q -= 1
            self.push_jump()
            if name == '}' and q == last and self.line_start(q) != self.line_end(q):
                end = self.line_end(q)
                return (p, end if op else max(self.line_start(q), end - 1), 'excl' if op else 'incl')
            return (p, self.line_start(q), 'excl')
        if name in (')', '('):
            q = p
            for _ in range(n):
                q = self.sentence(q, name == ')')
            return (p, q, 'excl')
        if name == '%':
            if count is not None:
                target = min(self.last_line(), (n * (self.last_line() + 1) + 99) // 100 - 1)
                self.push_jump()
                return (p, self.first_nonblank(max(0, target)), 'line')
            e = self.line_end(cur)
            q = p
            while q < e and t[q] not in '()[]{}':
                q += 1
            if q >= e:
                return None
            m = self.match_bracket(q)
            if m is None:
                return None
            self.push_jump()
            return (p, m, 'incl')
        if name in ('[(', '[{'):
            o, c = BRACKETS[name[1]]
            q = self.open_before(o, c, p - 1 if p < len(t) and t[p] == o else p, n) if not (p < len(t) and t[p] == c) else self.open_before(o, c, p - 1, n)
            q = self.unmatched_open(o, c, p, n)
            return None if q is None else (p, q, 'excl')
        if name in ('])', ']}'):
            o, c = BRACKETS[name[1]]
            q = self.unmatched_close(o, c, p, n)
            return None if q is None else (p, q, 'excl')
        if name in ('n', 'N'):
            if not self.last_search:
                raise Abort('E35: No previous regular expression')
            forward = self.search_forward if name == 'n' else not self.search_forward
            q = self.search(self.last_search, forward, p, n)
            if q is None:
                return None
            self.highlight = True
            self.push_jump()
            return (p, q, 'excl')
        if name in ('*', '#', 'g*', 'g#'):
            word = self.word_under_cursor()
            if not word:
                raise Abort('E348: No string under cursor')
            escaped = re.sub(r'([\\/.*$^~\[\]])', r'\\\1', word)
            pattern = escaped if name.startswith('g') or not re.match(r'\w', word) else '\\<' + escaped + '\\>'
            self.last_search = pattern
            self.search_forward = name.endswith('*')
            self.push_search_history(pattern)
            start = self.word_start(p)
            q = self.search(pattern, self.search_forward, start, n)
            if q is None:
                return None
            self.highlight = True
            self.push_jump()
            return (p, q, 'excl')
        if name in ('`', "'"):
            if arg in ('`', "'"):
                q = self.jumps[-1] if self.jumps else 0
                self.push_jump(force=True)
            else:
                if arg == '"':
                    arg = '.'
                if arg not in self.marks:
                    raise Abort('E20: Mark not set')
                q = self.marks[arg]
                self.push_jump()
            q = min(q, len(t))
            if name == "'":
                return (p, self.first_nonblank(self.line_of(q)), 'line')
            return (p, q, 'excl')
        if name in ('gd', 'gD'):
            word = self.word_under_cursor()
            if not word:
                return None
            pattern = re.compile(r'(?:\bmodule|\bfunction)\s+' + re.escape(word) + r'\b|^\s*' + re.escape(word) + r'\s*=', re.M)
            m = pattern.search(t)
            if not m:
                m = re.compile(r'\b' + re.escape(word) + r'\b').search(t)
            if not m:
                return None
            self.push_jump()
            q = m.start() + m.group(0).rfind(word) if word in m.group(0) else m.start()
            return (p, q, 'excl')
        return None

    def move_to(self, result, name=None):
        start, target, kind = result
        if self.mode.startswith('VISUAL'):
            self.pos = self.clamp(target) if target < len(self.t) or not self.t else max(0, len(self.t) - (0 if self.t.endswith('\n') else 1))
            self.pos = min(self.pos, max(0, len(self.t)))
        else:
            self.pos = self.clamp(target)
        if name not in ('j', 'k', '<Up>', '<Down>', 'gj', 'gk', '<C-n>', '<C-p>', '<C-j>'):
            self.want_col = float('inf') if name in ('$', '<End>', 'g$') else self.col(self.pos)

    def word_forward(self, p, big):
        t = self.t
        n = len(t)
        c = char_class(t[p], big)
        if c:
            while p < n and char_class(t[p], big) == c:
                p += 1
        while p < n and char_class(t[p], big) == 0:
            if t[p] == '\n' and p + 1 < n and t[p + 1] == '\n':
                return p + 1
            p += 1
        return p

    def word_backward(self, p, big):
        t = self.t
        p -= 1
        while p > 0 and char_class(t[p], big) == 0:
            if t[p] == '\n' and t[p - 1] == '\n':
                return p
            p -= 1
        if p <= 0:
            return 0
        c = char_class(t[p], big)
        while p > 0 and char_class(t[p - 1], big) == c:
            p -= 1
        return p

    def word_end(self, p, big):
        t = self.t
        n = len(t)
        p += 1
        while p < n and char_class(t[p], big) == 0:
            p += 1
        if p >= n:
            return n
        c = char_class(t[p], big)
        while p + 1 < n and char_class(t[p + 1], big) == c:
            p += 1
        return p

    def word_end_backward(self, p, big):
        t = self.t
        if p >= len(t):
            p = len(t) - 1
        c = char_class(t[p], big) if p >= 0 else 0
        if c:
            while p >= 0 and char_class(t[p], big) == c:
                p -= 1
        while p >= 0 and char_class(t[p], big) == 0:
            if t[p] == '\n' and p > 0 and t[p - 1] == '\n':
                return p
            p -= 1
        return max(p, 0)

    def word_start(self, p):
        t = self.t
        if p >= len(t):
            return p
        c = char_class(t[p])
        while p > 0 and char_class(t[p - 1]) == c and c:
            p -= 1
        return p

    def word_under_cursor(self, big=False):
        t = self.t
        p = self.pos
        e = self.line_end(self.line_of(p))
        while p < e and char_class(t[p], big) != 2 and not big:
            p += 1
        if p >= e:
            p = self.pos
            while p < e and char_class(t[p], big) == 0:
                p += 1
            if p >= e:
                return ''
        c = char_class(t[p], big)
        a = b = p
        s = self.line_start(self.line_of(p))
        while a > s and char_class(t[a - 1], big) == c:
            a -= 1
        while b < e and char_class(t[b], big) == c:
            b += 1
        return t[a:b]

    def find_char(self, kind, ch, n, repeat=False):
        t = self.t
        p = self.pos
        cur = self.line_of(p)
        s, e = self.line_start(cur), self.line_end(cur)
        q = p
        forward = kind in 'ft'
        if repeat and kind in 'tT':
            q += 1 if forward else -1
        for _ in range(n):
            q = t.find(ch, q + 1, e) if forward else t.rfind(ch, s, max(s, q))
            if q < 0:
                return None
        if kind == 't':
            q -= 1
        elif kind == 'T':
            q += 1
        return (p, q, 'incl' if forward else 'excl')

    def sentence(self, p, forward):
        t = self.t
        ends = [m.end() for m in re.finditer(r'[.!?][)\]"\']*(?:[ \t]+|\n|$)|\n\s*\n', t)]
        starts = sorted(set([0] + [e + len(t[e:]) - len(t[e:].lstrip()) for e in ends]))
        if forward:
            for s in starts:
                if s > p:
                    return min(s, len(t))
            return len(t)
        for s in reversed(starts):
            if s < p:
                return s
        return 0

    def match_bracket(self, q):
        t = self.t
        ch = t[q]
        pairs = {'(': ')', '[': ']', '{': '}'}
        if ch in pairs:
            o, c = ch, pairs[ch]
            depth = 0
            for i in range(q, len(t)):
                if t[i] == o:
                    depth += 1
                elif t[i] == c:
                    depth -= 1
                    if depth == 0:
                        return i
            return None
        o = {')': '(', ']': '[', '}': '{'}[ch]
        depth = 0
        for i in range(q, -1, -1):
            if t[i] == ch:
                depth += 1
            elif t[i] == o:
                depth -= 1
                if depth == 0:
                    return i
        return None

    def open_before(self, o, c, p, count):
        return self.unmatched_open(o, c, p + 1, count)

    def unmatched_open(self, o, c, p, count):
        """Opening bracket of the count-th enclosing pair of p (not p itself)."""
        t = self.t
        depth = 0
        i = p - 1
        while i >= 0:
            ch = t[i]
            if ch == c:
                depth += 1
            elif ch == o:
                if depth == 0:
                    count -= 1
                    if count == 0:
                        return i
                else:
                    depth -= 1
            i -= 1
        return None

    def unmatched_close(self, o, c, p, count):
        t = self.t
        depth = 0
        i = p + 1
        while i < len(t):
            ch = t[i]
            if ch == o:
                depth += 1
            elif ch == c:
                if depth == 0:
                    count -= 1
                    if count == 0:
                        return i
                else:
                    depth -= 1
            i += 1
        return None

    def enclosing(self, o, c, p, count):
        """(open, close) of the count-th pair containing p, inclusive positions."""
        t = self.t
        if p < len(t) and t[p] == o:
            count -= 1
            if count == 0:
                close = self.unmatched_close(o, c, p, 1)
                return None if close is None else (p, close)
            start = self.unmatched_open(o, c, p, count)
        elif p < len(t) and t[p] == c:
            start = self.unmatched_open(o, c, p, count)
        else:
            start = self.unmatched_open(o, c, p, count)
        if start is None:
            return None
        close = self.unmatched_close(o, c, start, 1)
        return None if close is None else (start, close)

    # -------------------------------------------------------- text objects
    def text_object(self, kind, inner, count):
        """Return (start, end_exclusive, kind) — kind 'char' or 'line' — as a 'range' tuple."""
        t = self.t
        p = min(self.pos, max(0, len(t) - 1)) if t else 0
        if kind in ('w', 'W'):
            big = kind == 'W'
            cur = self.line_of(p)
            s, e = self.line_start(cur), self.line_end(cur)
            if s == e:
                return ('range', s, min(len(t), e + 1) if not inner else e, 'char')
            a = b = p
            for i in range(count):
                c = char_class(t[b], big) if b < e else 0
                if i == 0:
                    while a > s and char_class(t[a - 1], big) == c:
                        a -= 1
                while b < e and char_class(t[b], big) == c:
                    b += 1
                if not inner:
                    if c != 0:
                        if b < e and char_class(t[b], big) == 0:
                            while b < e and char_class(t[b], big) == 0:
                                b += 1
                        elif i == 0:
                            while a > s and char_class(t[a - 1], big) == 0:
                                a -= 1
                    else:
                        c2 = char_class(t[b], big) if b < e else 0
                        while b < e and char_class(t[b], big) == c2 and c2:
                            b += 1
                if b >= e:
                    break
            return ('range', a, b, 'char')
        if kind in ('s',):
            a = self.sentence(p + 1, False)
            b = self.sentence(p, True)
            if inner:
                while b > a and t[b - 1] in ' \t\n':
                    b -= 1
            return ('range', a, b, 'char')
        if kind == 'p':
            cur = self.line_of(p)
            last = self.last_line()

            def blank(n):
                return self.line_text(n).strip() == ''
            state = blank(cur)
            a = cur
            while a > 0 and blank(a - 1) == state:
                a -= 1
            b = cur
            for i in range(count):
                while b < last and blank(b + 1) == state:
                    b += 1
                if i < count - 1 and b < last:
                    b += 1
                    state = blank(b)
            if not inner:
                if b < last and not blank(cur):
                    b += 1
                    while b < last and blank(b + 1):
                        b += 1
                elif a > 0:
                    while a > 0 and blank(a - 1) != blank(cur):
                        a -= 1
            return ('range', self.line_start(a), self.line_start(b), 'line')
        if kind in BRACKETS:
            o, c = BRACKETS[kind]
            pair = self.enclosing(o, c, p, count)
            if pair is None:
                return None
            a, b = pair
            if not inner:
                return ('range', a, b + 1, 'char')
            a += 1
            if t[a:b].strip() == '' and '\n' not in t[a:b]:
                return ('range', a, b, 'char')
            if a < len(t) and t[a] == '\n':
                # Multi-line block: the inner lines are operated on linewise.
                first = a + 1
                close_line_start = self.line_start(self.line_of(b))
                if t[close_line_start:b].strip() == '' and close_line_start > first:
                    return ('range', first, close_line_start, 'linesel')
                a = first
            return ('range', a, b, 'char')
        if kind in ('"', "'", '`'):
            cur = self.line_of(p)
            s, e = self.line_start(cur), self.line_end(cur)
            quotes = []
            i = s
            while i < e:
                if t[i] == '\\':
                    i += 2
                    continue
                if t[i] == kind:
                    quotes.append(i)
                i += 1
            pair = None
            for a, b in zip(quotes[::2], quotes[1::2]):
                if a <= p <= b:
                    pair = (a, b)
                    break
            if pair is None:
                pair = next(((a, b) for a, b in zip(quotes[::2], quotes[1::2]) if a > p), None)
            if pair is None:
                return None
            a, b = pair
            if inner:
                return ('range', a + 1, b, 'char')
            b += 1
            if b < e and t[b] in ' \t':
                while b < e and t[b] in ' \t':
                    b += 1
            else:
                while a > s and t[a - 1] in ' \t':
                    a -= 1
            return ('range', a, b, 'char')
        return None

    # ------------------------------------------------------------ operators
    def operate(self, op, result, reg, force=None, motion_name=None):
        t = self.t
        if result[0] == 'range':
            _, a, b, kind = result
            if kind == 'linesel':
                self.apply_operator(op, a, max(a, b - 1), 'line', reg, inner_block=True)
                return
            if kind == 'line':
                self.apply_operator(op, a, b, 'line', reg)
                return
            if force == 'V':
                self.apply_operator(op, a, max(a, b - 1), 'line', reg)
                return
            if b <= a and op != 'c':
                return
            self.apply_operator(op, a, b, 'char', reg)
            return
        start, target, kind = result
        if motion_name in ('w', 'W') and op == 'c' and start < len(t) and char_class(t[start]) != 0:
            q = start
            # cw on a word behaves like ce (and on the last char of a word, like cl)
            end = self.word_end(q - 1, motion_name == 'W') if q > 0 else self.word_end(-1, motion_name == 'W')
            if char_class(t[q], motion_name == 'W') != char_class(t[min(q + 1, len(t) - 1)], motion_name == 'W') or q + 1 >= len(t):
                end = q
            count_end = target
            if count_end > start:
                # with counts, use the end of the word before the count-th word start
                q2 = start
                ends = []
                while q2 < count_end:
                    q2 = self.word_end(q2 if ends else q2 - 1, motion_name == 'W') if True else q2
                    ends.append(q2)
                    if len(ends) > 10000:
                        break
                    nxt = self.word_forward(q2, motion_name == 'W')
                    if nxt >= count_end:
                        break
                    q2 = nxt
                end = ends[-1] if ends else end
            target, kind = end, 'incl'
        elif motion_name in ('w', 'W') and op:
            if self.line_of(target) > self.line_of(start):
                nl = t.rfind('\n', start, target)
                if nl >= start and t[nl + 1:target].strip() == '':
                    target = nl if nl > start else nl
                    if target == start and op != 'c':
                        target = nl
            target = min(target, len(t))
        if force == 'v':
            kind = 'excl' if kind in ('incl', 'line') else 'incl'
        elif force == 'V':
            kind = 'line'
        a, b = sorted((start, target))
        if kind == 'line':
            self.apply_operator(op, a, b, 'line', reg)
            return
        if kind == 'incl':
            b = min(len(t), b + 1)
        elif b > a and self.col(b) == 0 and self.line_of(b) > self.line_of(a):
            # exclusive motion ending in column 0
            if a <= self.first_nonblank(self.line_of(a)) and motion_name not in ('w', 'W'):
                self.apply_operator(op, a, b - 1, 'line', reg)
                return
            b -= 1
        self.apply_operator(op, a, b, 'char', reg)

    def line_span(self, a, b):
        """Line range [l1, l2] for positions a..b -> (start, end_exclusive, text)."""
        l1, l2 = self.line_of(a), self.line_of(b)
        l2 = min(l2, max(l1, self.last_line()))
        s = self.line_start(l1)
        e = self.line_end(l2)
        text = self.t[s:e] + '\n'
        return l1, l2, s, e, text

    def apply_operator(self, op, a, b, kind, reg, inner_block=False, count=1):
        t = self.t
        orig = self.pos
        if kind == 'line':
            l1, l2, s, e, text = self.line_span(a, b)
            if op in ('d', 'c', 'y'):
                self.store(reg, text, 'line', op)
            if op == 'y':
                self.pos = orig if self.line_of(orig) == l1 else self.clamp(s if a < orig else self.col_pos(l1))
                if a < orig:
                    self.pos = self.clamp(a if self.line_of(a) == l1 and kind != 'line' else self.line_start(l1) + min(self.col(orig), max(0, e - s - 1)))
                self.msg(f'{l2 - l1 + 1} lines yanked' if l2 > l1 else '')
                return
            if op == 'd':
                if e < len(t):
                    self.replace(s, e + 1, '')
                elif s > 0:
                    self.replace(s - 1, e, '')
                else:
                    self.replace(s, e, '')
                n = min(l1, self.last_line())
                self.pos = self.first_nonblank(n)
                self.want_col = self.col(self.pos)
                if l2 - l1 >= 2:
                    self.msg(f'{l2 - l1 + 1} fewer lines')
                return
            if op == 'c':
                indent = self.indent_of(l1) if self.options['autoindent'] else ''
                if inner_block and l1 > 0:
                    indent = self.indent_of(l1)
                self.replace(s, e, indent)
                self.pos = s + len(indent)
                self.start_insert()
                return
            if op in ('<', '>'):
                self.shift_lines(l1, l2, 1 if op == '>' else -1, count)
                self.pos = self.first_nonblank(l1)
                return
            if op == '=':
                self.reindent(l1, l2)
                self.pos = self.first_nonblank(l1)
                return
            if op == 'gc':
                self.toggle_comment(l1, l2)
                self.pos = self.first_nonblank(l1)
                return
            if op in ('g~', 'gu', 'gU'):
                self.change_case(op, s, e)
                self.pos = self.clamp(a if self.line_of(a) == l1 and a > s else s)
                return
            return
        # characterwise
        text = t[a:b]
        if op in ('d', 'c', 'y'):
            self.store(reg, text, 'char', op)
        if op == 'y':
            self.pos = self.clamp(min(a, orig) if a < orig else orig)
            if a < orig:
                self.pos = self.clamp(a)
            return
        if op == 'd':
            self.replace(a, b, '')
            self.pos = self.clamp(a)
            self.want_col = self.col(self.pos)
            return
        if op == 'c':
            self.replace(a, b, '')
            self.pos = a
            self.start_insert()
            return
        if op in ('<', '>', '=', 'gc'):
            self.apply_operator(op, a, max(a, b - 1), 'line', reg)
            return
        if op in ('g~', 'gu', 'gU'):
            self.change_case(op, a, b)
            self.pos = self.clamp(a)
            return

    def change_case(self, op, a, b):
        text = self.t[a:b]
        new = text.swapcase() if op == 'g~' else text.lower() if op == 'gu' else text.upper()
        self.replace(a, b, new)

    def shift_lines(self, l1, l2, direction, count=1):
        sw = self.options['shiftwidth']
        for n in range(l2, l1 - 1, -1):
            s = self.line_start(n)
            fn = self.first_nonblank(n)
            if fn == self.line_end(n) and direction > 0:
                continue
            width = len(self.t[s:fn].expandtabs(self.options['tabstop']))
            new = max(0, (width // sw + direction * count) * sw if direction < 0 and width % sw else width + direction * count * sw)
            if direction < 0 and width % sw:
                new = max(0, (width // sw - (count - 1)) * sw)
            self.replace(s, fn, ' ' * new if self.options['expandtab'] else '\t' * (new // self.options['tabstop']) + ' ' * (new % self.options['tabstop']))

    def bracket_depth_delta(self, line):
        """Bracket depth change of a line, ignoring strings and // comments."""
        depth = 0
        leading = 0
        seen_code = False
        i = 0
        in_string = False
        while i < len(line):
            ch = line[i]
            if in_string:
                if ch == '\\':
                    i += 2
                    continue
                if ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
                seen_code = True
            elif line.startswith('//', i):
                break
            elif ch in '([{':
                depth += 1
                seen_code = True
            elif ch in ')]}':
                depth -= 1
                if not seen_code:
                    leading += 1
            elif ch not in ' \t':
                seen_code = True
            i += 1
        return depth, leading

    def reindent(self, l1, l2):
        sw = self.options['shiftwidth']
        depth = 0
        comment = False
        for n in range(0, l1):
            line = self.line_text(n)
            d, _ = self.bracket_depth_delta(line)
            depth += d
        depth = max(0, depth)
        for n in range(l1, l2 + 1):
            line = self.line_text(n)
            stripped = line.strip()
            d, leading = self.bracket_depth_delta(stripped)
            if '/*' in stripped and '*/' not in stripped:
                comment = True
            level = max(0, depth - leading)
            if stripped:
                s = self.line_start(n)
                self.replace(s, self.first_nonblank(n), ' ' * (level * sw) + (' ' if comment and stripped.startswith('*') else ''))
            if '*/' in stripped:
                comment = False
            depth = max(0, depth + d)

    def toggle_comment(self, l1, l2):
        lines = [n for n in range(l1, l2 + 1) if self.line_text(n).strip()]
        if not lines:
            return
        commented = all(self.line_text(n).lstrip().startswith('//') for n in lines)
        indent = min(len(self.indent_of(n)) for n in lines)
        for n in reversed(lines):
            s = self.line_start(n)
            if commented:
                fn = self.first_nonblank(n)
                width = 3 if self.t.startswith('// ', fn) else 2
                self.replace(fn, fn + width, '')
            else:
                self.replace(s + indent, s + indent, '// ')

    # --------------------------------------------------------------- commands
    def command(self, name, arg, count, reg, keys):
        t = self.t
        n = count or 1
        p = self.pos
        cur = self.line_of(p)
        change_keys = keys

        def done_change():
            self.record(reg, count, change_keys, name)
        if name in ('i', 'a', 'I', 'A', 'o', 'O', 'gi', 'gI', '<Insert>'):
            if name == 'a' and self.line_end(cur) > p:
                self.pos = p + 1
            elif name == 'I':
                self.pos = self.first_nonblank(cur)
            elif name == 'gI':
                self.pos = self.line_start(cur)
            elif name == 'A':
                self.pos = self.line_end(cur)
            elif name == 'gi':
                self.pos = min(self.marks.get('^', p), len(t))
            elif name == 'o':
                indent = self.auto_indent(cur, below=True)
                e = self.line_end(cur)
                self.replace(e, e, '\n' + indent)
                self.pos = e + 1 + len(indent)
            elif name == 'O':
                indent = self.auto_indent(cur, below=False)
                s = self.line_start(cur)
                self.replace(s, s, indent + '\n')
                self.pos = s + len(indent)
            self.start_insert(count=n, repeat_kind=name if name in 'oO' else None)
            self.record(reg, count, keys, name)
            return
        if name == 'R':
            self.start_insert(count=n, replace=True)
            self.record(reg, count, keys, name)
            return
        if name in ('v', 'V', '<C-v>'):
            self.anchor = p
            self.set_mode({'v': 'VISUAL', 'V': 'VISUAL_LINE', '<C-v>': 'VISUAL_BLOCK'}[name])
            if count and name == 'v':
                self.pos = self.clamp(p + n - 1)
            elif count and name == 'V':
                self.pos = self.col_pos(min(self.last_line(), cur + n - 1))
            return
        if name == 'gv':
            if not self.last_visual:
                raise Abort()
            mode, a, b = self.last_visual
            self.set_mode(mode)
            self.anchor, self.pos = min(a, len(t)), self.clamp(b)
            return
        if name in ('x', '<Del>', 'X', 's'):
            e = self.line_end(cur)
            s = self.line_start(cur)
            if name in ('x', '<Del>', 's'):
                if p >= e and name != 's':
                    raise Abort()
                a, b = p, min(e, p + n)
            else:
                if p <= s:
                    raise Abort()
                a, b = max(s, p - n), p
            self.store(reg, t[a:b], 'char', 'd')
            self.replace(a, b, '')
            self.pos = a
            if name == 's':
                self.start_insert()
                self.record(reg, count, keys, name)
                return
            self.pos = self.clamp(a)
            done_change()
            return
        if name in ('D', 'C'):
            last = min(self.last_line(), cur + n - 1)
            e = self.line_end(last)
            self.store(reg, t[p:e], 'char', 'd')
            self.replace(p, e, '')
            if name == 'C':
                self.pos = p
                self.start_insert()
                self.record(reg, count, keys, name)
                return
            self.pos = self.clamp(p)
            done_change()
            return
        if name == 'S':
            self.keys = []
            last = min(self.last_line(), cur + n - 1)
            self.apply_operator('c', self.line_start(cur), self.line_start(last), 'line', reg)
            self.record(reg, count, keys, name)
            return
        if name == 'Y':
            last = min(self.last_line(), cur + n - 1)
            self.apply_operator('y', self.line_start(cur), self.line_start(last), 'line', reg)
            return
        if name in ('p', 'P', 'gp', 'gP'):
            self.put(reg, n, after=name in ('p', 'gp'), move_after=name.startswith('g'))
            done_change()
            return
        if name == 'r':
            e = self.line_end(cur)
            if p + n > e:
                raise Abort()
            if arg == '\n':
                self.replace(p, p + n, '\n' + self.indent_of(cur) if False else '\n')
                self.pos = p + 1
            else:
                self.replace(p, p + n, arg * n)
                self.pos = p + n - 1
            done_change()
            return
        if name == '~':
            e = self.line_end(cur)
            if p >= e:
                raise Abort()
            b = min(e, p + n)
            self.replace(p, b, t[p:b].swapcase())
            self.pos = self.clamp(b)
            done_change()
            return
        if name in ('J', 'gJ'):
            joins = max(1, n - 1)
            if cur >= self.last_line():
                raise Abort()
            for _ in range(joins):
                if cur >= self.last_line():
                    break
                e = self.line_end(cur)
                nxt = self.line_of(e + 1)
                if name == 'gJ':
                    self.replace(e, e + 1, '')
                    self.pos = e
                    continue
                fn = self.first_nonblank(nxt)
                rest = self.t[fn:self.line_end(nxt)]
                before = self.t[self.line_start(cur):e]
                trimmed_end = e
                while trimmed_end > self.line_start(cur) and self.t[trimmed_end - 1] in ' \t':
                    trimmed_end -= 1
                sep = '' if (not rest or rest.startswith(')') or not before.strip()) else ' '
                if before.endswith((' ', '\t')) and sep:
                    trimmed_end = e
                    sep = ''
                self.replace(trimmed_end if sep or not rest else e, fn, sep)
                self.pos = (trimmed_end if sep or not rest else e)
            self.pos = self.clamp(self.pos)
            done_change()
            return
        if name in ('u', '<C-r>', 'U'):
            for _ in range(n):
                q = self.buf.undo() if name in ('u', 'U') else self.buf.redo()
                self.refresh()
                if q is None:
                    self.msg('Already at oldest change' if name != '<C-r>' else 'Already at newest change')
                    break
                self.pos = self.clamp(q)
                self.want_col = self.col(self.pos)
            return
        if name == '.':
            self.repeat_change(count)
            return
        if name == 'q':
            if self.recording is not None:
                self.stop_recording()
                return
            if not (arg.isalnum() or arg == '"'):
                raise Abort()
            self.recording = arg
            self.macro_keys = []
            return
        if name == '@':
            target = self.last_macro if arg == '@' else arg
            if target is None:
                raise Abort('E748: No previously used register')
            if target == ':':
                for _ in range(n):
                    if self.cmd_history[':']:
                        self.execute_ex(self.cmd_history[':'][-1])
                return
            reg_obj = self.get_register(target)
            if reg_obj is None:
                raise Abort()
            self.last_macro = target
            macro = parse_keys(reg_obj.text)
            self.replay(macro * n)
            return
        if name == 'm':
            if arg.isalpha() or arg in "'`[]<>":
                self.marks[arg if arg not in "'`" else '`'] = p
                if arg in "'`":
                    self.push_jump(force=True)
                return
            raise Abort()
        if name in ('<C-a>', '<C-x>'):
            self.increment(n if name == '<C-a>' else -n)
            done_change()
            return
        if name in ('&', 'g&'):
            if not self.last_sub:
                raise Abort('E35: No previous regular expression')
            pattern, replacement, flags = self.last_sub
            if name == '&':
                self.substitute(cur, cur, pattern, replacement, flags.replace('g', '') if False else '')
            else:
                self.substitute(0, self.last_line(), pattern, replacement, flags)
            done_change()
            return
        if name in ('<C-d>', '<C-u>', '<C-f>', '<C-b>', '<PageDown>', '<PageUp>'):
            first, last = self.host.visible_lines()
            page = max(1, last - first)
            amount = (count or max(1, page // 2)) if name in ('<C-d>', '<C-u>') else page * n
            sign = 1 if name in ('<C-d>', '<C-f>', '<PageDown>') else -1
            target = max(0, min(self.last_line(), cur + sign * amount))
            self.pos = self.first_nonblank(target)
            self.host.scroll('lines', sign * amount)
            return
        if name in ('<C-e>', '<C-y>'):
            self.host.scroll('lines', n if name == '<C-e>' else -n)
            return
        if name in ('zz', 'z.', 'zt', 'z<CR>', 'zb', 'z-'):
            if count:
                self.pos = self.first_nonblank(min(self.last_line(), n - 1))
            self.host.scroll({'zz': 'center', 'z.': 'center', 'zt': 'top', 'z<CR>': 'top', 'zb': 'bottom', 'z-': 'bottom'}[name])
            return
        if name in ('ZZ', 'ZQ'):
            self.host.ex_command('x' if name == 'ZZ' else 'q!')
            return
        if name in (':',):
            self.start_cmdline(':')
            if count:
                self.cmdline = '.,.+%d' % (n - 1) if n > 1 else '.'
            return
        if name in ('/', '?'):
            self.start_cmdline(name)
            return
        if name in ('<C-o>', '<C-i>', '<Tab>'):
            self.jump(-n if name == '<C-o>' else n)
            return
        if name == '<C-g>':
            total = self.last_line() + 1
            self.msg(f'line {cur + 1} of {total} --{(cur + 1) * 100 // max(1, total)}%-- col {self.col(p) + 1}')
            return
        if name in ('<Esc>', '<C-c>'):
            self.highlight = self.highlight
            return
        if name in ('gd', 'gD'):
            return
        if name in ('K',):
            word = self.word_under_cursor()
            if word and not self.host.goto_definition(word):
                self.msg('No definition for ' + word)
            return
        if name == '<C-]>':
            word = self.word_under_cursor()
            if word and not self.host.goto_definition(word):
                result = self.motion('gd', None, None, None)
                if result:
                    self.move_to(result)
            return
        if name in ('gx', 'ga'):
            if p < len(t):
                self.msg(f'<{t[p]}> {ord(t[p])}, Hex {ord(t[p]):02x}')
            return
        raise Abort()

    def auto_indent(self, n, below=True):
        if not self.options['autoindent']:
            return ''
        indent = self.indent_of(n)
        line = self.line_text(n).rstrip()
        if below and line.endswith(('{', '(', '[')):
            indent += ' ' * self.options['shiftwidth']
        if not below and line.strip().startswith(('}', ')', ']')) and n > 0:
            indent = indent + ' ' * self.options['shiftwidth']
        return indent

    def put(self, reg, n, after=True, move_after=False, replace_range=None):
        register = self.get_register(reg)
        if register is None or (not register.text and register.kind != 'line'):
            raise Abort('E353: Nothing in register ' + (reg or '"'))
        t = self.t
        p = self.pos
        cur = self.line_of(p)
        if register.kind == 'block':
            self.put_block(register, n, after)
            return
        if register.kind == 'line':
            text = register.text if register.text.endswith('\n') else register.text + '\n'
            text = text * n
            if after:
                e = self.line_end(cur)
                if e >= len(t):
                    self.replace(e, e, '\n' + text[:-1])
                    start = e + 1
                else:
                    self.replace(e + 1, e + 1, text)
                    start = e + 1
            else:
                start = self.line_start(cur)
                self.replace(start, start, text)
            if move_after:
                self.pos = self.clamp(min(len(self.t), start + len(text)))
            else:
                self.pos = self.first_nonblank(self.line_of(start))
            return
        text = register.text * n
        e = self.line_end(cur)
        at = p + 1 if after and p < e else p
        self.replace(at, at, text)
        self.pos = self.clamp(at + len(text) if move_after else at + len(text) - 1, insert=move_after)
        if not move_after:
            self.pos = self.clamp(self.pos)

    def put_block(self, register, n, after):
        lines = register.text.split('\n')
        width = max((len(x) for x in lines), default=0)
        cur = self.line_of(self.pos)
        col = self.col(self.pos) + (1 if after and self.line_end(cur) > self.pos else 0)
        for i, piece in enumerate(lines):
            line = cur + i
            if line > self.last_line():
                e = len(self.t)
                if self.t.endswith('\n'):
                    self.replace(e, e, '\n')
                else:
                    self.replace(e, e, '\n')
                self.refresh() if False else None
            s, e = self.line_start(line), self.line_end(line)
            pad = max(0, col - (e - s))
            insert = (' ' * pad) + (piece.ljust(width) * n if i < len(lines) else piece)
            at = min(s + col, e)
            if at == e:
                insert = insert.rstrip()
            self.replace(at, at, insert)
        self.pos = self.line_start(cur) + col

    def increment(self, delta):
        t = self.t
        cur = self.line_of(self.pos)
        s, e = self.line_start(cur), self.line_end(cur)
        line = t[s:e]
        col = self.pos - s
        for m in re.finditer(r'-?\d+', line):
            if m.end() > col:
                start, end = m.start(), m.end()
                text = m.group(0)
                if text.startswith('-') and start > 0 and (line[start - 1].isalnum() or line[start - 1] in '_)]'):
                    start += 1
                    text = text[1:]
                value = int(text) + delta
                width = len(text.lstrip('-'))
                digits = str(abs(value))
                if text.lstrip('-').startswith('0') and width > 1:
                    digits = digits.zfill(width)
                new = ('-' if value < 0 else '') + digits
                self.replace(s + start, s + end, new)
                self.pos = s + start + len(new) - 1
                return
        raise Abort()

    # ------------------------------------------------------------------ visual
    def visual_range(self):
        a, b = sorted((self.anchor, self.pos))
        t = self.t
        if self.mode == 'VISUAL_LINE':
            l1, l2 = self.line_of(a), self.line_of(b)
            return 'line', self.line_start(l1), self.line_start(l2)
        if self.mode == 'VISUAL_BLOCK':
            return 'block', a, b
        return 'char', a, min(len(t), b + 1)

    def block_bounds(self):
        l1, l2 = sorted((self.line_of(self.anchor), self.line_of(self.pos)))
        c1, c2 = sorted((self.col(self.anchor), self.col(self.pos)))
        if self.want_col == float('inf'):
            c2 = 10 ** 9
        return l1, l2, c1, c2

    def selection(self):
        """Ranges [(a, b)] (end exclusive) to highlight, or [] outside visual mode."""
        if not self.mode.startswith('VISUAL'):
            return []
        t = self.t
        if self.mode == 'VISUAL_BLOCK':
            l1, l2, c1, c2 = self.block_bounds()
            out = []
            for n in range(l1, l2 + 1):
                s, e = self.line_start(n), self.line_end(n)
                out.append((min(s + c1, e), min(s + c2 + 1, e)))
            return out
        kind, a, b = self.visual_range()
        if kind == 'line':
            l2 = self.line_of(b)
            return [(a, min(len(t), self.line_end(l2) + 1))]
        return [(a, b)]

    def visual_command(self, name, arg, count, reg):
        t = self.t
        n = count or 1
        if self.motion_known(name) and name not in ('gd', 'gD'):
            if len(name) == 2 and name[0] in 'ia':
                result = self.text_object(name[1], name[0] == 'i', n)
                if result is None:
                    raise Abort()
                _, a, b, kind = result
                cur_a, cur_b = sorted((self.anchor, self.pos))
                if kind in ('char', 'linesel') and name[1] in BRACKETS and self.anchor != self.pos and cur_a <= a and b - 1 <= cur_b:
                    result = self.text_object(name[1], name[0] == 'i', n + 1) if (a, b - 1) == (cur_a, cur_b) else result
                    if result is None:
                        raise Abort()
                    _, a, b, kind = result
                if kind == 'line' and self.mode == 'VISUAL':
                    self.set_mode('VISUAL_LINE')
                self.anchor, self.pos = a, (b if kind == 'line' else max(a, b - 1))
                return
            result = self.motion(name, arg, count, None)
            if result is None:
                raise Abort()
            self.move_to(result, name)
            return
        if name in ('v', 'V', '<C-v>'):
            target = {'v': 'VISUAL', 'V': 'VISUAL_LINE', '<C-v>': 'VISUAL_BLOCK'}[name]
            self.set_mode('NORMAL' if self.mode == target else target)
            if self.mode == 'NORMAL':
                self.pos = self.clamp(self.pos)
            return
        if name in ('<Esc>', '<C-c>', '<C-[>'):
            self.set_mode('NORMAL')
            self.pos = self.clamp(self.pos)
            return
        if name in ('o', 'O'):
            if name == 'O' and self.mode == 'VISUAL_BLOCK':
                l1, l2, c1, c2 = self.block_bounds()
                a_line, p_line = self.line_of(self.anchor), self.line_of(self.pos)
                self.anchor = self.line_start(a_line) + (c2 if self.col(self.anchor) == c1 else c1)
                self.pos = self.line_start(p_line) + (c2 if self.col(self.pos) == c1 else c1)
                return
            self.anchor, self.pos = self.pos, self.anchor
            return
        if name == ':':
            self.set_mode('NORMAL')
            self.start_cmdline(':')
            self.cmdline = "'<,'>"
            return
        kind, a, b = self.visual_range()
        mode = self.mode
        lines = self.line_of(b) - self.line_of(a) + 1
        length = b - a
        self.set_mode('NORMAL')
        replay = self.visual_replay_keys(mode, lines, length)
        if mode == 'VISUAL_BLOCK':
            self.block_command(name, arg, n, reg)
            return
        ops = {'d': 'd', 'x': 'd', '<Del>': 'd', 'y': 'y', 'c': 'c', 's': 'c', '<': '<', '>': '>', '=': '=',
               '~': 'g~', 'u': 'gu', 'U': 'gU', 'g~': 'g~', 'gu': 'gu', 'gU': 'gU', 'gc': 'gc', 'g?': 'g~',
               '<Tab>': '>', '<S-Tab>': '<'}
        if name in ('X', 'D', 'Y', 'C', 'S', 'R'):
            op = {'X': 'd', 'D': 'd', 'Y': 'y', 'C': 'c', 'S': 'c', 'R': 'c'}[name]
            self.apply_operator(op, a, b if kind == 'line' else max(a, b - 1), 'line', reg)
            if op != 'y':
                self.record(reg, None, replay + [name], op)
            return
        if name in ops:
            op = ops[name]
            if kind == 'line':
                self.apply_operator(op, a, b, 'line', reg, count=n if op in '<>' else 1)
            elif op in ('<', '>'):
                self.apply_operator(op, a, max(a, b - 1), 'line', reg, count=n)
            else:
                self.apply_operator(op, a, b, 'char', reg)
            if op != 'y':
                if kind == 'line' or op in ('<', '>', '=', 'gc'):
                    doubled = [op, op] if len(op) == 1 else [op[0], op[1], op[1]]
                    self.record(reg, lines if lines > 1 else None, doubled, op)
                else:
                    self.record(reg, None, replay + [name], op)
            return
        if name == 'r':
            text = self.t[a:b] if kind == 'char' else self.t[a:self.line_end(self.line_of(b))]
            end = b if kind == 'char' else self.line_end(self.line_of(b))
            self.replace(a, end, re.sub(r'[^\n]', arg, text))
            self.pos = self.clamp(a)
            self.record(reg, None, replay + ['r', arg], 'r')
            return
        if name in ('J', 'gJ'):
            l1, l2 = self.line_of(a), self.line_of(b if kind == 'line' else max(a, b - 1))
            self.pos = self.line_start(l1)
            self.command(name, None, max(2, l2 - l1 + 1), reg, [name])
            return
        if name in ('p', 'P'):
            register = self.get_register(reg)
            if register is None:
                raise Abort()
            saved = Register(register.text, register.kind)
            if kind == 'line':
                l2 = self.line_of(b)
                start, end = self.line_start(self.line_of(a)), self.line_end(l2)
                old = self.t[start:end] + '\n'
                text = saved.text if saved.kind == 'line' else saved.text + '\n'
                self.replace(start, min(len(self.t), end + 1), text if end < len(self.t) else text.rstrip('\n'))
                self.pos = self.first_nonblank(self.line_of(start))
                if name == 'p':
                    self.store(None, old, 'line', 'd')
            else:
                old = self.t[a:b]
                text = saved.text
                if saved.kind == 'line':
                    text = '\n' + saved.text
                self.replace(a, b, text)
                self.pos = self.clamp(a + len(text) - 1)
                if name == 'p':
                    self.store(None, old, 'char', 'd')
            return
        if name in ('I', 'A'):
            self.pos = a if name == 'I' else (b if kind == 'char' else self.line_end(self.line_of(b)))
            self.start_insert()
            return
        if name == '*' or name == '#':
            text = self.t[a:b]
            self.last_search = re.sub(r'([\\/.*$^~\[\]])', r'\\\1', text)
            self.search_forward = name == '*'
            q = self.search(self.last_search, self.search_forward, a, n)
            if q is not None:
                self.pos = q
            return
        raise Abort()

    def visual_replay_keys(self, mode, lines, length):
        if mode == 'VISUAL_LINE':
            return ['V'] + ([str(lines - 1), 'j'] if lines > 1 else [])
        if mode == 'VISUAL' and lines == 1:
            return ['v'] + ([str(length - 1), 'l'] if length > 1 else [])
        return ['v'] + ([str(lines - 1), 'j', '$'] if lines > 1 else [])

    def block_command(self, name, arg, n, reg):
        l1, l2, c1, c2 = self.block_bounds()
        to_end = c2 >= 10 ** 9
        spans = []
        for line in range(l1, l2 + 1):
            s, e = self.line_start(line), self.line_end(line)
            spans.append((line, min(s + c1, e), min(s + c2 + 1, e)))
        text = '\n'.join(self.t[a:b] for _, a, b in spans)
        if name in ('d', 'x', '<Del>', 'y', 'c', 's'):
            self.store(reg, text, 'block', 'y' if name == 'y' else 'd')
            if name == 'y':
                self.pos = self.line_start(l1) + c1
                return
            for _, a, b in reversed(spans):
                self.replace(a, b, '')
            self.pos = self.clamp(self.line_start(l1) + c1)
            if name in ('c', 's'):
                self.pos = min(self.line_start(l1) + c1, self.line_end(l1))
                self.start_insert(block=(l1, l2, c1, False))
            return
        if name in ('I', 'A'):
            if name == 'I':
                self.pos = min(self.line_start(l1) + c1, self.line_end(l1))
                self.start_insert(block=(l1, l2, c1, False))
            else:
                col = None if to_end else c2 + 1
                self.pos = self.line_end(l1) if to_end else self.line_start(l1) + min(col, self.line_end(l1) - self.line_start(l1))
                if not to_end and self.line_end(l1) - self.line_start(l1) < col:
                    pad = col - (self.line_end(l1) - self.line_start(l1))
                    self.replace(self.line_end(l1), self.line_end(l1), ' ' * pad)
                    self.pos = self.line_start(l1) + col
                self.start_insert(block=(l1, l2, col, True))
            return
        if name == 'r':
            for _, a, b in reversed(spans):
                self.replace(a, b, arg * (b - a))
            self.pos = self.line_start(l1) + c1
            return
        if name in ('~', 'u', 'U', 'g~', 'gu', 'gU'):
            op = {'~': 'g~', 'u': 'gu', 'U': 'gU'}.get(name, name)
            for _, a, b in reversed(spans):
                self.change_case(op, a, b)
            self.pos = self.line_start(l1) + c1
            return
        if name in ('<', '>'):
            self.shift_lines(l1, l2, 1 if name == '>' else -1, n)
            self.pos = self.first_nonblank(l1)
            return
        if name in ('J', 'gJ'):
            self.pos = self.line_start(l1)
            self.command(name, None, max(2, l2 - l1 + 1), reg, [name])
            return
        if name == 'gc':
            self.toggle_comment(l1, l2)
            return
        raise Abort()

    # ------------------------------------------------------------------ insert
    def start_insert(self, count=1, replace=False, repeat_kind=None, block=None):
        self.mode = 'REPLACE' if replace else 'INSERT'
        self.insert = dict(count=count, keys=[], start=self.pos, replaced=[], repeat=repeat_kind,
                           block=block, change=None)

    def insert_key(self, key):
        ins = self.insert or dict(count=1, keys=[], start=self.pos, replaced=[], repeat=None, block=None, change=None)
        self.insert = ins
        t = self.t
        p = self.pos
        if ins.get('register_wait'):
            ins['register_wait'] = False
            reg = self.get_register(key) if len(key) == 1 else None
            if reg:
                ins['keys'].append('<C-r>')
                ins['keys'].append(key)
                self.insert_text(reg.text)
            return
        if key in ('<Esc>', '<C-c>', '<C-[>'):
            self.finish_insert()
            return
        if key == '<C-o>':
            self.oneshot = True
            self.mode = 'NORMAL'
            self.pending_insert = ins
            return
        if key == '<C-r>':
            ins['register_wait'] = True
            return
        ins['keys'].append(key)
        self.apply_insert_key(key)

    def apply_insert_key(self, key):
        t = self.t
        p = self.pos
        cur = self.line_of(p)
        s, e = self.line_start(cur), self.line_end(cur)
        sw = self.options['shiftwidth']
        if key in ('<CR>', '\n', '<C-j>', '<C-m>'):
            before = t[s:p]
            indent = re.match(r'[ \t]*', before)[0] if self.options['autoindent'] else ''
            opener = before.rstrip().endswith(('{', '(', '['))
            if opener:
                indent += ' ' * sw
            after = t[p:e]
            closer = after.lstrip().startswith(('}', ')', ']')) and opener
            stripped_end = p
            while stripped_end > s and t[stripped_end - 1] in ' \t' and before.strip():
                stripped_end -= 1
            if closer:
                self.replace(stripped_end, p + len(after) - len(after.lstrip()), '\n' + indent + '\n' + indent[:-sw] if len(indent) >= sw else '\n' + indent + '\n')
            else:
                self.replace(stripped_end, p + len(after) - len(after.lstrip()) if after.strip() else p, '\n' + indent)
            self.pos = stripped_end + 1 + len(indent)
            return
        if key in ('<BS>', '<C-h>'):
            if p == 0:
                return
            if self.mode == 'REPLACE':
                ins = self.insert
                if ins['replaced']:
                    position, original = ins['replaced'].pop()
                    self.replace(position, position + 1, original) if original else self.replace(position, position + 1, '')
                    self.pos = position
                else:
                    self.pos = p - 1
                return
            before = t[s:p]
            sts = self.options['softtabstop']
            if before and before.strip(' ') == '' and sts and len(before) % sts == 0 and len(before) >= sts:
                self.replace(p - sts, p, '')
                self.pos = p - sts
            elif before and before.strip(' ') == '' and sts and len(before) % sts:
                cut = len(before) % sts
                self.replace(p - cut, p, '')
                self.pos = p - cut
            else:
                self.replace(p - 1, p, '')
                self.pos = p - 1
            return
        if key == '<Del>':
            if p < len(t):
                self.replace(p, p + 1, '')
            return
        if key == '<Tab>':
            if self.options['expandtab']:
                col = len(t[s:p].expandtabs(self.options['tabstop']))
                width = self.options['softtabstop'] or sw
                self.insert_text(' ' * (width - col % width))
            else:
                self.insert_text('\t')
            return
        if key == '<C-w>':
            q = p
            while q > s and t[q - 1] in ' \t':
                q -= 1
            if q > s:
                c = char_class(t[q - 1])
                while q > s and char_class(t[q - 1]) == c:
                    q -= 1
            elif q == s and q > 0 and p == s:
                q -= 1
            self.replace(q, p, '')
            self.pos = q
            return
        if key == '<C-u>':
            fn = self.first_nonblank(cur)
            q = fn if p > fn else s
            self.replace(q, p, '')
            self.pos = q
            return
        if key in ('<C-t>', '<C-d>'):
            offset = p - self.first_nonblank(cur)
            self.shift_lines(cur, cur, 1 if key == '<C-t>' else -1)
            self.pos = max(self.line_start(cur), self.first_nonblank(cur) + offset)
            return
        if key == '<C-v>':
            clip = self.host.clipboard_get()
            if clip:
                self.insert_text(clip)
            return
        if key in ('<C-n>', '<C-p>'):
            self.host.complete(key == '<C-n>')
            return
        if key in ('<Left>', '<Right>', '<Up>', '<Down>', '<Home>', '<End>'):
            if key == '<Left>':
                self.pos = max(s, p - 1)
            elif key == '<Right>':
                self.pos = min(e, p + 1)
            elif key == '<Home>':
                self.pos = s
            elif key == '<End>':
                self.pos = e
            else:
                target = cur + (1 if key == '<Down>' else -1)
                if 0 <= target < len(self.lines()):
                    self.pos = self.line_start(target) + min(p - s, self.line_end(target) - self.line_start(target))
            self.insert['start'] = self.pos
            self.insert['keys'] = []
            return
        if key.startswith('<') and len(key) > 1:
            return
        if key == '}' or key == ')' or key == ']':
            before = t[s:p]
            if before.strip() == '' and before and key == '}':
                opener = self.unmatched_open('{', '}', p, 1)
                indent = self.indent_of(self.line_of(opener)) if opener is not None else before[:max(0, len(before) - sw)]
                self.replace(s, p, indent)
                p = s + len(indent)
                self.pos = p
        self.insert_text(key)

    def insert_text(self, text):
        p = self.pos
        if self.mode == 'REPLACE':
            e = self.line_end(self.line_of(p))
            for ch in text:
                if ch != '\n' and p < e:
                    self.insert['replaced'].append((p, self.t[p]))
                    self.replace(p, p + 1, ch)
                else:
                    self.insert['replaced'].append((p, ''))
                    self.replace(p, p, ch)
                    if ch == '\n':
                        e = self.line_end(self.line_of(p + 1))
                    else:
                        e += 1
                p += 1
            self.pos = p
            return
        self.replace(p, p, text)
        self.pos = p + len(text)

    def finish_insert(self):
        ins = self.insert or {}
        keys = ins.get('keys', [])
        count = ins.get('count', 1)
        block = ins.get('block')
        if count > 1 and keys:
            for _ in range(count - 1):
                if ins.get('repeat') in ('o', 'O'):
                    cur = self.line_of(self.pos)
                    indent = self.indent_of(cur)
                    e = self.line_end(cur)
                    self.replace(e, e, '\n' + indent)
                    self.pos = e + 1 + len(indent)
                for k in keys:
                    self.apply_insert_key(k)
        if block:
            l1, l2, col, append = block
            start = ins['start']
            if self.line_of(self.pos) == l1 and self.pos >= start:
                text = self.t[start:self.pos]
                if text and '\n' not in text:
                    for line in range(l2, l1, -1):
                        s, e = self.line_start(line), self.line_end(line)
                        if col is None:
                            at = e
                        else:
                            if e - s < col:
                                if not append:
                                    continue
                                self.replace(e, e, ' ' * (col - (e - s)))
                                e = self.line_end(line)
                            at = s + col
                        self.replace(at, at, text)
                    self.pos = start
        inserted = ''.join(k if len(k) == 1 else ('\n' if k == '<CR>' else '') for k in keys)
        self.last_insert = inserted
        self.marks['^'] = self.pos
        change = ins.get('change')
        if change is not None:
            change = dict(change)
            change['keys'] = change['keys'] + keys + ['<Esc>']
            self.last_change = change
        self.insert = None
        self.mode = 'NORMAL'
        s = self.line_start(self.line_of(self.pos))
        if self.pos > s and not block:
            self.pos -= 1
        self.pos = self.clamp(self.pos)
        self.want_col = self.col(self.pos)
        if self.oneshot:
            self.oneshot = False

    def finish_oneshot(self):
        self.oneshot = False
        ins = getattr(self, 'pending_insert', None) or dict(count=1, keys=[], start=self.pos, replaced=[], repeat=None, block=None, change=None)
        self.mode = 'INSERT'
        self.insert = ins

    # ----------------------------------------------------------------- repeat
    def repeat_change(self, count):
        change = self.last_change
        if not change:
            return
        keys = []
        if change['reg']:
            reg = change['reg']
            if reg.isdigit() and reg != '0' and reg != '9':
                reg = str(int(reg) + 1)
                change['reg'] = reg
            keys += ['"', reg]
        total = count if count is not None else change['count']
        if total:
            keys += list(str(total))
        keys += change['keys']
        saved = self.last_change
        self.replay(keys)
        if count is None:
            self.last_change = saved
        elif self.last_change is not None:
            self.last_change = dict(self.last_change, count=count)

    def replay(self, keys):
        if self.depth > 50:
            raise Abort('E132: Function call depth is higher than maxfuncdepth')
        self.depth += 1
        recording = self.recording
        self.recording = None
        try:
            for i, key in enumerate(keys):
                if i > 200000:
                    break
                self.feed(key)
        except Abort:
            if self.depth > 1:
                raise
        finally:
            self.depth -= 1
            self.recording = recording
            self.keys = []

    def stop_recording(self):
        keys = self.macro_keys[:-1]
        self.registers[self.recording.lower()] = Register(keys_to_text(keys), 'char') if not self.recording.isupper() else Register(
            (self.registers.get(self.recording.lower()) or Register()).text + keys_to_text(keys), 'char')
        self.recording = None
        self.macro_keys = []

    # ----------------------------------------------------------------- jumps
    def push_jump(self, force=False):
        p = self.pos
        if self.jump_index < len(self.jumps):
            self.jumps = self.jumps[:self.jump_index]
        if force or not self.jumps or self.line_of(min(self.jumps[-1], len(self.t))) != self.line_of(p):
            self.jumps.append(p)
        self.jumps = self.jumps[-100:]
        self.jump_index = len(self.jumps)
        self.marks['`'] = p

    def jump(self, delta):
        if not self.jumps:
            raise Abort()
        if self.jump_index >= len(self.jumps) and delta < 0:
            self.jumps.append(self.pos)
            self.jump_index = len(self.jumps) - 1
        target = self.jump_index + delta
        if not 0 <= target < len(self.jumps):
            raise Abort()
        self.jump_index = target
        self.pos = self.clamp(min(self.jumps[target], len(self.t)))

    # ----------------------------------------------------------------- search
    def compile_search(self, pattern):
        try:
            return vim_regex(pattern, self.options['ignorecase'], self.options['smartcase'])
        except re.error as error:
            raise Abort(f'E486: Invalid pattern: {error}')

    def search(self, pattern, forward, start, count=1, wrap=None):
        regex = self.compile_search(pattern)
        t = self.t
        wrap = self.options['wrapscan'] if wrap is None else wrap
        p = start
        for _ in range(count):
            if forward:
                m = regex.search(t, p + 1) if p + 1 <= len(t) else None
                while m and m.start() == p:
                    m = regex.search(t, m.start() + 1)
                if m is None and wrap:
                    m = regex.search(t, 0)
                    if m:
                        self.msg('search hit BOTTOM, continuing at TOP')
                if m is None or (m.start() == p and count == 1 and False):
                    raise Abort('E486: Pattern not found: ' + pattern)
                p = m.start()
            else:
                found = None
                for m in regex.finditer(t, 0, max(0, p)):
                    if m.start() < p:
                        found = m.start()
                if found is None and wrap:
                    for m in regex.finditer(t, p):
                        found = m.start()
                    if found is not None:
                        self.msg('search hit TOP, continuing at BOTTOM')
                if found is None:
                    raise Abort('E486: Pattern not found: ' + pattern)
                p = found
        return p

    def highlight_regex(self):
        if not (self.options['hlsearch'] and self.highlight and self.last_search):
            return None
        try:
            return vim_regex(self.last_search, self.options['ignorecase'], self.options['smartcase'])
        except re.error:
            return None

    def push_search_history(self, pattern):
        history = self.cmd_history['/']
        if pattern in history:
            history.remove(pattern)
        history.append(pattern)

    # --------------------------------------------------------------- cmdline
    def start_cmdline(self, kind):
        self.cmdtype = kind
        self.cmdline = ''
        self.history_index = None
        self.cmd_origin = self.pos
        self.cmd_return_mode = self.mode if self.mode != 'COMMAND' else 'NORMAL'
        self.mode = 'COMMAND'

    def command_key(self, key):
        if getattr(self, 'cmd_register_wait', False):
            self.cmd_register_wait = False
            if key == '<C-w>':
                self.cmdline += self.word_under_cursor()
            elif len(key) == 1:
                reg = self.get_register(key)
                if reg:
                    self.cmdline += reg.text.rstrip('\n')
            return
        if key in ('<Esc>', '<C-c>'):
            self.mode = 'NORMAL'
            self.pos = self.cmd_origin
            self.pending_search_op = None
            self.host.search_preview(-1, -1)
            return
        if key in ('<CR>', '<C-j>', '<C-m>'):
            line, kind = self.cmdline, self.cmdtype
            self.mode = 'NORMAL'
            self.pos = self.cmd_origin
            self.host.search_preview(-1, -1)
            history = self.cmd_history[':' if kind == ':' else '/']
            if line:
                if line in history:
                    history.remove(line)
                history.append(line)
            self.begin()
            if kind == ':':
                self.execute_ex(line)
            else:
                self.execute_search(line, kind)
            if self.oneshot and self.mode == 'NORMAL':
                self.finish_oneshot()
            return
        if key in ('<BS>', '<C-h>'):
            if not self.cmdline:
                self.mode = 'NORMAL'
                self.pos = self.cmd_origin
                self.pending_search_op = None
                return
            self.cmdline = self.cmdline[:-1]
        elif key == '<C-u>':
            self.cmdline = ''
        elif key == '<C-w>':
            self.cmdline = re.sub(r'(\w+|[^\w\s]+)\s*$', '', self.cmdline.rstrip()) if self.cmdline.strip() else ''
        elif key == '<C-r>':
            self.cmd_register_wait = True
            return
        elif key in ('<Up>', '<Down>', '<C-p>', '<C-n>'):
            history = self.cmd_history[':' if self.cmdtype == ':' else '/']
            if not history:
                return
            if self.history_index is None:
                self.history_index = len(history)
            self.history_index += -1 if key in ('<Up>', '<C-p>') else 1
            self.history_index = max(0, min(len(history), self.history_index))
            self.cmdline = history[self.history_index] if self.history_index < len(history) else ''
        elif key == '<Tab>':
            if self.cmdtype == ':':
                self.complete_ex()
            return
        elif key == ' ':
            self.cmdline += ' '
        elif len(key) == 1:
            self.cmdline += key
        else:
            return
        if self.cmdtype in '/?' and self.options['incsearch'] and self.cmdline:
            try:
                regex = vim_regex(self.cmdline, self.options['ignorecase'], self.options['smartcase'])
                t = self.t
                if self.cmdtype == '/':
                    m = regex.search(t, self.cmd_origin + 1) or regex.search(t, 0)
                    if m:
                        self.host.search_preview(m.start(), m.end())
                else:
                    found = None
                    for m in regex.finditer(t, 0, self.cmd_origin):
                        found = m
                    if found:
                        self.host.search_preview(found.start(), found.end())
            except re.error:
                pass

    def complete_ex(self):
        names = ['write', 'quit', 'wq', 'substitute', 'global', 'vglobal', 'normal', 'sort', 'nohlsearch', 'set',
                 'registers', 'marks', 'delete', 'yank', 'move', 'copy', 'join', 'undo', 'redo', 'put',
                 'compile', 'export', 'kicad', 'inspect', 'help']
        word = self.cmdline.strip()
        matches = [n for n in names if n.startswith(word)]
        if len(matches) == 1:
            self.cmdline = matches[0] + ' '
        elif matches:
            self.msg(' '.join(matches))

    def execute_search(self, pattern, kind):
        forward = kind == '/'
        if not pattern:
            pattern = self.last_search
            if not pattern:
                raise Abort('E35: No previous regular expression')
        offset = None
        sep = '/' if forward else '?'
        unescaped = re.split(r'(?<!\\)' + re.escape(sep), pattern, maxsplit=1)
        if len(unescaped) == 2:
            pattern, offset = unescaped
            pattern = pattern or self.last_search
        self.last_search = pattern
        self.search_forward = forward
        self.highlight = True
        self.push_search_history(pattern)
        op = self.pending_search_op
        self.pending_search_op = None
        if op:
            name, reg, count, force, op_keys = op
            q = self.search(pattern, forward, self.pos, count or 1)
            self.operate(name, (self.pos, q, 'excl'), reg, force, '/')
            self.record(reg, None, op_keys + [kind] + parse_keys(pattern) + ['<CR>'], name)
            return
        q = self.search(pattern, forward, self.pos)
        if offset:
            m = re.fullmatch(r'([esb]?)([+-]?\d*)', offset)
            if m:
                regex = self.compile_search(pattern)
                match = regex.match(self.t, q)
                amount = int(m[2]) if m[2] not in ('', '+', '-') else (1 if m[2] == '+' else -1 if m[2] == '-' else 0)
                if m[1] == 'e' and match:
                    q = max(q, match.end() - 1) + amount
                elif m[1] in ('s', 'b'):
                    q += amount
                elif amount:
                    line = max(0, min(self.last_line(), self.line_of(q) + amount))
                    q = self.first_nonblank(line)
        if self.mode.startswith('VISUAL') or self.cmd_return_mode.startswith('VISUAL'):
            self.mode = self.cmd_return_mode
        self.push_jump()
        self.pos = self.clamp(q)
        self.want_col = self.col(self.pos)

    # -------------------------------------------------------------------- ex
    def parse_address(self, s, i):
        """Parse one address at s[i:]; return (line or None, i)."""
        line = None
        t = self.t
        cur = self.line_of(self.pos)
        if i < len(s):
            ch = s[i]
            if ch == '.':
                line, i = cur, i + 1
            elif ch == '$':
                line, i = self.last_line(), i + 1
            elif ch.isdigit():
                m = re.match(r'\d+', s[i:])
                line, i = int(m[0]) - 1, i + len(m[0])
            elif ch == "'" and i + 1 < len(s):
                mark = s[i + 1]
                if mark not in self.marks:
                    raise Abort('E20: Mark not set')
                line, i = self.line_of(min(self.marks[mark], len(t))), i + 2
            elif ch in '/?':
                end = i + 1
                while end < len(s) and s[end] != ch:
                    end += 2 if s[end] == '\\' else 1
                pattern = s[i + 1:end] or self.last_search
                start = self.line_end(cur) if ch == '/' else self.line_start(cur)
                q = self.search(pattern, ch == '/', start if ch == '/' else start, 1)
                line, i = self.line_of(q), min(len(s), end + 1)
        while i < len(s) and s[i] in '+-':
            sign = 1 if s[i] == '+' else -1
            i += 1
            m = re.match(r'\d+', s[i:])
            amount = int(m[0]) if m else 1
            if m:
                i += len(m[0])
            line = (cur if line is None else line) + sign * amount
        return line, i

    def parse_range(self, s):
        s = s.lstrip()
        i = 0
        if s.startswith('%'):
            return 0, self.last_line(), s[1:].lstrip(), True
        first, i = self.parse_address(s, 0)
        if first is None and i < len(s) and s[i] in ',;':
            first = self.line_of(self.pos)
        if i < len(s) and s[i] in ',;':
            if s[i] == ';' and first is not None:
                self.pos = self.line_start(max(0, min(first, self.last_line())))
            second, i = self.parse_address(s, i + 1)
            if second is None:
                second = self.line_of(self.pos)
        else:
            second = first
        has = first is not None
        if not has:
            cur = self.line_of(self.pos)
            first = second = cur
        if first > second:
            first, second = second, first
        last = self.last_line()
        if first < 0 or second > last + (1 if not self.t.endswith('\n') else 0):
            raise Abort('E16: Invalid range')
        return first, min(second, last), s[i:].lstrip(), has

    def execute_ex(self, line):
        line = line.strip().lstrip(':')
        if not line:
            return
        if line.startswith(('g/', 'g!', 'v/', 'global', 'vglobal')) or re.match(r'^%?\s*(g|global|v|vglobal)!?\s*[/#|]', line) or re.match(r"^[%.$\d',;+-]*\s*(g|v)(lobal|global)?!?\s*\W", line):
            pass
        first, last, rest, has_range = self.parse_range(line)
        m = re.match(r'([A-Za-z]+|[<>&!=#~@]+)?(!)?\s*(.*)$', rest, re.S)
        name, bang, arg = (m[1] or ''), bool(m[2]), m[3]
        if name and name.isalpha() and name[0] in 'sgv' and arg[:1] and not arg[:1].isalnum() and arg[:1] not in ' "|' and name in ('s', 'g', 'v', 'substitute', 'global', 'vglobal'):
            pass
        # :s/a/b/ style where the delimiter follows the name directly
        sm = re.match(r'(s|substitute|g|global|v|vglobal)(!?)([^\w\s"|])(.*)$', rest, re.S)
        if sm:
            name, bang, arg = sm[1], bool(sm[2]), sm[3] + sm[4]
        if not name:
            if has_range:
                self.push_jump()
                self.pos = self.first_nonblank(min(last, self.last_line()))
                return
            return
        cur = self.line_of(self.pos)
        if name in ('s', 'substitute'):
            self.ex_substitute(first, last, arg)
            return
        if name in ('&', '&&'):
            if not self.last_sub:
                raise Abort('E35: No previous regular expression')
            pattern, replacement, flags = self.last_sub
            self.substitute(first, last, pattern, replacement, flags if name == '&&' else arg.strip())
            return
        if name in ('g', 'global', 'v', 'vglobal'):
            if not has_range:
                first, last = 0, self.last_line()
            self.ex_global(first, last, arg, invert=bang or name.startswith('v'))
            return
        if name in ('d', 'de', 'del', 'delete', 'y', 'ya', 'yank'):
            reg = None
            arg = arg.strip()
            if arg and not arg[0].isdigit():
                reg, arg = arg[0], arg[1:].strip()
            if arg.isdigit():
                first = last
                last = min(self.last_line(), first + int(arg) - 1)
            op = 'd' if name.startswith('d') else 'y'
            self.apply_operator(op, self.line_start(first), self.line_start(last), 'line', reg)
            return
        if name in ('m', 'mo', 'move', 't', 'co', 'copy'):
            target, _ = self.parse_address(arg.strip(), 0)
            if target is None and arg.strip() == '0':
                target = -1
            if target is None:
                raise Abort('E14: Invalid address')
            s, e = self.line_start(first), self.line_end(last)
            text = self.t[s:e] + '\n'
            if name in ('m', 'mo', 'move') and first <= target <= last:
                if target == last or target == first - 1:
                    return
                raise Abort('E134: Cannot move a range of lines into itself')
            # insertion point after line `target` (-1 = before first line)
            if target < 0:
                at = 0
            else:
                te = self.line_end(target)
                at = te + 1 if te < len(self.t) else te
            if at == len(self.t) and not self.t.endswith('\n'):
                insert = '\n' + text[:-1]
            else:
                insert = text
            if name in ('m', 'mo', 'move'):
                if at > s:
                    self.replace(at, at, insert)
                    self.delete_lines(first, last)
                    self.pos = self.first_nonblank(target - (last - first + 1) + (last - first + 1))
                    self.pos = self.first_nonblank(self.line_of(at - (e + 1 - s)) + (last - first))
                else:
                    self.delete_lines(first, last)
                    self.replace(at, at, insert)
                    self.pos = self.first_nonblank(self.line_of(at) + (last - first))
            else:
                self.replace(at, at, insert)
                self.pos = self.first_nonblank(self.line_of(at + (1 if insert.startswith('\n') else 0)) + (last - first))
            return
        if name in ('j', 'join'):
            if first == last:
                last = min(self.last_line(), first + 1)
            self.pos = self.line_start(first)
            self.command('J' if not bang else 'gJ', None, last - first + 1, None, ['J'])
            return
        if name in ('>', '<', '>>', '<<', '>>>', '<<<'):
            self.shift_lines(first, last, 1 if name[0] == '>' else -1, len(name))
            self.pos = self.first_nonblank(last)
            return
        if name in ('norm', 'normal'):
            keys = parse_keys(arg)
            lines = range(first, last + 1) if has_range else [None]
            self.anchors = [self.line_start(n) for n in lines] if has_range else []
            for index in range(len(lines)):
                if has_range:
                    self.pos = self.anchors[index]
                self.mode = 'NORMAL'
                self.keys = []
                try:
                    self.replay(keys)
                finally:
                    if self.mode in ('INSERT', 'REPLACE'):
                        self.finish_insert()
                    elif self.mode != 'NORMAL':
                        self.mode = 'NORMAL'
                    self.keys = []
                self.begin_join()
            return
        if name in ('noh', 'nohl', 'nohlsearch'):
            self.highlight = False
            return
        if name in ('se', 'set', 'setl', 'setlocal'):
            self.ex_set(arg)
            return
        if name in ('reg', 'registers', 'di', 'display'):
            items = []
            for key in ['"', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '-'] + [chr(c) for c in range(97, 123)] + ['+']:
                reg = self.get_register(key) if key != '"' else self.registers.get('"')
                if reg and reg.text:
                    items.append(f'"{key} {reg.text[:40]!r}')
            self.msg('  '.join(items) or 'No registers')
            return
        if name in ('marks',):
            items = [f"{k} {self.line_of(min(v, len(self.t))) + 1}:{self.col(min(v, len(self.t)))}" for k, v in sorted(self.marks.items())]
            self.msg('  '.join(items) or 'No marks')
            return
        if name in ('delm', 'delmarks'):
            if bang:
                for k in [k for k in self.marks if k.islower()]:
                    del self.marks[k]
            for ch in arg.replace(' ', ''):
                self.marks.pop(ch, None)
            return
        if name in ('sor', 'sort'):
            if not has_range:
                first, last = 0, self.last_line()
            self.ex_sort(first, last, arg.strip(), bang)
            return
        if name in ('u', 'un', 'undo'):
            self.command('u', None, 1, None, ['u'])
            return
        if name in ('red', 'redo'):
            self.command('<C-r>', None, 1, None, ['<C-r>'])
            return
        if name in ('pu', 'put'):
            reg = arg.strip()[:1] or None
            register = self.get_register(reg)
            if register is None:
                raise Abort('E353: Nothing in register')
            self.pos = self.line_start(last)
            saved = self.registers.get('"')
            text = register.text if register.text.endswith('\n') else register.text + '\n'
            self.registers['__put'] = Register(text, 'line')
            clip = self.options['clipboard']
            self.options['clipboard'] = ''
            try:
                self.put('__put', 1, after=not bang)
            finally:
                self.options['clipboard'] = clip
                self.registers.pop('__put', None)
                if saved:
                    self.registers['"'] = saved
            return
        if name in ('le', 'left', 'ri', 'right', 'ce', 'center'):
            width = int(arg) if arg.strip().isdigit() else (0 if name.startswith('le') else 80)
            for n in range(last, first - 1, -1):
                s, e = self.line_start(n), self.line_end(n)
                text = self.t[s:e].strip()
                if name.startswith('le'):
                    new = ' ' * width + text
                elif name.startswith('ri'):
                    new = text.rjust(width)
                else:
                    new = text.center(width).rstrip()
                self.replace(s, e, new)
            return
        if name in ('retab', 'ret'):
            for n in range(last if has_range else self.last_line(), (first if has_range else 0) - 1, -1):
                s, fn = self.line_start(n), self.first_nonblank(n)
                self.replace(s, fn, self.t[s:fn].expandtabs(self.options['tabstop']))
            return
        if name in ('k', 'ma', 'mark'):
            mark = arg.strip()[:1]
            if mark:
                self.marks[mark] = self.line_start(last)
            return
        if name in ('h', 'help'):
            if not self.host.ex_command(line):
                self.msg(__doc__.split('\n\n')[1].replace('\n', ' '))
            return
        if not self.host.ex_command(line):
            raise Abort('E492: Not an editor command: ' + line)

    def begin_join(self):
        self.join = True

    def delete_lines(self, first, last):
        s, e = self.line_start(first), self.line_end(last)
        if e < len(self.t):
            self.replace(s, e + 1, '')
        elif s > 0:
            self.replace(s - 1, e, '')
        else:
            self.replace(s, e, '')

    def ex_set(self, arg):
        for item in arg.split():
            m = re.match(r'(no|inv)?([a-z]+)(\?|!|[+-^]?=(.*))?$', item)
            if not m:
                raise Abort('E518: Unknown option: ' + item)
            prefix, name, suffix, value = m[1], m[2], m[3], m[4]
            name = OPTION_ALIASES.get(name, name)
            if name not in self.options:
                if prefix == 'no' and OPTION_ALIASES.get(m[2][2:] if False else name, None):
                    pass
                raise Abort('E518: Unknown option: ' + item)
            current = self.options[name]
            if suffix == '?':
                self.msg(f'{name}={current}' if not isinstance(current, bool) else ('' if current else 'no') + name)
                continue
            if isinstance(current, bool):
                new = not current if (prefix == 'inv' or suffix == '!') else prefix != 'no'
            elif isinstance(current, int):
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    raise Abort('E521: Number required after =: ' + item)
                new = current + number if '+=' in item else current - number if '-=' in item else number
                new = max(1 if name != 'softtabstop' else 0, min(16, new))
            else:
                new = '' if prefix == 'no' else (value or '')
            self.options[name] = new
            self.host.option_changed(name, new)

    def ex_sort(self, first, last, flags, reverse):
        s, e = self.line_start(first), self.line_end(last)
        lines = self.t[s:e].split('\n')
        pattern = None
        m = re.search(r'/((?:\\.|[^/])*)/', flags)
        if m:
            pattern = self.compile_search(m[1])
            flags = flags.replace(m[0], '')
        key = None
        if 'n' in flags:
            def key(line):
                found = re.search(r'-?\d+(?:\.\d+)?', line if not pattern else pattern.split(line, 1)[-1])
                return (found is not None, float(found[0]) if found else 0.)
        elif 'i' in flags:
            def key(line):
                return line.lower()
        elif pattern:
            def key(line):
                return pattern.split(line, 1)[-1]
        lines.sort(key=key, reverse=reverse)
        if 'u' in flags:
            seen = set()
            unique = []
            for line in lines:
                k = line.lower() if 'i' in flags else line
                if k not in seen:
                    seen.add(k)
                    unique.append(line)
            lines = unique
        self.replace(s, e, '\n'.join(lines))
        self.pos = self.clamp(self.pos)

    def split_pattern(self, arg):
        """Split '/pat/rep/flags' with any delimiter; returns (pattern, rest...)."""
        if not arg:
            return None
        delim = arg[0]
        parts = []
        cur = ''
        i = 1
        while i < len(arg):
            ch = arg[i]
            if ch == '\\' and i + 1 < len(arg):
                if arg[i + 1] == delim:
                    cur += delim
                else:
                    cur += arg[i:i + 2]
                i += 2
                continue
            if ch == delim and len(parts) < 2:
                parts.append(cur)
                cur = ''
            else:
                cur += ch
            i += 1
        parts.append(cur)
        return parts

    def ex_substitute(self, first, last, arg):
        parts = self.split_pattern(arg)
        if parts is None:
            if not self.last_sub:
                raise Abort('E35: No previous regular expression')
            pattern, replacement, flags = self.last_sub
        else:
            pattern = parts[0] or self.last_search
            replacement = parts[1] if len(parts) > 1 else ''
            flags = parts[2].strip() if len(parts) > 2 else ''
            count = re.search(r'\d+$', flags)
            if count:
                first = last
                last = min(self.last_line(), first + int(count[0]) - 1)
                flags = flags[:count.start()].strip()
        if not pattern:
            raise Abort('E35: No previous regular expression')
        self.last_sub = (pattern, replacement, flags)
        self.last_search = pattern
        self.substitute(first, last, pattern, replacement, flags)

    def substitute(self, first, last, pattern, replacement, flags):
        ic = self.options['ignorecase']
        if 'i' in flags:
            ic = True
        if 'I' in flags:
            ic = False
        try:
            regex = vim_regex(pattern, ic, self.options['smartcase'] and 'i' not in flags)
        except re.error as error:
            raise Abort(f'E486: Invalid pattern: {error}')
        apply = vim_replacement(replacement)
        total = 0
        lines_changed = 0
        last_line_changed = None
        n = last
        if 'n' in flags:
            for line in range(first, last + 1):
                total += len(regex.findall(self.line_text(line)))
            self.msg(f'{total} matches on {last - first + 1} lines')
            return
        # bottom-up so earlier positions remain valid
        while n >= first:
            s, e = self.line_start(n), self.line_end(n)
            text = self.t[s:e]
            new, k = regex.subn(apply, text, count=0 if 'g' in flags else 1)
            if k:
                self.replace(s, e, new)
                total += k
                lines_changed += 1
                if last_line_changed is None:
                    last_line_changed = n
            n -= 1
        if not total:
            if 'e' not in flags:
                raise Abort('E486: Pattern not found: ' + pattern)
            return
        target = self.line_of(self.marks.get('[', self.line_start(last_line_changed)))
        self.pos = self.first_nonblank(min(self.last_line(), last_line_changed + self.t.count('\n', self.line_start(first), self.line_start(first)) if False else target))
        if total > 2:
            self.msg(f'{total} substitutions on {lines_changed} lines')

    def ex_global(self, first, last, arg, invert=False):
        if not arg:
            raise Abort('E35: No previous regular expression')
        delim = arg[0]
        parts = self.split_pattern(arg)
        pattern = parts[0] or self.last_search
        command = arg[1 + len(arg[1:].split(delim, 1)[0]) + 1:] if len(arg[1:].split(delim, 1)) > 1 else ''
        if len(parts) >= 2:
            # Recompute the command text verbatim after the second delimiter.
            i = 1
            while i < len(arg):
                if arg[i] == '\\':
                    i += 2
                    continue
                if arg[i] == delim:
                    break
                i += 1
            command = arg[i + 1:]
        regex = self.compile_search(pattern)
        self.last_search = pattern
        command = command.strip() or 'p'
        anchors = [self.line_start(n) for n in range(first, last + 1)
                   if bool(regex.search(self.line_text(n))) != invert]
        if not anchors:
            self.msg('Pattern not found: ' + pattern)
            return
        self.anchors = anchors
        count = len(anchors)
        for index in range(count):
            position = self.anchors[index]
            if position < 0 or position > len(self.t):
                continue
            # a deleted line collapses its anchor onto a neighbour; skip duplicates
            if index and self.anchors[index] == self.anchors[index - 1] and command.startswith('d'):
                continue
            self.pos = self.line_start(self.line_of(position))
            if command in ('p', 'print', '#', 'nu', 'number'):
                continue
            self.execute_ex(command)
        self.anchors = []
        self.pos = self.clamp(self.pos)
