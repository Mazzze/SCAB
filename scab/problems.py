"""Problems panel: every error/warning with its location; click to jump there."""
from pathlib import Path
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem
from .diagnostics import count_text
from .icons import icon

COLORS = {'error': '#e8a0a6', 'warning': '#dfc48f', 'info': '#a9c3ea'}


class ProblemsPanel(QTreeWidget):
    """Lists problems; emits goto(path, line, column) when one is activated."""
    goto = Signal(object, int, int)

    def __init__(self):
        super().__init__()
        self.setHeaderLabels(['Problem', 'Where'])
        self.setColumnWidth(0, 430)
        self.setRootIsDecorated(True)
        self.setAlternatingRowColors(False)
        self.setUniformRowHeights(True)
        self.itemActivated.connect(self.activate)
        self.itemClicked.connect(self.activate)
        self.problems = []
        self.setToolTip('Errors and warnings from the live syntax check and OpenSCAD. Click to jump to the line.')

    def set_problems(self, problems):
        self.problems = list(problems)
        self.clear()
        for problem in self.problems:
            item = QTreeWidgetItem([problem.message, problem.location])
            item.setIcon(0, icon(problem.severity if problem.severity in ('error', 'warning', 'info') else 'info', 16))
            item.setForeground(0, QBrush(QColor(COLORS.get(problem.severity, '#dce5ef'))))
            item.setData(0, Qt.UserRole, (problem.file, problem.line, problem.column))
            tip = problem.message + (f'\n\n{problem.hint}' if problem.hint else '')
            item.setToolTip(0, tip)
            item.setToolTip(1, str(problem.file) if problem.file else '')
            if problem.hint:
                hint = QTreeWidgetItem([f'Hint: {problem.hint}', ''])
                hint.setForeground(0, QBrush(QColor('#8c8f95')))
                hint.setData(0, Qt.UserRole, (problem.file, problem.line, problem.column))
                hint.setToolTip(0, problem.hint)
                item.addChild(hint)
            for file, line, text in problem.trace:
                where = f'{Path(file).name}:{line + 1}' if file is not None else ''
                child = QTreeWidgetItem([text, where])
                child.setForeground(0, QBrush(QColor('#8c8f95')))
                child.setData(0, Qt.UserRole, (file, line, None))
                item.addChild(child)
            self.addTopLevelItem(item)
            if problem.severity == 'error':
                item.setExpanded(True)

    def summary(self):
        return count_text(self.problems)

    def activate(self, item, column=0):
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        file, line, col = data
        if line is None:
            return
        self.goto.emit(file, int(line), int(col or 0))

    def first_error(self):
        return next((p for p in self.problems if p.severity == 'error' and p.line is not None), None)
