import pytest
from scab.vim import Vim, TextBuffer, Host, parse_keys, vim_regex


class ClipHost(Host):
    def __init__(self):
        self.clip = None
        self.commands = []
    def clipboard_get(self): return self.clip
    def clipboard_set(self, text): self.clip = text
    def ex_command(self, command):
        self.commands.append(command)
        return command in ('w', 'x', 'q!')


def make(text, pos=0, clipboard=False):
    host = ClipHost()
    v = Vim(TextBuffer(text), host)
    if not clipboard:
        v.options['clipboard'] = ''
    v.pos = pos
    return v


def run(text, keys, pos=0, **kw):
    v = make(text, pos, **kw)
    v.feed_keys(keys)
    return v


def T(v):
    return v.buf.text()


# ---------------------------------------------------------------- motions
@pytest.mark.parametrize('keys,expected', [
    ('w', 4), ('2w', 7), ('e', 2), ('2e', 6), ('$', 14), ('0', 0), ('fz', 10), ('2fo', 2),
    ('tz', 9), ('W', 4), ('ww', 7), ('ge', 0), ('l', 1), ('3l', 3),
])
def test_line_motions(keys, expected):
    v = run('foo bar.baz quz\nnext', keys)
    assert v.pos == expected


def test_backward_and_repeat_find():
    v = run('a,b,c,d', '$')
    v.feed_keys('F,')
    assert v.pos == 5
    v.feed_keys(';')
    assert v.pos == 3
    v.feed_keys(',')
    assert v.pos == 5
    v = run('a,b,c,d', 't,;')
    assert v.pos == 2


def test_word_motions_cross_lines_and_empty_lines():
    v = run('foo\n\nbar baz', 'w')
    assert v.pos == 4
    v.feed_keys('w')
    assert v.pos == 5
    v.feed_keys('b')
    assert v.pos == 4
    v.feed_keys('b')
    assert v.pos == 0
    v = run('foo  bar', '$b')
    assert v.pos == 5


def test_vertical_motions_keep_column():
    v = run('abcdef\nab\nabcdef', '4ljj')
    assert v.pos == 14
    v.feed_keys('k')
    assert v.pos == 8
    v = run('a\nb\nc\nd\n', 'G')
    assert v.line_of(v.pos) == 3
    v.feed_keys('gg')
    assert v.pos == 0
    v.feed_keys('3G')
    assert v.line_of(v.pos) == 2


def test_paragraph_and_brackets():
    text = 'a\nb\n\nc\nd\n\ne'
    v = run(text, '}')
    assert v.pos == 4
    v.feed_keys('}')
    assert v.pos == 9
    v.feed_keys('{')
    assert v.pos == 4
    v = run('f(a, [1, 2], {x});', '%', pos=0)
    assert v.pos == 16
    v.feed_keys('%')
    assert v.pos == 1
    v = run('module m() {\n  cube(1);\n}', '[{', pos=17)
    assert v.pos == 11
    v.feed_keys(']}')
    assert v.pos == 24


def test_search_and_star():
    v = run('cube(1);\nsphere(2);\ncube(3);', '/cube\r'.replace('\r', '<CR>'))
    assert v.pos == 20
    v.feed_keys('n')
    assert v.pos == 0
    v.feed_keys('N')
    assert v.pos == 20
    v = run('foo bar foo baz foo', '*')
    assert v.pos == 8
    v.feed_keys('#')
    assert v.pos == 0
    v = run('abc\nxyz abc', '?abc<CR>', pos=0)
    assert v.pos == 8


def test_marks_and_jumps():
    v = run('one\ntwo\nthree\nfour', 'jmaG')
    v.feed_keys("'a")
    assert v.line_of(v.pos) == 1
    v.feed_keys('G`a')
    assert v.pos == 4
    v.feed_keys('gg<C-o>')
    assert v.line_of(v.pos) == 1


