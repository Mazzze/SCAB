import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from scadforge.editor import Editor, Diagnostic

app = QApplication.instance() or QApplication([])

SPECIAL = {'<Esc>': Qt.Key_Escape, '<CR>': Qt.Key_Return, '<BS>': Qt.Key_Backspace}


def type_keys(editor, keys):
    i = 0
    while i < len(keys):
        for name, code in SPECIAL.items():
            if keys.startswith(name, i):
                QTest.keyClick(editor, code)
                i += len(name)
                break
        else:
            if keys.startswith('<C-', i):
                QTest.keyClick(editor, keys[i + 3], Qt.ControlModifier)
                i += 5
                continue
            QTest.keyClicks(editor, keys[i])
            i += 1


def test_vim_through_qt_events():
    e = Editor()
    e.vim_engine.options['clipboard'] = ''
    e.setPlainText('cube(size=[1,2,3]);\nsphere(r=2);')
    e.show()
    type_keys(e, 'f1di[')
    assert e.toPlainText().startswith('cube(size=[]);')
    type_keys(e, 'u')
    assert e.toPlainText().startswith('cube(size=[1,2,3]);')
    type_keys(e, 'j0cwball<Esc>')
    assert e.toPlainText().endswith('ball(r=2);')
    type_keys(e, 'u')
    assert e.toPlainText().endswith('sphere(r=2);'), 'cw + typed text is one undo step'
    type_keys(e, 'ggyyGp')
    assert e.toPlainText() == 'cube(size=[1,2,3]);\nsphere(r=2);\ncube(size=[1,2,3]);'
    type_keys(e, 'f2<C-a><C-a>')
    assert e.toPlainText().endswith('cube(size=[1,4,3]);')
    type_keys(e, 'ggqaA // x<Esc>jq@a')
    assert e.toPlainText().split('\n')[:2] == ['cube(size=[1,2,3]); // x', 'sphere(r=2); // x']
    assert e.mode.startswith('NORMAL')
    type_keys(e, 'i')
    assert e.mode.startswith('INSERT')
    type_keys(e, '<Esc>:3<CR>')
    assert e.textCursor().blockNumber() == 2


def test_diagnostics_render():
    e = Editor()
    e.setPlainText('cube(1)\nsphere(2);')
    e.set_diagnostics([Diagnostic(0, None, 'error', 'syntax error')])
    assert 0 in e.diagnostics_by_line
    e.show()
    e.gutter.repaint()
