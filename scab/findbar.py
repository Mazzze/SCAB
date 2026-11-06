"""Find / replace bar for the source editor (Ctrl+F, Ctrl+H, F3)."""
import re
from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLineEdit, QToolButton, QLabel, QPushButton
from .icons import icon


def compile_pattern(text, case=False, word=False, regex=False):
    """Python regex for the find field, or None when empty / invalid."""
    if not text:
        return None
    body = text if regex else re.escape(text)
    if word:
        body = r'(?<![\w$])(?:' + body + r')(?![\w$])'
    try:
        return re.compile(body, 0 if case else re.IGNORECASE)
    except re.error:
        return None


def expand(match, template, regex):
    """Replacement text; with regex on, $1 / \\1 / \\g<name> refer to groups."""
    if not regex:
        return template
    template = re.sub(r'\$(\d+)', r'\\g<\1>', template)
    try:
        return match.expand(template)
    except (re.error, IndexError):
        return template


class FindBar(QWidget):
    closed = Signal()

    def __init__(self, editor_getter):
        super().__init__()
        self.editor_getter = editor_getter
        self.setObjectName('findbar')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(2)
        self.find = QLineEdit()
        self.find.setPlaceholderText('Find')
        self.find.setClearButtonEnabled(True)
        top.addWidget(self.find, 1)
        self.case = self.toggle('Aa', 'Match case')
        self.word = self.toggle('W', 'Whole word')
        self.regex = self.toggle('.*', 'Regular expression ($1 or \\1 in the replacement)')
        for button in (self.case, self.word, self.regex):
            top.addWidget(button)
        self.count = QLabel('')
        self.count.setObjectName('secondary')
        self.count.setMinimumWidth(80)
        top.addWidget(self.count)
        for name, tip, fn in (('undo', 'Previous match (Shift+Enter, Shift+F3)', self.previous),
                              ('redo', 'Next match (Enter, F3)', self.next)):
            button = QToolButton()
            button.setIcon(icon(name, 14))
            button.setToolTip(tip)
            button.clicked.connect(fn)
            top.addWidget(button)
        close = QToolButton()
        close.setText('×')
        close.setToolTip('Close (Esc)')
        close.clicked.connect(self.close_bar)
        top.addWidget(close)
        layout.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(2)
        self.replace = QLineEdit()
        self.replace.setPlaceholderText('Replace with')
        bottom.addWidget(self.replace, 1)
        one = QPushButton('Replace')
        one.setToolTip('Replace this match and find the next (Enter in the replace field)')
        one.clicked.connect(self.replace_one)
        every = QPushButton('Replace all')
        every.setToolTip('Replace every match (Ctrl+Alt+Enter) · one undo step')
        every.clicked.connect(self.replace_all)
        bottom.addWidget(one)
        bottom.addWidget(every)
        self.replace_row = QWidget()
        self.replace_row.setLayout(bottom)
        layout.addWidget(self.replace_row)
        self.find.textChanged.connect(self.refresh)
        for button in (self.case, self.word, self.regex):
            button.toggled.connect(self.refresh)
        self.find.installEventFilter(self)
        self.replace.installEventFilter(self)
        self.hide()

    def toggle(self, text, tip):
        button = QToolButton()
        button.setText(text)
        button.setCheckable(True)
        button.setToolTip(tip)
        button.setFixedWidth(28)
        return button

    # ---------------------------------------------------------------- state
    @property
    def editor(self):
        return self.editor_getter()

    def pattern(self):
        return compile_pattern(self.find.text(), self.case.isChecked(), self.word.isChecked(), self.regex.isChecked())

    def matches(self):
        pattern = self.pattern()
        if pattern is None:
            return []
        text = self.editor.buffer.text()
        return [m for m in pattern.finditer(text) if m.end() > m.start()]

    def open(self, replace=False):
        editor = self.editor
        cursor = editor.textCursor()
        seed = cursor.selectedText() if cursor.hasSelection() else ''
        if not seed or '\u2029' in seed:
            word = QTextCursor(cursor)
            word.select(QTextCursor.WordUnderCursor)
            seed = word.selectedText() if not self.find.text() else ''
        if seed and '\u2029' not in seed:
            self.find.setText(seed)
        self.replace_row.setVisible(replace)
        self.show()
        self.find.setFocus()
        self.find.selectAll()
        self.refresh()

    def close_bar(self):
        self.hide()
        editor = self.editor
        editor.find_regex = None
        editor.refresh_decorations()
        editor.setFocus()
        self.closed.emit()

    def refresh(self):
        editor = self.editor
        pattern = self.pattern()
        bad = bool(self.find.text()) and pattern is None
        self.find.setStyleSheet('QLineEdit { border-color: #e5606d; }' if bad else '')
        editor.find_regex = pattern if self.isVisible() else None
        editor.refresh_decorations()
        self.update_count()

    def update_count(self, current=None):
        found = self.matches()
        if not self.find.text():
            self.count.setText('')
        elif not found:
            self.count.setText('no results')
        else:
            position = self.editor.buffer.to_py(self.editor.textCursor().selectionStart())
            index = current if current is not None else next((i for i, m in enumerate(found) if m.start() == position), None)
            self.count.setText(f'{index + 1} of {len(found)}' if index is not None else f'{len(found)} matches')

    # ------------------------------------------------------------ navigation
    def select(self, match):
        editor = self.editor
        cursor = editor.textCursor()
        cursor.setPosition(editor.buffer.to_qt(match.start()))
        cursor.setPosition(editor.buffer.to_qt(match.end()), QTextCursor.KeepAnchor)
        editor.setTextCursor(cursor)
        editor.centerCursor()
        if editor.vim:
            engine = editor.vim_engine
            engine.refresh()
            engine.pos = match.start()
        editor.refresh_decorations()

    def next(self, backwards=False):
        found = self.matches()
        if not found:
            self.update_count()
            return None
        editor = self.editor
        cursor = editor.textCursor()
        start = editor.buffer.to_py(cursor.selectionStart())
        end = editor.buffer.to_py(cursor.selectionEnd())
        if backwards:
            candidates = [i for i, m in enumerate(found) if m.start() < start]
            index = candidates[-1] if candidates else len(found) - 1
        else:
            candidates = [i for i, m in enumerate(found) if m.start() >= end and not (m.start() == start and m.end() == end)]
            index = candidates[0] if candidates else 0
        self.select(found[index])
        self.update_count(index)
        return found[index]

    def previous(self):
        return self.next(backwards=True)

    def replace_one(self):
        editor = self.editor
        cursor = editor.textCursor()
        start = editor.buffer.to_py(cursor.selectionStart())
        end = editor.buffer.to_py(cursor.selectionEnd())
        match = next((m for m in self.matches() if m.start() == start and m.end() == end), None)
        if match is None:
            self.next()
            return
        new = expand(match, self.replace.text(), self.regex.isChecked())
        edit = editor.textCursor()
        edit.beginEditBlock()
        edit.insertText(new)
        edit.endEditBlock()
        editor.setTextCursor(edit)
        self.next()

    def replace_all(self):
        pattern = self.pattern()
        if pattern is None:
            return 0
        editor = self.editor
        text = editor.buffer.text()
        template = self.replace.text()
        regex = self.regex.isChecked()
        new, count = pattern.subn(lambda m: expand(m, template, regex) if m.end() > m.start() else m.group(0), text)
        if not count or new == text:
            self.count.setText('no results')
            return 0
        prefix = 0
        while prefix < min(len(text), len(new)) and text[prefix] == new[prefix]:
            prefix += 1
        suffix = 0
        while suffix < min(len(text), len(new)) - prefix and text[-1 - suffix] == new[-1 - suffix]:
            suffix += 1
        cursor = QTextCursor(editor.document())
        cursor.beginEditBlock()
        cursor.setPosition(editor.buffer.to_qt(prefix))
        cursor.setPosition(editor.buffer.to_qt(len(text) - suffix), QTextCursor.KeepAnchor)
        cursor.insertText(new[prefix:len(new) - suffix])
        cursor.endEditBlock()
        self.count.setText(f'replaced {count}')
        editor.refresh_decorations()
        return count

    # ------------------------------------------------------------------ keys
    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            key, mods = event.key(), event.modifiers()
            if key == Qt.Key_Escape:
                self.close_bar()
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if obj is self.replace and mods & Qt.ControlModifier and mods & Qt.AltModifier:
                    self.replace_all()
                elif obj is self.replace and not mods & Qt.ShiftModifier:
                    self.replace_one()
                else:
                    self.next(backwards=bool(mods & Qt.ShiftModifier))
                return True
            if key == Qt.Key_F3:
                self.next(backwards=bool(mods & Qt.ShiftModifier))
                return True
            if key == Qt.Key_H and mods & Qt.ControlModifier:
                self.replace_row.show()
                self.replace.setFocus()
                return True
        return super().eventFilter(obj, event)
