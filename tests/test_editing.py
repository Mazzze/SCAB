import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from scab.editor import Editor
from scab.findbar import FindBar, compile_pattern

app = QApplication.instance() or QApplication([])


def plain(text):
    e = Editor()
    e.toggle_vim(False)
    e.setPlainText(text)
    e.show()
    return e


def cursor_to(e, line, column=0):
    e.goto_line(line, column)


def test_auto_pairs_and_skip_closing():
    e = plain('')
    QTest.keyClicks(e, 'cube(')
    assert e.toPlainText() == 'cube()'
    QTest.keyClicks(e, '[1,2')
    QTest.keyClicks(e, ']')
    QTest.keyClicks(e, ')')
    QTest.keyClicks(e, ';')
    assert e.toPlainText() == 'cube([1,2]);'
    e = plain('')
    QTest.keyClicks(e, 'x(')
    QTest.keyClick(e, Qt.Key_Backspace)
    assert e.toPlainText() == 'x'


def test_enter_between_braces_and_smart_home():
    e = plain('')
    QTest.keyClicks(e, 'module m() {')
    QTest.keyClick(e, Qt.Key_Return)
    assert e.toPlainText() == 'module m() {\n    \n}'
    QTest.keyClicks(e, 'cube(1')
    QTest.keyClick(e, Qt.Key_Home)
    assert e.textCursor().positionInBlock() == 4
    QTest.keyClick(e, Qt.Key_Home)
    assert e.textCursor().positionInBlock() == 0


def test_comment_move_duplicate_lines():
    e = plain('a();\nb();\nc();')
    cursor_to(e, 0)
    QTest.keyClick(e, Qt.Key_Slash, Qt.ControlModifier)
    assert e.toPlainText() == '// a();\nb();\nc();'
    QTest.keyClick(e, Qt.Key_Slash, Qt.ControlModifier)
    assert e.toPlainText() == 'a();\nb();\nc();'
    QTest.keyClick(e, Qt.Key_Down, Qt.AltModifier)
    assert e.toPlainText() == 'b();\na();\nc();' and e.textCursor().blockNumber() == 1
    QTest.keyClick(e, Qt.Key_Up, Qt.AltModifier)
    assert e.toPlainText() == 'a();\nb();\nc();'
    QTest.keyClick(e, Qt.Key_D, Qt.ControlModifier)
    assert e.toPlainText() == 'a();\na();\nb();\nc();' and e.textCursor().blockNumber() == 1
    e.undo()
    assert e.toPlainText() == 'a();\nb();\nc();'


def test_shortcuts_in_vim_insert_mode_only():
    e = Editor()
    e.setPlainText('a();\nb();')
    e.show()
    asked = []
    e.findRequested.connect(asked.append)
    QTest.keyClick(e, Qt.Key_F, Qt.ControlModifier)        # normal mode: Vim page down, no find bar
    assert asked == []
    QTest.keyClicks(e, 'i')
    QTest.keyClick(e, Qt.Key_F, Qt.ControlModifier)
    QTest.keyClick(e, Qt.Key_H, Qt.ControlModifier)
    assert asked == [False, True]
    QTest.keyClick(e, Qt.Key_Slash, Qt.ControlModifier)
    assert e.toPlainText() == 'a();\n// b();'      # Ctrl-F in normal mode paged down to line 2 first


def test_find_bar_next_count_and_replace():
    e = plain('cube(10);\nCube(2);\nsphere(10);\ncubes(1);')
    bar = FindBar(lambda: e)
    bar.find.setText('cube')
    bar.show()
    bar.refresh()
    assert len(bar.matches()) == 3
    bar.word.setChecked(True)
    assert len(bar.matches()) == 2
    bar.case.setChecked(True)
    assert [m.group(0) for m in bar.matches()] == ['cube']
    bar.case.setChecked(False)
    first = bar.next()
    second = bar.next()
    third = bar.next()
    assert (first.start(), second.start(), third.start()) == (0, 10, 0)      # wraps around
    assert bar.count.text() == '1 of 2'
    bar.find.setText(r'(\w+)\((\d+)\)')
    bar.word.setChecked(False)
    bar.regex.setChecked(True)
    bar.replace.setText('$1(\\2 * 2)')
    assert bar.replace_all() == 4
    assert e.toPlainText() == 'cube(10 * 2);\nCube(2 * 2);\nsphere(10 * 2);\ncubes(1 * 2);'
    e.undo()
    assert e.toPlainText() == 'cube(10);\nCube(2);\nsphere(10);\ncubes(1);'
    assert compile_pattern('(', regex=True) is None and compile_pattern('(').pattern == r'\('
