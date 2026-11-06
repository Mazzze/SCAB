"""Clearance tab: KiCad parts versus the SCAD model."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QDoubleSpinBox, QPushButton,
                               QTreeWidget, QTreeWidgetItem)
from .icons import icon

STATUS = {'collision': ('error', '#e8a0a6', 'Collision'), 'tight': ('warning', '#dfc48f', 'Tight'), 'ok': (None, '#9a9da3', 'OK')}


def mm(value):
    return '—' if value == float('inf') else f'{value:.2f}'


class ClearancePanel(QWidget):
    checkRequested = Signal()
    partSelected = Signal(str, int)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(6, 4, 6, 4)
        self.board_label = QLabel('Parts of every board against the model and against the other boards')
        self.board_label.setObjectName('secondary')
        row.addWidget(self.board_label, 1)
        row.addWidget(QLabel('Min. clearance'))
        self.minimum = QDoubleSpinBox()
        self.minimum.setRange(0, 20)
        self.minimum.setDecimals(2)
        self.minimum.setSingleStep(.25)
        self.minimum.setValue(1.)
        self.minimum.setSuffix(' mm')
        self.minimum.valueChanged.connect(lambda *_: self.checkRequested.emit())
        row.addWidget(self.minimum)
        check = QPushButton('Check')
        check.clicked.connect(self.checkRequested.emit)
        row.addWidget(check)
        layout.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Board', 'Part', 'Status', 'Height', 'Headroom', 'Min. gap', '3D model'])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSortingEnabled(True)
        for column, width in enumerate((80, 70, 90, 70, 80, 80, 70)):
            self.tree.setColumnWidth(column, width)
        self.tree.itemClicked.connect(lambda item, _: self.partSelected.emit(*item.data(0, Qt.UserRole)))
        self.tree.setToolTip('Height: top of the part above the board. Headroom: free space straight above it '
                             '(below it for bottom-side parts). Min. gap: closest model surface in any direction.')
        layout.addWidget(self.tree, 1)
        self.results = []

    def set_results(self, results, minimum):
        """results: [(board id, clearance.Result)]."""
        self.results = results
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        order = {'collision': 0, 'tight': 1, 'ok': 2}
        for ident, r in sorted(results, key=lambda item: (order[item[1].status(minimum)], item[0], item[1].ref)):
            status = r.status(minimum)
            name, color, text = STATUS[status]
            item = QTreeWidgetItem([ident, r.ref, text, mm(r.height), '—' if r.collides else mm(r.headroom),
                                    '0.00' if r.collides else mm(r.gap), 'yes' if r.has_model else 'box'])
            item.setData(0, Qt.UserRole, (ident, r.index))
            if name:
                item.setIcon(2, icon(name, 14))
            for column in range(7):
                item.setForeground(column, QBrush(QColor(color if column == 2 else '#cfd1d4')))
            for column in (3, 4, 5):
                item.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
            self.tree.addTopLevelItem(item)
        self.tree.setSortingEnabled(True)

    def summary(self, minimum):
        counts = {'collision': 0, 'tight': 0}
        for _, r in self.results:
            status = r.status(minimum)
            if status in counts:
                counts[status] += 1
        return counts
