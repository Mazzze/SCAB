"""Inspector: click the model, see and edit the parameters that made that part."""
import re
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush, QFont
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem, QLineEdit,
                               QPushButton, QComboBox, QCheckBox, QColorDialog, QToolButton)
from .trace import fmt, Unknown
from .inspector import color_literal

NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')


def to_qcolor(value):
    if isinstance(value, str):
        c = QColor(value)
        return c if c.isValid() else None
    if isinstance(value, list) and len(value) in (3, 4) and all(isinstance(x, (int, float)) for x in value):
        c = QColor()
        c.setRgbF(*(max(0., min(1., float(x))) for x in value[:3]), float(value[3]) if len(value) == 4 else 1.)
        return c
    return None


class ValueEdit(QLineEdit):
    """Expression editor; mouse wheel / Up / Down nudge the number under the cursor."""
    committed = Signal(str)

    def __init__(self, text):
        super().__init__(text)
        self.original = text
        self.returnPressed.connect(self.commit)
        self.setToolTip('Enter: apply · Wheel / ↑↓: change number (Shift ×0.1, Ctrl ×10) · Esc: revert')

    def commit(self):
        if self.text() != self.original:
            self.committed.emit(self.text())

    def nudge(self, direction, modifiers):
        text = self.text()
        position = self.cursorPosition()
        match = None
        for m in NUMBER.finditer(text):
            if m.start() <= position <= m.end():
                match = m
                break
        if match is None:
            match = next(NUMBER.finditer(text), None)
        if match is None:
            return
        raw = match.group(0)
        decimals = len(raw.split('.')[1]) if '.' in raw and 'e' not in raw.lower() else 0
        step = 10 ** -decimals if decimals else 1.
        if modifiers & Qt.ShiftModifier:
            step /= 10
            decimals += 1
        if modifiers & Qt.ControlModifier:
            step *= 10
        value = float(raw) + direction * step
        new = f'{value:.{decimals}f}' if decimals else f'{value:g}'
        self.setText(text[:match.start()] + new + text[match.end():])
        self.setCursorPosition(match.start() + len(new))
        self.commit()
        self.original = self.text()

    def wheelEvent(self, e):
        if self.hasFocus():
            self.nudge(1 if e.angleDelta().y() > 0 else -1, e.modifiers())
            e.accept()
        else:
            super().wheelEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Up, Qt.Key_Down):
            self.nudge(1 if e.key() == Qt.Key_Up else -1, e.modifiers())
            return
        if e.key() == Qt.Key_Escape:
            self.setText(self.original)
            return
        super().keyPressEvent(e)