# ---------------------------------------------------------------- operators
@pytest.mark.parametrize('text,keys,result', [
    ('foo bar baz', 'dw', 'bar baz'),
    ('foo bar baz', '2dw', 'baz'),
    ('foo bar baz', 'd2w', 'baz'),
    ('foo bar\nbaz', 'wdw', 'foo \nbaz'),
    ('foo bar baz', 'de', ' bar baz'),
    ('foo bar baz', 'cwqux<Esc>', 'qux bar baz'),
    ('foo bar baz', 'c2wx<Esc>', 'x baz'),
    ('foo bar baz', 'd$', ''),
    ('foo bar baz', 'wD', 'foo '),
    ('foo bar baz', 'wC!<Esc>', 'foo !'),
    ('a\nb\nc\nd', 'dd', 'b\nc\nd'),
    ('a\nb\nc\nd', '2dd', 'c\nd'),
    ('a\nb\nc\nd', 'dj', 'c\nd'),
    ('a\nb\nc\nd', 'Gdk', 'a\nb'),
    ('a\nb\nc\nd', 'jdG', 'a'),
    ('a\nb\nc\nd', 'jjdgg', 'd'),
    ('cube(size=[1,2,3]);', 'fsdt]', 'cube(]);'),
    ('cube(size=[1,2,3]);', 'f1df]', 'cube(size=[);'),
    ('cube(size=[1,2,3]);', 'f1di[', 'cube(size=[]);'),
    ('cube(size=[1,2,3]);', 'f1da[', 'cube(size=);'),
    ('cube(size=[1,2,3]);', 'f1di(', 'cube();'),
    ('cube(size=[1,2,3]);', 'f1ci(2<Esc>', 'cube(2);'),
    ('cube(size=[1,2,3]);', 'f1dib', 'cube();'),
    ('x = "hello world";', 'fwdi"', 'x = "";'),
    ('x = "hello world";', 'fwda"', 'x =;'),
    ('x = "hi" + 1;', 'fhda"', 'x = + 1;'),
    ('x = "hello world";', 'ci"hi<Esc>', 'x = "hi";'),
    ('foo bar baz', 'wdiw', 'foo  baz'),
    ('foo bar baz', 'wdaw', 'foo baz'),
    ('foo bar baz', 'wciwX<Esc>', 'foo X baz'),
    ('m() {\n    a();\n    b();\n}', 'jdi{', 'm() {\n}'),
    ('m() {\n    a();\n    b();\n}', 'jda{', 'm() '),
    ('a\nb\n\nc\nd', 'dap', 'c\nd'),
    ('a\nb\n\nc\nd', 'dip', '\nc\nd'),
    ('abc', 'x', 'bc'),
    ('abc', '$X', 'ac'),
    ('abc', '2x', 'c'),
    ('abc', 'sZ<Esc>', 'Zbc'),
    ('abc\ndef', 'Snew<Esc>', 'new\ndef'),
    ('abc', 'rx', 'xbc'),
    ('abc', '2rx', 'xxc'),
    ('abc', '~~', 'ABc'),
    ('abc def', 'gUiw', 'ABC def'),
    ('ABC DEF', 'guu', 'abc def'),
    ('abc def', 'g~~', 'ABC DEF'),
    ('a\nb', 'J', 'a b'),
    ('a\n    b\nc', '3J', 'a b c'),
    ('a\nb', 'gJ', 'ab'),
    ('cube();', '>>', '    cube();'),
    ('    cube();', '<<', 'cube();'),
    ('a\nb', '>j', '    a\n    b'),
    ('cube();', 'gcc', '// cube();'),
    ('// cube();', 'gcc', 'cube();'),
    ('x = 9;', '<C-a>', 'x = 10;'),
    ('x = 10;', '5<C-x>', 'x = 5;'),
    ('x = -3;', '<C-a>', 'x = -2;'),
    ('d = 1.6;', 'f6<C-a>', 'd = 1.7;'),
    ('m(){\ncube();\nif(a){\nb();\n}\n}', 'gg=G', 'm(){\n    cube();\n    if(a){\n        b();\n    }\n}'),
])
def test_edits(text, keys, result):
    v = run(text, keys)
    assert T(v) == result


def test_insert_commands_and_autoindent():
    v = run('module m() {', 'o', pos=0)
    assert v.mode == 'INSERT'
    v.feed_keys('cube();<Esc>')
    assert T(v) == 'module m() {\n    cube();'
    v = run('foo', 'Aba<Esc>Iz<Esc>')
    assert T(v) == 'zfooba'
    v = run('a\nc', 'ob<Esc>')
    assert T(v) == 'a\nb\nc'
    v = run('a\nc', 'jOb<Esc>')
    assert T(v) == 'a\nb\nc'
    v = run('m() {}', 'f}i<CR><Esc>')
    assert T(v) == 'm() {\n    \n}'
    v = run('', 'i    x{<CR>y<CR>}<Esc>')
    assert T(v) == '    x{\n        y\n    }'
    v = run('ab', 'a<C-w>x<Esc>')
    assert T(v) == 'xb'


