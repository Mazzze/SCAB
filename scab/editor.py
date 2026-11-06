"""Source editor: syntax highlighting, completion, diagnostics and full Vim emulation."""
import re
from PySide6.QtCore import Qt, Signal, QStringListModel, QTimer, QEvent, QRect
from PySide6.QtGui import (QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QGuiApplication,
                           QTextCursor, QTextFormat, QPainter)
from PySide6.QtWidgets import QPlainTextEdit, QCompleter, QTextEdit, QWidget, QToolTip
from .model import SIGNATURES, TOKEN
from .vim import Vim, Host, Abort

SEVERITY_COLORS = {'error': '#e5606d', 'warning': '#d9a343', 'info': '#6c9ce0'}
KEYWORDS = ('module', 'function', 'if', 'else', 'let', 'each', 'true', 'false', 'undef', 'include', 'use',
            'for', 'intersection_for', 'assert', 'echo', 'children')

QT_KEYS = {Qt.Key_Escape: '<Esc>', Qt.Key_Return: '<CR>', Qt.Key_Enter: '<CR>', Qt.Key_Backspace: '<BS>',
           Qt.Key_Delete: '<Del>', Qt.Key_Tab: '<Tab>', Qt.Key_Backtab: '<S-Tab>', Qt.Key_Up: '<Up>',
           Qt.Key_Down: '<Down>', Qt.Key_Left: '<Left>', Qt.Key_Right: '<Right>', Qt.Key_Home: '<Home>',
           Qt.Key_End: '<End>', Qt.Key_PageUp: '<PageUp>', Qt.Key_PageDown: '<PageDown>', Qt.Key_Insert: '<Insert>'}
# Ctrl+key combinations the editor keeps for Vim instead of the window's menu shortcuts.
VIM_CTRL = set('RDUFBEYOIAXVWGNPHJTKLC[]') - {'K', 'L'}


class Highlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        def paint(start, length, color, bold=False):
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            self.setFormat(start, length, f)
        state = self.previousBlockState()
        depth = max(0, state) // 2
        in_comment = bool(max(0, state) % 2)
        palette = ('#a9b7c6', '#c8a26a', '#8fa8c8', '#9fb88a', '#b59ac9', '#a9b7c6')
        comment_end = 0
        for m in TOKEN.finditer(text):
            v, start = m[0], m.start()
            if start < comment_end:
                continue
            if in_comment:
                close = text.find('*/', start)
                if close < 0:
                    paint(start, len(text) - start, '#7a7e85')
                    break
                paint(start, close + 2 - start, '#7a7e85')
                in_comment = False
                comment_end = close + 2
                continue
            color = None
            if v.startswith('//'):
                paint(start, len(v), '#7a7e85')
                break
            if v.startswith('/*'):
                color = '#7a7e85'
                if not v.endswith('*/') or len(v) < 4:
                    in_comment = True
            elif v == '/' and text[start:start + 2] == '/*':
                paint(start, len(text) - start, '#7a7e85')
                in_comment = True
                break
            elif v.startswith('"'):
                color = '#6aab73'
            elif v in ('(', '[', '{'):
                color = palette[depth % len(palette)]
                depth += 1
            elif v in (')', ']', '}'):
                depth = max(0, depth - 1)
                color = palette[depth % len(palette)]
            elif re.fullmatch(r'\d+(?:\.\d*)?(?:[eE][+-]?\d+)?', v):
                color = '#2aacb8'
            elif v in KEYWORDS:
                color = '#cf8e6d'
            elif v in SIGNATURES:
                color = '#56a8f5'
            elif v.startswith('$'):
                color = '#c77dbb'
            if color:
                paint(start, len(v), color)
        self.setCurrentBlockState(depth * 2 + int(in_comment))