class InspectorPanel(QWidget):
    """Shows the call chain and parameters of a picked primitive."""
    editRequested = Signal(object, str)        # EditTarget, new source text
    gotoRequested = Signal(int, int)           # file index, character position
    colorPreview = Signal(object)              # rgba tuple or None

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        top = QHBoxLayout()
        self.title = QLabel('INSPECTOR · click any part of the model')
        self.title.setWordWrap(True)
        self.title.setStyleSheet('color:#84cfc7;font-weight:bold;padding:4px;')
        top.addWidget(self.title, 1)
        self.library_toggle = QCheckBox('Library frames')
        self.library_toggle.setToolTip('Also show module calls inside libraries such as BOSL2')
        self.library_toggle.toggled.connect(lambda *_: self.render())
        top.addWidget(self.library_toggle)
        layout.addLayout(top)
        self.path_label = QLabel('')
        self.path_label.setWordWrap(True)
        self.path_label.setStyleSheet('color:#a9cde7;padding:0 4px 4px 4px;')
        layout.addWidget(self.path_label)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['PARAMETER', 'VALUE', 'APPLY TO', 'FROM'])
        self.tree.setColumnWidth(0, 130)
        self.tree.setColumnWidth(1, 160)
        self.tree.setColumnWidth(2, 260)
        self.tree.setUniformRowHeights(False)
        self.tree.itemDoubleClicked.connect(self.goto_item)
        layout.addWidget(self.tree, 1)
        hint = QLabel('Edits change the .scad source where the value is written — inside modules and other files too. '
                      '"definition" changes every use; "only this call" overrides just this instance. Double-click a row to see its code.')
        hint.setWordWrap(True)
        hint.setStyleSheet('color:#7f93a8;padding:3px;')
        layout.addWidget(hint)
        self.groups = []
        self.files = []

    def clear(self, message='Click any part of the model to inspect its parameters'):
        self.groups = []
        self.tree.clear()
        self.title.setText('INSPECTOR · ' + message)
        self.path_label.setText('')

    def show_leaf(self, leaf, groups, files, color_frame=None):
        self.groups, self.files = groups, files
        path, text = files[leaf.node.file]
        line = text.count('\n', 0, leaf.node.start) + 1
        kind = 'cutter (removes material)' if leaf.cutter else 'solid'
        self.title.setText(f'INSPECTOR · {leaf.node.name}() {kind} · {path.name if path else "untitled"}:{line}')
        chain = [g for g in groups if g.kind == 'module' and not g.library]
        names = ' ← '.join(g.title.split('(')[0] for g in chain)
        self.path_label.setText(('inside ' + names) if names else 'top-level primitive')
        self.render()

    def render(self):
        self.tree.clear()
        show_library = self.library_toggle.isChecked()
        bold = QFont()
        bold.setBold(True)
        hidden = 0
        for group in self.groups:
            if group.library and not show_library:
                hidden += 1
                continue
            item = QTreeWidgetItem([group.title, '', '', ''])
            item.setFirstColumnSpanned(True)
            item.setFont(0, bold)
            item.setForeground(0, QBrush(QColor('#8fa3b8' if group.library else '#ffd479' if group.kind == 'variables' else '#83d9ff')))
            item.setData(0, Qt.UserRole, ('group', group.file, group.position))
            self.tree.addTopLevelItem(item)
            for row in group.rows:
                self.add_row(item, row)
            item.setExpanded(not group.library)
        if hidden:
            note = QTreeWidgetItem([f'{hidden} library frame(s) hidden — tick "Library frames" to show', '', '', ''])
            note.setFirstColumnSpanned(True)
            note.setForeground(0, QBrush(QColor('#6f8296')))
            self.tree.addTopLevelItem(note)

    def add_row(self, parent, row):
        value_text = fmt(row.value) if not isinstance(row.value, Unknown) else row.expression
        item = QTreeWidgetItem(['  ' + row.name, '', '', row.origin + (f' · {row.note}' if row.note else '')])
        item.setToolTip(0, f'{row.name} = {row.expression}\nvalue: {value_text}')
        item.setToolTip(3, row.origin)
        item.setData(0, Qt.UserRole, ('row', row.frame_file, row.frame_pos))
        parent.addChild(item)
        if not row.editable or not row.targets:
            item.setText(1, value_text)
            item.setForeground(1, QBrush(QColor('#9fb3c8')))
            return
        combo = QComboBox()
        for target in row.targets:
            combo.addItem(target.describe(self.files), target)
        combo.setToolTip('Where the edit is written')
        self.tree.setItemWidget(item, 2, combo)
        editor = self.make_editor(row, combo)
        self.tree.setItemWidget(item, 1, editor)

    def current_text(self, target):
        text = self.files[target.file][1]
        return text[target.start:target.end] if target.kind != 'insert' else ''

    def make_editor(self, row, combo):
        target = lambda: combo.currentData()
        kind = row.kind
        if kind == 'color':
            button = QPushButton(fmt(row.value) if row.value is not None else 'color…')
            color = to_qcolor(row.value)
            if color is not None:
                light = color.lightnessF() > .55
                button.setStyleSheet(f'background:{color.name()};color:{"#08101a" if light else "#ffffff"};')

            def pick():
                initial = to_qcolor(row.value) or QColor('#60bfba')
                dialog = QColorDialog(initial, self)
                dialog.setOption(QColorDialog.ShowAlphaChannel, True)
                dialog.setWindowTitle(f'{row.name} · live preview, OK writes the source')
                dialog.currentColorChanged.connect(lambda c: self.colorPreview.emit(c.getRgbF()))
                if dialog.exec():
                    c = dialog.currentColor()
                    t = target()
                    original = self.current_text(t) if t.kind != 'insert' else ('[' if isinstance(row.value, list) else '"')
                    self.editRequested.emit(t, color_literal(c.getRgbF(), original))
                else:
                    self.colorPreview.emit(None)
            button.clicked.connect(pick)
            return button
        if kind == 'bool':
            box = QCheckBox('true' if row.value else 'false')
            box.setChecked(bool(row.value))
            box.toggled.connect(lambda checked: self.editRequested.emit(target(), 'true' if checked else 'false'))
            return box
        t = row.targets[0]
        initial = self.current_text(t) if t.kind != 'insert' else (fmt(row.value) if row.value is not None else '')
        edit = ValueEdit(initial)
        edit.setPlaceholderText(row.expression)

        def retarget(*_):
            current = target()
            edit.original = self.current_text(current) if current.kind != 'insert' else edit.text()
            if current.kind != 'insert':
                edit.setText(edit.original)
        combo.currentIndexChanged.connect(retarget)
        edit.committed.connect(lambda text: self.editRequested.emit(target(), text))
        return edit

    def goto_item(self, item, column):
        data = item.data(0, Qt.UserRole)
        if data and data[1] is not None and data[1] >= 0:
            self.gotoRequested.emit(data[1], data[2] or 0)