def test_insert_counts_and_dot():
    v = run('', '3ihi<Esc>')
    assert T(v) == 'hihihi'
    v = run('a b c d', 'cwX<Esc>w.w.')
    assert T(v) == 'X X X d'
    v = run('a\nb\nc\nd', 'dd.')
    assert T(v) == 'c\nd'
    v = run('1 2 3 4 5 6', 'dw2.')
    assert T(v) == '4 5 6'
    v = run('x\nx', 'A;<Esc>j.')
    assert T(v) == 'x;\nx;'
    v = run('abc', '3ox<Esc>')
    assert T(v) == 'abc\nx\nx\nx'


def test_replace_mode():
    v = run('abcdef', 'Rxy<Esc>')
    assert T(v) == 'xycdef'
    v = run('ab', 'Rxyz<Esc>')
    assert T(v) == 'xyz'
    v = run('abc', 'Rxy<BS><BS><Esc>')
    assert T(v) == 'abc'


def test_yank_put_registers():
    v = run('foo bar', 'yiwP')
    assert T(v) == 'foofoo bar'
    v = run('a\nb', 'yyp')
    assert T(v) == 'a\na\nb'
    v = run('a\nb', 'yyjp')
    assert T(v) == 'a\nb\na'
    v = run('a\nb', 'yyjP')
    assert T(v) == 'a\na\nb'
    v = run('one two', '"ayiww"byiw0"bP')
    assert T(v) == 'twoone two'
    v.feed_keys('"ap')
    assert v.registers['a'].text == 'one'
    v = run('x y', '"ayiww"Ayiw')
    assert v.registers['a'].text == 'xy'
    v = run('a\nb\nc', 'dddd"2p')
    assert T(v) == 'c\na'
    v = run('abc', 'x"_xp')
    assert T(v) == 'ca'
    v = run('ab', 'yl3p')
    assert T(v) == 'aaaab'
    v = run('foo', 'yiwA <C-r>"<Esc>')
    assert T(v) == 'foo foo'


def test_system_clipboard():
    v = make('hello world', clipboard=True)
    v.feed_keys('yiw')
    assert v.host.clip == 'hello'
    v.host.clip = 'XY'
    v.feed_keys('$p')
    assert T(v) == 'hello worldXY'
    v.host.clip = 'line\n'
    v.feed_keys('P')
    assert T(v) == 'line\nhello worldXY'
    v = make('abc')
    v.feed_keys('"+yl')
    assert v.host.clip == 'a'


def test_visual_modes():
    v = run('foo bar baz', 'wvlld')
    assert T(v) == 'foo  baz'
    v = run('foo bar baz', 'wviwy')
    assert v.registers['"'].text == 'bar'
    v = run('a\nb\nc', 'Vjd')
    assert T(v) == 'c'
    v = run('a\nb\nc', 'Vj>')
    assert T(v) == '    a\n    b\nc'
    v = run('abc\ndef\nghi', 'l<C-v>jjld')
    assert T(v) == 'a\nd\ng'
    v = run('abc\ndef', '<C-v>jIX<Esc>')
    assert T(v) == 'Xabc\nXdef'
    v = run('ab\ncd', '<C-v>j$AZ<Esc>')
    assert T(v) == 'abZ\ncdZ'
    v = run('abc\ndef', 'l<C-v>jy$p')
    assert T(v) == 'abcb\ndefe'
    v = run('foo bar', 'veU')
    assert T(v) == 'FOO bar'
    v = run('foo bar', 'wvep', )
    assert T(v) == 'foo bar'
    v = run('foo bar', 'yiwwviwp')
    assert T(v) == 'foo foo'
    v = run('abc', 'vlrx')
    assert T(v) == 'xxc'
    v = run('f(a(bc))', 'fbvi(i(d')
    assert T(v) == 'f()'
    v = run('a\nb\nc\nd', 'Vjdj.')
    assert T(v) == 'c'
    v = run('a\nb\nc\nd\ne', 'Vj>j.')
    assert T(v) == '    a\n        b\n    c\nd\ne'


def test_gv_and_o():
    v = run('abcdef', 'vll<Esc>gvd')
    assert T(v) == 'def'
    v = run('abcdef', 'lvlloh<Esc>')
    assert v.marks['<'] == 0


def test_undo_redo():
    v = run('abc', 'xxuu')
    assert T(v) == 'abc'
    v.feed_keys('<C-r>')
    assert T(v) == 'bc'
    v = run('abc', 'ciwhello<Esc>u')
    assert T(v) == 'abc'