class Gutter(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.setMouseTracking(True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(event.rect(), QColor('#1e1f22'))
        editor = self.editor
        block = editor.firstVisibleBlock()
        top = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top()
        current = editor.textCursor().blockNumber()
        relative = editor.vim_engine.options.get('relativenumber') and editor.vim
        diagnostics = editor.diagnostics_by_line
        while block.isValid() and top <= event.rect().bottom():
            h = editor.blockBoundingRect(block).height()
            number = block.blockNumber()
            if block.isVisible() and top + h >= event.rect().top():
                problems = diagnostics.get(number)
                if problems:
                    severity = 'error' if any(d.severity == 'error' for d in problems) else problems[0].severity
                    p.setBrush(QColor(SEVERITY_COLORS.get(severity, '#ff5c6c')))
                    p.setPen(Qt.NoPen)
                    p.drawEllipse(4, int(top + h / 2 - 4), 8, 8)
                label = str(abs(number - current)) if relative and number != current else str(number + 1)
                p.setPen(QColor('#a1a3ab' if number == current else '#4b5059'))
                p.drawText(0, int(top), self.width() - 8, int(h), Qt.AlignRight | Qt.AlignVCenter, label)
            block = block.next()
            top += h

    def mouseMoveEvent(self, event):
        block = self.editor.cursorForPosition(event.position().toPoint()).block()
        problems = self.editor.diagnostics_by_line.get(block.blockNumber())
        if problems:
            QToolTip.showText(event.globalPosition().toPoint(), '\n'.join(d.text() for d in problems), self)
        else:
            QToolTip.hideText()


class QtBuffer:
    """Adapts a QTextDocument to the Vim engine's buffer interface (Python str indices)."""
    def __init__(self, editor):
        self.editor = editor
        self._revision = None
        self._text = ''
        self._bmp = True

    def text(self):
        doc = self.editor.document()
        revision = doc.revision()
        if revision != self._revision or self._revision is None:
            self._text = self.editor.toPlainText()
            self._bmp = len(self._text.encode('utf-16-le')) == 2 * len(self._text)
            self._revision = revision
        return self._text

    def to_qt(self, index):
        text = self.text()
        if self._bmp:
            return index
        return len(text[:index].encode('utf-16-le')) // 2

    def to_py(self, units):
        text = self.text()
        if self._bmp:
            return min(units, len(text))
        return len(text.encode('utf-16-le')[:units * 2].decode('utf-16-le', errors='ignore'))

    def replace(self, a, b, s, join=False):
        qa, qb = self.to_qt(a), self.to_qt(b)
        cursor = QTextCursor(self.editor.document())
        if join:
            cursor.joinPreviousEditBlock()
        else:
            cursor.beginEditBlock()
        cursor.setPosition(qa)
        cursor.setPosition(qb, QTextCursor.KeepAnchor)
        cursor.insertText(s)
        cursor.endEditBlock()

    def _history(self, redo):
        doc = self.editor.document()
        if not (doc.isRedoAvailable() if redo else doc.isUndoAvailable()):
            return None
        cursor = QTextCursor(doc)
        cursor.setPosition(self.editor.textCursor().position())
        (doc.redo if redo else doc.undo)(cursor)
        self._revision = None
        return self.to_py(cursor.selectionStart() if cursor.hasSelection() else cursor.position())

    def undo(self):
        return self._history(False)

    def redo(self):
        return self._history(True)


class EditorHost(Host):
    def __init__(self, editor):
        self.editor = editor

    def clipboard_get(self):
        clipboard = QGuiApplication.clipboard()
        return clipboard.text() if clipboard is not None else None

    def clipboard_set(self, text):
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    def message(self, text, error=False):
        self.editor.message.emit(('E: ' if error and not text.startswith('E') else '') + text if error else text)

    def ex_command(self, command):
        handler = self.editor.ex_handler
        return bool(handler(command)) if handler else False

    def visible_lines(self):
        editor = self.editor
        first = editor.firstVisibleBlock().blockNumber()
        height = max(1, editor.fontMetrics().lineSpacing())
        return first, first + max(1, editor.viewport().height() // height) - 1

    def scroll(self, kind, amount=0):
        self.editor.pending_scroll = (kind, amount)

    def goto_definition(self, word):
        handler = self.editor.definition_handler
        return bool(handler(word)) if handler else False

    def option_changed(self, name, value):
        editor = self.editor
        if name in ('tabstop',):
            editor.setTabStopDistance(editor.fontMetrics().horizontalAdvance(' ') * value)
        editor.update_gutter()

    def search_preview(self, start, end):
        self.editor.preview_range = (start, end) if start >= 0 else None
        if start >= 0:
            cursor = self.editor.textCursor()
            cursor.setPosition(self.editor.buffer.to_qt(start))
            self.editor.setTextCursor(cursor)
            self.editor.ensureCursorVisible()
        self.editor.refresh_decorations()

    def complete(self, forward=True):
        self.editor.popup(True)


class Diagnostic:
    __slots__ = ('line', 'column', 'severity', 'message', 'file')

    def __init__(self, line, column, severity, message, file=None):
        self.line, self.column, self.severity, self.message, self.file = line, column, severity, message, file

    def text(self):
        return f'{self.severity.upper()} line {self.line + 1}: {self.message}'


class Editor(QPlainTextEdit):
    modeChanged = Signal(str)
    hintChanged = Signal(str)
    command = Signal(str)
    message = Signal(str)
    findRequested = Signal(bool)          # True: with replace
    findNextRequested = Signal(bool)      # True: backwards
    gotoLineRequested = Signal()

    def __init__(self):
        super().__init__()
        font = QFont()
        font.setFamilies(['JetBrains Mono', 'Cascadia Mono', 'Consolas', 'Menlo', 'DejaVu Sans Mono', 'monospace'])
        font.setPointSize(11)
        font.setStyleHint(QFont.Monospace)
        self.setFont(font)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * 4)
        self.highlighter = Highlighter(self.document())
        self.vim = True
        self.buffer = QtBuffer(self)
        self.vim_engine = Vim(self.buffer, EditorHost(self))
        self.ex_handler = None
        self.definition_handler = None
        self.pending_scroll = None
        self.preview_range = None
        self.syncing = False
        self.diagnostics = []
        self.diagnostics_by_line = {}
        self.find_regex = None
        self.auto_pairs = True
        self.all_words = set(SIGNATURES)
        self.completer = QCompleter(self)
        self.word_model = QStringListModel(self.completer)
        self.completer.setModel(self.word_model)
        self.completer.setWidget(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitive)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.activated.connect(self.complete)
        self.gutter = Gutter(self)
        self.words_timer = QTimer(self)
        self.words_timer.setSingleShot(True)
        self.words_timer.timeout.connect(self.update_words)
        self.blockCountChanged.connect(self.update_gutter)
        self.updateRequest.connect(lambda *_: self.gutter.update())
        self.updateRequest.connect(self.update_search_highlight)
        self.cursorPositionChanged.connect(self.cursor_changed)
        self.textChanged.connect(lambda: self.words_timer.start(400))
        self.viewport().installEventFilter(self)
        self.update_gutter()
        self.update_words()
        self.apply_cursor_shape()

    # ----------------------------------------------------------- properties
    @property
    def mode(self):
        if not self.vim:
            return 'EDIT'
        return self.vim_engine.mode_label

    def set_mode(self, mode):
        engine = self.vim_engine
        if mode == 'NORMAL' and engine.mode in ('INSERT', 'REPLACE'):
            engine.feed('<Esc>')
        elif mode == 'INSERT' and engine.mode == 'NORMAL':
            engine.feed('i')
        self.sync_from_engine()

    def toggle_vim(self, checked):
        self.vim = checked
        if checked:
            self.vim_engine.mode = 'NORMAL'
            self.vim_engine.keys = []
        self.apply_cursor_shape()
        self.refresh_decorations()
        self.modeChanged.emit(self.mode)

    # ---------------------------------------------------------------- gutter
    def gutter_width(self):
        digits = max(3, len(str(self.blockCount())))
        return 22 + self.fontMetrics().horizontalAdvance('9') * digits

    def update_gutter(self, *_):
        width = self.gutter_width()
        self.setViewportMargins(width + 2, 0, 0, 0)
        self.gutter.setGeometry(0, 0, width, self.height())
        self.gutter.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update_gutter()

    # ------------------------------------------------------------ completion
    def update_words(self):
        words = set(re.findall(r'[$A-Za-z_][$\w]*', self.toPlainText())) | set(SIGNATURES) | set(KEYWORDS)
        self.all_words = words

    def complete(self, word):
        c = self.textCursor()
        c.movePosition(QTextCursor.StartOfWord, QTextCursor.KeepAnchor)
        c.insertText(word)
        self.setTextCursor(c)
        if self.vim:
            self.vim_engine.refresh()
            self.vim_engine.pos = self.buffer.to_py(self.textCursor().position())

    def popup(self, force=False):
        c = self.textCursor()
        c.select(QTextCursor.WordUnderCursor)
        word = c.selectedText()
        if not force and len(word) < 2:
            self.completer.popup().hide()
            return
        candidates = sorted(w for w in self.all_words if w.startswith(word) and w != word)
        self.word_model.setStringList(candidates)
        if not candidates:
            self.completer.popup().hide()
            return
        self.completer.setCompletionPrefix(word)
        self.completer.setCurrentRow(0)
        rect = self.cursorRect()
        rect.setWidth(310)
        self.completer.complete(rect)

    # ---------------------------------------------------------- source api
    def select_source(self, start, end):
        source = self.toPlainText()
        c = self.textCursor()
        c.setPosition(len(source[:start].encode('utf-16-le')) // 2)
        c.setPosition(len(source[:end].encode('utf-16-le')) // 2, QTextCursor.KeepAnchor)
        self.setTextCursor(c)
        self.centerCursor()
        if self.vim:
            engine = self.vim_engine
            engine.refresh()
            engine.pos = engine.clamp(start)
            if engine.mode != 'NORMAL':
                engine.mode = 'NORMAL'
            self.modeChanged.emit(self.mode)

    def goto_line(self, line, column=0):
        block = self.document().findBlockByNumber(max(0, line))
        if not block.isValid():
            return
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + min(max(0, column), max(0, block.length() - 1)))
        self.setTextCursor(cursor)
        self.centerCursor()
        self.vim_engine.refresh()
        self.vim_engine.pos = self.buffer.to_py(cursor.position())
        self.setFocus()

    def source_position(self):
        units = self.textCursor().position()
        return len(self.toPlainText().encode('utf-16-le')[:units * 2].decode('utf-16-le'))

    # ------------------------------------------------------------ diagnostics
    def set_diagnostics(self, diagnostics):
        self.diagnostics = list(diagnostics)
        by_line = {}
        for d in self.diagnostics:
            by_line.setdefault(d.line, []).append(d)
        self.diagnostics_by_line = by_line
        self.refresh_decorations()
        self.gutter.update()

    def eventFilter(self, obj, event):
        if obj is self.viewport() and event.type() == QEvent.ToolTip:
            block = self.cursorForPosition(event.pos()).block()
            problems = self.diagnostics_by_line.get(block.blockNumber())
            if problems:
                QToolTip.showText(event.globalPos(), '\n'.join(d.text() for d in problems), self)
            else:
                QToolTip.hideText()
            return True
        return super().eventFilter(obj, event)

    # ----------------------------------------------------------- decorations
    def apply_cursor_shape(self):
        engine = self.vim_engine
        block = self.vim and engine.mode not in ('INSERT', 'COMMAND')
        if self.vim and engine.mode == 'REPLACE':
            block = False
        self.setCursorWidth(max(2, self.fontMetrics().horizontalAdvance('M')) if block else 2)

    def refresh_decorations(self):
        selections = []
        line = QTextEdit.ExtraSelection()
        line.format.setBackground(QColor('#26282e'))
        line.format.setProperty(QTextFormat.FullWidthSelection, True)
        line.cursor = QTextCursor(self.textCursor())
        line.cursor.clearSelection()
        selections.append(line)
        for d in self.diagnostics:
            block = self.document().findBlockByNumber(d.line)
            if not block.isValid():
                continue
            fmt = QTextCharFormat()
            fmt.setUnderlineStyle(QTextCharFormat.WaveUnderline)
            fmt.setUnderlineColor(QColor(SEVERITY_COLORS.get(d.severity, '#ff5c6c')))
            if d.severity == 'error':
                fmt.setBackground(QColor(120, 40, 46, 70))
            cursor = QTextCursor(block)
            text = block.text()
            start = d.column if d.column is not None and d.column < len(text) else len(text) - len(text.lstrip())
            cursor.setPosition(block.position() + start)
            end = block.position() + (len(text) if len(text) > start else start + 1)
            cursor.setPosition(min(end, self.document().characterCount() - 1), QTextCursor.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.format = fmt
            sel.cursor = cursor
            selections.append(sel)
        selections.extend(self.search_selections())
        if self.vim:
            engine = self.vim_engine
            for a, b in engine.selection():
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(QColor('#214283'))
                cursor = QTextCursor(self.document())
                cursor.setPosition(self.buffer.to_qt(a))
                cursor.setPosition(self.buffer.to_qt(b), QTextCursor.KeepAnchor)
                sel.cursor = cursor
                selections.append(sel)
            if self.preview_range:
                a, b = self.preview_range
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(QColor('#2d5a37'))
                sel.format.setForeground(QColor('#ffffff'))
                cursor = QTextCursor(self.document())
                cursor.setPosition(self.buffer.to_qt(a))
                cursor.setPosition(self.buffer.to_qt(b), QTextCursor.KeepAnchor)
                sel.cursor = cursor
                selections.append(sel)
        selections.extend(self.bracket_selections())
        self.setExtraSelections(selections)

    def search_selections(self):
        regex = self.find_regex
        if regex is None and self.vim:
            regex = self.vim_engine.highlight_regex()
        if regex is None:
            return []
        first = self.firstVisibleBlock()
        if not first.isValid():
            return []
        height = max(1, self.fontMetrics().lineSpacing())
        lines = self.viewport().height() // height + 2
        last = self.document().findBlockByNumber(first.blockNumber() + lines)
        start_qt = first.position()
        end_qt = last.position() + last.length() if last.isValid() else self.document().characterCount() - 1
        text = self.buffer.text()
        a, b = self.buffer.to_py(start_qt), self.buffer.to_py(end_qt)
        out = []
        for m in regex.finditer(text, a, b):
            if m.end() == m.start():
                continue
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(QColor('#32593d'))
            cursor = QTextCursor(self.document())
            cursor.setPosition(self.buffer.to_qt(m.start()))
            cursor.setPosition(self.buffer.to_qt(m.end()), QTextCursor.KeepAnchor)
            sel.cursor = cursor
            out.append(sel)
            if len(out) > 400:
                break
        return out

    def update_search_highlight(self, rect, dy):
        if dy and (self.find_regex is not None or (self.vim and self.vim_engine.highlight_regex() is not None)):
            QTimer.singleShot(0, self.refresh_decorations)

    def bracket_selections(self):
        text = self.buffer.text()
        p = self.buffer.to_py(self.textCursor().position())
        for q in (p, p - 1):
            if 0 <= q < len(text) and text[q] in '()[]{}':
                engine = self.vim_engine
                engine.refresh()
                try:
                    match = engine.match_bracket(q)
                except (IndexError, KeyError):
                    match = None
                if match is None:
                    return []
                out = []
                for position in (q, match):
                    sel = QTextEdit.ExtraSelection()
                    sel.format.setBackground(QColor('#43454a'))
                    cursor = QTextCursor(self.document())
                    cursor.setPosition(self.buffer.to_qt(position))
                    cursor.setPosition(self.buffer.to_qt(position + 1), QTextCursor.KeepAnchor)
                    sel.cursor = cursor
                    out.append(sel)
                return out
        return []

    # ----------------------------------------------------------------- cursor
    def cursor_changed(self):
        c = self.textCursor()
        text = self.buffer.text()
        position = self.buffer.to_py(c.position())
        before = text[max(0, position - 6000):position]
        stack = []
        for m in TOKEN.finditer(before):
            if m[0].startswith(('"', '//', '/*')):
                continue
            if m[0] in ('(', '['):
                stack.append((m[0], m.start()))
            elif m[0] in (')', ']') and stack:
                stack.pop()
        call = next((pos for kind, pos in reversed(stack) if kind == '('), None)
        m = re.search(r'([$\w]+)\s*$', before[:call] if call is not None else before)
        self.hintChanged.emit(SIGNATURES.get(m[1], '') if m else '')
        if not self.syncing and self.vim:
            engine = self.vim_engine
            engine.refresh()
            if engine.mode not in ('COMMAND',):
                engine.pos = position
        self.refresh_decorations()
        self.gutter.update()

    def sync_from_engine(self):
        engine = self.vim_engine
        self.syncing = True
        try:
            cursor = self.textCursor()
            target = self.buffer.to_qt(min(engine.pos, len(self.buffer.text())))
            if cursor.position() != target or cursor.hasSelection():
                cursor.setPosition(target)
                self.setTextCursor(cursor)
            if self.pending_scroll:
                kind, amount = self.pending_scroll
                self.pending_scroll = None
                bar = self.verticalScrollBar()
                if kind == 'lines':
                    bar.setValue(bar.value() + amount)
                elif kind == 'center':
                    self.centerCursor()
                elif kind in ('top', 'bottom'):
                    line = self.textCursor().blockNumber()
                    first, last = engine.host.visible_lines()
                    bar.setValue(max(0, line if kind == 'top' else line - (last - first)))
            self.ensureCursorVisible()
        finally:
            self.syncing = False
        self.apply_cursor_shape()
        self.refresh_decorations()
        self.gutter.update()
        self.modeChanged.emit(self.mode)

    # -------------------------------------------------------------- keyboard
    @staticmethod
    def key_name(e):
        key, text, mods = e.key(), e.text(), e.modifiers()
        ctrl = bool(mods & (Qt.ControlModifier | Qt.MetaModifier)) if False else bool(mods & Qt.ControlModifier)
        if key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr, Qt.Key_CapsLock):
            return None
        if ctrl:
            if key == Qt.Key_BracketLeft:
                return '<Esc>'
            if key == Qt.Key_BracketRight:
                return '<C-]>'
            if key == Qt.Key_Space:
                return '<C-Space>'
            if Qt.Key_A <= key <= Qt.Key_Z:
                return '<C-%s>' % chr(key).lower()
            return None
        if key in QT_KEYS:
            if key == Qt.Key_Backtab or (key == Qt.Key_Tab and mods & Qt.ShiftModifier):
                return '<S-Tab>'
            return QT_KEYS[key]
        if text and text.isprintable():
            return text
        return None

    def event(self, e):
        # Let Vim own Ctrl+letter keys (Ctrl-R redo, Ctrl-O jumps, Ctrl-V block...)
        if e.type() == QEvent.ShortcutOverride and self.conventional_mode() and self.is_editing_shortcut(e):
            e.accept()
            return True
        if e.type() == QEvent.ShortcutOverride and self.vim:
            mods = e.modifiers()
            if mods == Qt.ControlModifier and Qt.Key_A <= e.key() <= Qt.Key_Z and chr(e.key()) in VIM_CTRL:
                e.accept()
                return True
            if e.key() == Qt.Key_Escape:
                e.accept()
                return True
        return super().event(e)

    def keyPressEvent(self, e):
        key = self.key_name(e)
        popup = self.completer.popup()
        if popup.isVisible() and key in ('<CR>', '<Tab>', '<Up>', '<Down>', '<C-n>', '<C-p>'):
            if key in ('<CR>', '<Tab>'):
                index = popup.currentIndex()
                word = index.data() if index.isValid() else self.completer.currentCompletion()
                if word:
                    self.complete(word)
                popup.hide()
                return
            index = popup.currentIndex()
            row = max(0, index.row()) + (1 if key in ('<Down>', '<C-n>') else -1)
            model = self.completer.completionModel()
            popup.setCurrentIndex(model.index(max(0, min(row, model.rowCount() - 1)), 0))
            return
        if key == '<C-Space>':
            self.popup(True)
            return
        if self.conventional_mode() and self.editing_shortcut(e):
            return
        if not self.vim:
            self.conventional_key(e)
            return
        if key is None:
            return
        if key == '<Esc>':
            popup.hide()
        engine = self.vim_engine
        was_insert = engine.mode in ('INSERT', 'REPLACE')
        engine.refresh()
        engine.pos = engine.clamp(self.buffer.to_py(self.textCursor().position()), insert=was_insert) if not self.syncing and engine.mode != 'COMMAND' else engine.pos
        if engine.mode not in ('INSERT', 'REPLACE', 'COMMAND') and not engine.keys:
            engine.pos = engine.clamp(engine.pos)
        try:
            engine.feed(key)
        except Abort:
            pass
        except Exception as error:  # never lose the editor to an engine bug
            engine.keys = []
            engine.mode = 'NORMAL'
            self.message.emit(f'Vim error: {error}')
        self.sync_from_engine()
        if engine.mode in ('INSERT', 'REPLACE') and len(key) == 1 and (key.isalnum() or key in '_$'):
            self.popup()
        elif popup.isVisible() and (len(key) != 1 or not (key.isalnum() or key in '_$')):
            popup.hide()

    def inputMethodEvent(self, e):
        if self.vim and self.vim_engine.mode not in ('INSERT', 'REPLACE'):
            for ch in e.commitString():
                self.vim_engine.feed(ch)
            self.sync_from_engine()
            e.accept()
            return
        super().inputMethodEvent(e)

    # ------------------------------------------------- conventional editing
    def conventional_mode(self):
        """Usual editor keys apply without Vim, and in Vim's Insert / Replace mode."""
        return not self.vim or self.vim_engine.mode in ('INSERT', 'REPLACE')

    @staticmethod
    def shortcut_name(e):
        key, mods = e.key(), e.modifiers()
        ctrl, shift, alt = bool(mods & Qt.ControlModifier), bool(mods & Qt.ShiftModifier), bool(mods & Qt.AltModifier)
        if ctrl and not alt:
            name = {Qt.Key_F: 'find', Qt.Key_H: 'replace', Qt.Key_G: 'goto', Qt.Key_Slash: 'comment',
                    Qt.Key_D: 'duplicate', Qt.Key_Z: 'redo' if shift else 'undo', Qt.Key_Y: 'redo'}.get(key)
            if name:
                return name
        if key == Qt.Key_F3 and not ctrl:
            return 'previous' if shift else 'next'
        if alt and not ctrl and key in (Qt.Key_Up, Qt.Key_Down):
            return 'move_up' if key == Qt.Key_Up else 'move_down'
        return None

    def is_editing_shortcut(self, e):
        return self.shortcut_name(e) is not None

    def editing_shortcut(self, e):
        name = self.shortcut_name(e)
        if name is None:
            return False
        if name in ('find', 'replace'):
            self.findRequested.emit(name == 'replace')
        elif name in ('next', 'previous'):
            self.findNextRequested.emit(name == 'previous')
        elif name == 'goto':
            self.gotoLineRequested.emit()
        elif name == 'undo':
            self.undo()
        elif name == 'redo':
            self.redo()
        elif name == 'comment':
            self.toggle_comment_lines()
        elif name == 'duplicate':
            self.duplicate_selection()
        elif name in ('move_up', 'move_down'):
            self.move_lines(-1 if name == 'move_up' else 1)
        self.vim_engine.refresh()
        return True

    def selected_lines(self):
        cursor = self.textCursor()
        doc = self.document()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        first = doc.findBlock(start)
        last = doc.findBlock(end)
        if end > start and end == last.position() and last.blockNumber() > first.blockNumber():
            last = last.previous()
        return first.blockNumber(), last.blockNumber()

    def select_lines(self, first, last):
        doc = self.document()
        a, b = doc.findBlockByNumber(first), doc.findBlockByNumber(last)
        cursor = self.textCursor()
        cursor.setPosition(a.position())
        cursor.setPosition(b.position() + len(b.text()), QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)

    def toggle_comment_lines(self):
        engine = self.vim_engine
        engine.refresh()
        first, last = self.selected_lines()
        had_selection = self.textCursor().hasSelection()
        column = self.textCursor().positionInBlock()
        engine.begin()
        engine.toggle_comment(first, last)
        if had_selection:
            self.select_lines(first, last)
        else:
            block = self.document().findBlockByNumber(first)
            cursor = self.textCursor()
            cursor.setPosition(block.position() + min(column, len(block.text())))
            self.setTextCursor(cursor)

    def move_lines(self, direction):
        engine = self.vim_engine
        engine.refresh()
        first, last = self.selected_lines()
        lines = engine.t.split('\n')
        if (direction < 0 and first == 0) or (direction > 0 and last >= len(lines) - 1):
            return
        column = self.textCursor().positionInBlock()
        had_selection = self.textCursor().hasSelection()
        block = lines[first:last + 1]
        if direction < 0:
            start, stop = first - 1, last
            new = block + [lines[first - 1]]
        else:
            start, stop = first, last + 1
            new = [lines[last + 1]] + block
        a = engine.line_start(start)
        b = engine.line_end(stop)
        engine.begin()
        engine.replace(a, b, '\n'.join(new))
        first, last = first + direction, last + direction
        if had_selection:
            self.select_lines(first, last)
        else:
            target = self.document().findBlockByNumber(first)
            cursor = self.textCursor()
            cursor.setPosition(target.position() + min(column, len(target.text())))
            self.setTextCursor(cursor)

    def duplicate_selection(self):
        engine = self.vim_engine
        engine.refresh()
        cursor = self.textCursor()
        selected = cursor.selectedText()
        engine.begin()
        if selected and '\u2029' not in selected:
            end = self.buffer.to_py(cursor.selectionEnd())
            text = selected
            engine.replace(end, end, text)
            cursor.setPosition(self.buffer.to_qt(end))
            cursor.setPosition(self.buffer.to_qt(end + len(text)), QTextCursor.KeepAnchor)
            self.setTextCursor(cursor)
            return
        first, last = self.selected_lines()
        column = cursor.positionInBlock()
        b = engine.line_end(last)
        text = engine.t[engine.line_start(first):b]
        engine.replace(b, b, '\n' + text)
        offset = last - first + 1
        if cursor.hasSelection():
            self.select_lines(first + offset, last + offset)
        else:
            target = self.document().findBlockByNumber(first + offset)
            moved = self.textCursor()
            moved.setPosition(target.position() + min(column, len(target.text())))
            self.setTextCursor(moved)

    PAIRS = {'(': ')', '[': ']', '{': '}', '"': '"'}

    def conventional_key(self, e):
        key, mods = e.key(), e.modifiers()
        text = e.text()
        cursor = self.textCursor()
        block_text = cursor.block().text()
        column = cursor.positionInBlock()
        after = block_text[column:column + 1]
        before = block_text[column - 1:column] if column else ''
        if key == Qt.Key_Home and not mods & (Qt.ControlModifier | Qt.AltModifier):
            indent = len(block_text) - len(block_text.lstrip())
            target = indent if column != indent else 0
            move = QTextCursor.KeepAnchor if mods & Qt.ShiftModifier else QTextCursor.MoveAnchor
            cursor.setPosition(cursor.block().position() + target, move)
            self.setTextCursor(cursor)
            return
        if self.auto_pairs and text and not mods & (Qt.ControlModifier | Qt.AltModifier):
            if text in self.PAIRS:
                closing = self.PAIRS[text]
                if cursor.hasSelection():
                    selected = cursor.selectedText()
                    cursor.insertText(text + selected + closing)
                    return
                in_string = block_text[:column].count('"') % 2 == 1
                if text == '"' and after == '"':
                    cursor.movePosition(QTextCursor.Right)
                    self.setTextCursor(cursor)
                    return
                if (not after or after in ' \t)]};,') and not (text == '"' and (in_string or before.isalnum())):
                    cursor.insertText(text + closing)
                    cursor.movePosition(QTextCursor.Left)
                    self.setTextCursor(cursor)
                    return
            elif text in ')]}' and after == text:
                cursor.movePosition(QTextCursor.Right)
                self.setTextCursor(cursor)
                return
        if key == Qt.Key_Backspace and not cursor.hasSelection() and self.auto_pairs and before in self.PAIRS \
                and after == self.PAIRS[before]:
            cursor.movePosition(QTextCursor.Left)
            cursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, 2)
            cursor.removeSelectedText()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter) and before == '{' and after == '}':
            indent = re.match(r'\s*', block_text)[0]
            cursor.insertText('\n' + indent + '    ' + '\n' + indent)
            cursor.movePosition(QTextCursor.Up)
            cursor.movePosition(QTextCursor.EndOfLine)
            self.setTextCursor(cursor)
            return
        if key in (Qt.Key_Tab, Qt.Key_Backtab):
            cursor = self.textCursor()
            if cursor.hasSelection() or key == Qt.Key_Backtab:
                self.indent_lines(key == Qt.Key_Backtab or bool(mods & Qt.ShiftModifier))
                return
            self.insertPlainText('    ')
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            line = self.textCursor().block().text()
            indent = re.match(r'\s*', line)[0]
            before = line[:self.textCursor().positionInBlock()].rstrip()
            self.insertPlainText('\n' + indent + ('    ' if before.endswith('{') else ''))
            return
        super().keyPressEvent(e)
        text = e.text()
        if text and (text.isalnum() or text in '_$'):
            self.popup()
        else:
            self.completer.popup().hide()

    def indent_lines(self, outdent=False):
        cursor = self.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        first = self.document().findBlock(start)
        last = self.document().findBlock(end)
        if end > start and end == last.position():
            last = last.previous()
        first_number, last_number = first.blockNumber(), last.blockNumber()
        edit = QTextCursor(self.document())
        edit.beginEditBlock()
        block = last
        while block.isValid() and block.blockNumber() >= first_number:
            edit.setPosition(block.position())
            if outdent:
                text = block.text()
                count = 1 if text.startswith('\t') else min(4, len(text) - len(text.lstrip(' ')))
                edit.setPosition(block.position() + count, QTextCursor.KeepAnchor)
                edit.removeSelectedText()
            else:
                edit.insertText('    ')
            block = block.previous()
        edit.endEditBlock()
        cursor.setPosition(self.document().findBlockByNumber(first_number).position())
        block = self.document().findBlockByNumber(last_number)
        cursor.setPosition(block.position() + len(block.text()), QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        self.completer.popup().hide()

    # ----------------------------------------------------------------- mouse
    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if not self.vim:
            return
        cursor = self.textCursor()
        engine = self.vim_engine
        engine.refresh()
        if cursor.hasSelection() and engine.mode not in ('INSERT', 'REPLACE', 'COMMAND'):
            a = self.buffer.to_py(cursor.anchor())
            p = self.buffer.to_py(cursor.position())
            if engine.mode == 'NORMAL':
                engine.set_mode('VISUAL')
            engine.anchor = a
            engine.pos = max(0, p - 1) if p > a else p
            self.sync_from_engine()
        elif engine.mode.startswith('VISUAL') and not (e.modifiers() & Qt.ShiftModifier):
            engine.set_mode('NORMAL')
            engine.pos = engine.clamp(self.buffer.to_py(cursor.position()))
            self.sync_from_engine()
        elif engine.mode == 'NORMAL':
            engine.pos = engine.clamp(self.buffer.to_py(cursor.position()))
            self.sync_from_engine()

    def contextMenuEvent(self, e):
        menu = self.createStandardContextMenu()
        actions = self.context_actions() if callable(getattr(self, 'context_actions', None)) else []
        if actions:
            menu.addSeparator()
            model = menu.addMenu('Model')
            for action in actions:
                if action is None:
                    model.addSeparator()
                else:
                    model.addAction(action)
        menu.exec(e.globalPos())

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.modeChanged.emit(self.mode)