def test_macros():
    v = run('1\n2\n3\n4', 'qaA!<Esc>jq2@a')
    assert T(v) == '1!\n2!\n3!\n4'
    v.feed_keys('@@')
    assert T(v) == '1!\n2!\n3!\n4!'
    v = run('a\nb\nc', 'qqI- <Esc>jq10@q')
    assert T(v) == '- a\n- b\n- c'


def test_ex_commands():
    v = run('foo foo\nfoo', ':s/foo/bar/<CR>')
    assert T(v) == 'bar foo\nfoo'
    v = run('foo foo\nfoo', ':%s/foo/bar/g<CR>')
    assert T(v) == 'bar bar\nbar'
    v = run('a1 b2', r':s/\(\w\)\(\d\)/\2\1/g<CR>')
    assert T(v) == '1a 2b'
    v = run('size=10; r=10;', ':s/10/&0/g<CR>')
    assert T(v) == 'size=100; r=100;'
    v = run('a\nx\nb\nx', ':g/x/d<CR>')
    assert T(v) == 'a\nb'
    v = run('a\nx\nb\nx', ':v/x/d<CR>')
    assert T(v) == 'x\nx'
    v = run('c\na\nb', ':sort<CR>')
    assert T(v) == 'a\nb\nc'
    v = run('10\n9\n100', ':sort n<CR>')
    assert T(v) == '9\n10\n100'
    v = run('a\nb\nc', ':2d<CR>')
    assert T(v) == 'a\nc'
    v = run('a\nb\nc', ':1,2d<CR>')
    assert T(v) == 'c'
    v = run('a\nb\nc', ':1m$<CR>')
    assert T(v) == 'b\nc\na'
    v = run('a\nb\nc', ':3m0<CR>')
    assert T(v) == 'c\na\nb'
    v = run('a\nb\nc', ':1t.<CR>')
    assert T(v) == 'a\na\nb\nc'
    v = run('a\nb\nc', ':%norm A;<CR>')
    assert T(v) == 'a;\nb;\nc;'
    v = run('a\nb\nc', ':3<CR>')
    assert v.line_of(v.pos) == 2
    v = run('a\nb', 'Vj:s/^/# /<CR>')
    assert T(v) == '# a\n# b'
    v = run('a\nb', ':%j<CR>')
    assert T(v) == 'a b'
    v = run('a\nb', ':%><CR>')
    assert T(v) == '    a\n    b'
    v = run('a', ':set sw=2<CR>>>')
    assert T(v) == '  a'
    v = run('FOO foo', ':set noic<CR>/foo<CR>')
    assert v.pos == 4
    v = run('x', ':w<CR>')
    assert v.host.commands == ['w']
    v = run('foo bar', ':s/foo/x<CR>j:&<CR>')
    assert T(v) == 'x bar'
    v = run('foo\nfoo', ':s/foo/x/<CR>j&')
    assert T(v) == 'x\nx'


def test_cmdline_editing_and_history():
    v = run('abc', ':s/b/X/<CR>u:<Up><CR>')
    assert T(v) == 'aXc'
    v = run('abc', ':xx<BS><BS><Esc>')
    assert v.mode == 'NORMAL'


def test_operator_with_search_motion():
    v = run('cube(1); sphere(2);', 'd/sph<CR>')
    assert T(v) == 'sphere(2);'


def test_insert_oneshot_normal():
    v = run('abc', 'A<C-o>0X<Esc>')
    assert T(v) == 'Xabc'


def test_regex_translation():
    assert vim_regex(r'\<foo\>').search('a foo b')
    assert not vim_regex(r'\<foo\>').search('afoob')
    assert vim_regex(r'a\(b\|c\)').fullmatch('ac')
    assert vim_regex('f(x)').search('f(x)')
    assert vim_regex(r'\vf(x|y)').fullmatch('fy')
    assert vim_regex('Foo').search('foo') is None
    assert vim_regex('foo').search('FOO')


def test_parse_keys():
    assert parse_keys('d2w<Esc><lt>') == ['d', '2', 'w', '<Esc>', '<']
    assert parse_keys('<C-R>') == ['<C-r>']


def test_mode_labels_and_selection():
    v = run('abc\ndef', 'Vj')
    assert v.mode_label.startswith('V-LINE')
    assert v.selection() == [(0, 7)]
    v.feed_keys('<Esc>')
    assert v.selection() == []
