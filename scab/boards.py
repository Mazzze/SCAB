"""Several KiCad boards in one design: ids, poses kept in the SCAD source, and the Boards panel."""
from dataclasses import dataclass
import math
from pathlib import Path
import re
import numpy as np
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem, QDoubleSpinBox,
                               QCheckBox, QToolButton, QHeaderView, QDialog, QFormLayout, QSpinBox, QDialogButtonBox)
from .icons import icon

IDENTITY_POSE = ([0., 0., 0.], [0., 0., 0.])


@dataclass
class BoardEntry:
    ident: str
    path: object
    model: object = None
    pose: tuple = IDENTITY_POSE
    visible: bool = True
    show_parts: bool = True
    loading: bool = False
    error: str = ''

    @property
    def matrix(self):
        return pose_matrix(*self.pose)

    def world_parts(self):
        """[(vertices_world, faces, colors)] in viewer order (body, pads, silk, parts)."""
        if self.model is None:
            return []
        m = self.matrix
        out = []
        for v, f, c in self.model.reference_parts():
            out.append(((np.c_[v, np.ones(len(v))] @ m.T)[:, :3].astype(np.float32), f, c))
        return out


def rotation(angles):
    rx, ry, rz = (math.radians(a) for a in angles)
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    X = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Y = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Z = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Z @ Y @ X


def pose_matrix(position, angles):
    """translate(position) rotate(angles) — the same order OpenSCAD uses."""
    m = np.eye(4)
    m[:3, :3] = rotation(angles)
    m[:3, 3] = position
    return m


def pose_from_matrix(matrix):
    r = matrix[:3, :3]
    y = math.asin(float(np.clip(-r[2, 0], -1, 1)))
    if abs(math.cos(y)) > 1e-8:
        x = math.atan2(r[2, 1], r[2, 2])
        z = math.atan2(r[1, 0], r[0, 0])
    else:
        x = math.atan2(-r[1, 2], r[1, 1])
        z = 0.
    clean = lambda v: 0. if abs(v) < 1e-9 else round(float(v), 6)
    return [clean(v) for v in matrix[:3, 3]], [clean(math.degrees(a)) for a in (x, y, z)]


def fmt(v):
    text = f'{float(v):.4f}'.rstrip('0').rstrip('.')
    return '0' if text in ('-0', '') else text


def pose_text(position, angles):
    return f'[[{", ".join(fmt(v) for v in position)}], [{", ".join(fmt(v) for v in angles)}]]'


def read_pose(source, ident):
    """The pose literal `<id>_pose = [[x, y, z], [rx, ry, rz]];` (simple arithmetic allowed)."""
    m = re.search(r'^[ \t]*' + re.escape(ident) + r'_pose\s*=\s*([^;]*);', source or '', re.M)
    if not m:
        return None
    try:
        from .parameters import evaluate, Scope
        value = evaluate(m.group(1), Scope(source, {}))
        position, angles = value
        position = [float(v) for v in position][:3]
        angles = [float(v) for v in angles][:3]
        if len(position) == 3 and len(angles) == 3:
            return position, angles
    except (ValueError, TypeError, SyntaxError, ZeroDivisionError):
        return None
    return None


def write_pose(source, ident, position, angles):
    """Replace (or insert after the board links) the pose assignment of a board."""
    text = pose_text(position, angles)
    pattern = re.compile(r'^([ \t]*' + re.escape(ident) + r'_pose\s*=\s*)([^;]*)(;)', re.M)
    m = pattern.search(source)
    if m:
        return source[:m.start(2)] + text + source[m.end(2):]
    line = f'{ident}_pose = {text};   // where the {ident} board sits: [x, y, z], [rx, ry, rz] (SCAB updates this)\n'
    links = list(re.finditer(r'^[ \t]*//\s*scab\s*:\s*kicad[^\n]*\n', source, re.M))
    if links:
        at = links[-1].end()
        return source[:at] + line + source[at:]
    return line + source


def add_link(source, ident, relative_path, include_name):
    """Insert `// scab: kicad <id> = path`, the pose and the include line at the top of the file."""
    link = f'// scab: kicad {ident} = {relative_path}\n'
    include = f'include <kicad/{include_name}>\n'
    links = list(re.finditer(r'^[ \t]*//\s*scab\s*:\s*kicad[^\n]*\n', source, re.M))
    at = links[-1].end() if links else 0
    source = source[:at] + link + source[at:]
    source = write_pose(source, ident, *IDENTITY_POSE)
    if include.strip() not in source:
        includes = list(re.finditer(r'^[ \t]*include\s*<kicad/[^>]*>[^\n]*\n', source, re.M))
        if includes:
            at = includes[-1].end()
        else:
            poses = list(re.finditer(r'^[ \t]*\w+_pose\s*=[^\n]*\n', source, re.M))
            at = poses[-1].end() if poses else 0
        source = source[:at] + include + source[at:]
    return source


def remove_link(source, ident, include_name=None):
    if include_name:
        source = re.sub(r'^[ \t]*include\s*<kicad/' + re.escape(include_name) + r'>[^\n]*\n', '', source, flags=re.M)
    source = re.sub(r'^[ \t]*//\s*scab\s*:\s*kicad[ \t]+' + re.escape(ident) + r'\s*=[^\n]*\n', '', source, flags=re.M)
    if ident == 'kicad':
        source = re.sub(r'^[ \t]*//\s*scab\s*:\s*kicad\s*=[^\n]*\n', '', source, count=1, flags=re.M)
    return re.sub(r'^[ \t]*' + re.escape(ident) + r'_pose\s*=[^;]*;[^\n]*\n', '', source, flags=re.M)



def rename_link(source, old, new, relative_path):
    """Rename a board id: its link, pose, include line and every `<old>_name` use in the file."""
    from .kicad_import import include_names
    stem = Path(relative_path).stem
    old_include, _ = include_names(old, stem)
    new_include, _ = include_names(new, stem)
    ident = r'(?:[ \t]+' + re.escape(old) + r')?' if old == 'kicad' else r'[ \t]+' + re.escape(old)
    pattern = re.compile(r'^([ \t]*//\s*scab\s*:\s*kicad)' + ident + r'(\s*=\s*)(\S+)', re.M)
    def link(m):
        return f'{m.group(1)} {new}{m.group(2)}{m.group(3)}' if Path(m.group(3)).stem == stem else m.group(0)
    source = pattern.sub(link, source, count=1)
    source = re.sub(r'(include\s*<kicad/)' + re.escape(old_include) + '>', lambda m: m.group(1) + new_include + '>', source)
    source = re.sub(r'(_pose\s*=[^;]*;[ \t]*//[^\n]*\bthe )' + re.escape(old) + r'( board\b)', lambda m: m.group(1) + new + m.group(2), source)
    use = re.compile(r'(?<![\w.])' + re.escape(old) + r'_(?=[A-Za-z_])')
    return ''.join(line if re.match(r'[ \t]*//\s*scab\s*:', line) else use.sub(new + '_', line)
                   for line in source.splitlines(keepends=True))

# ------------------------------------------------------------------- panel
COLUMNS = ['Board', 'Show', 'Parts', 'X', 'Y', 'Z', 'Rot X', 'Rot Y', 'Rot Z', 'File']


class BoardsPanel(QWidget):
    addRequested = Signal()
    removeRequested = Signal(str)
    reloadRequested = Signal(str)
    flipRequested = Signal(str)
    resetRequested = Signal(str)
    focusRequested = Signal(str)
    poseEdited = Signal(str, object, object)
    visibilityChanged = Signal(str, bool, bool)
    boardSelected = Signal(str)
    kicadRequested = Signal(str)
    menuRequested = Signal(str, object)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        bar.setSpacing(1)
        for name, tip, signal in (('kicad_import', 'Add a KiCad board…', self.addRequested),
                                  ('reload', 'Reload the selected board', self.reloadRequested),
                                  ('rotate', 'Flip the selected board (rotate 180° about X)', self.flipRequested),
                                  ('undo', 'Reset position and rotation', self.resetRequested),
                                  ('fit', 'Focus the view on the selected board', self.focusRequested)):
            button = QToolButton()
            button.setIcon(icon(name))
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, s=signal: s.emit() if s is self.addRequested else
                                   (s.emit(self.current()) if self.current() else None))
            bar.addWidget(button)
        kicad = QToolButton()
        kicad.setIcon(icon('kicad'))
        kicad.setToolTip('Open the selected board in KiCad (saving there updates it here)')
        kicad.clicked.connect(lambda: self.kicadRequested.emit(self.current()) if self.current() else None)
        bar.addWidget(kicad)
        remove = QToolButton()
        remove.setText('Remove')
        remove.setToolTip('Unlink the selected board from this file')
        remove.clicked.connect(lambda: self.removeRequested.emit(self.current()) if self.current() else None)
        bar.addWidget(remove)
        self.hint = QLabel('Move boards with the X/Y/Z/rotation fields, or select one and use Move (G) / Rotate (R) in the 3D view.')
        self.hint.setObjectName('secondary')
        bar.addWidget(self.hint, 1)
        layout.addLayout(bar)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        for column, width in enumerate((90, 44, 44, 78, 78, 78, 70, 70, 70, 260)):
            self.tree.setColumnWidth(column, width)
        self.tree.currentItemChanged.connect(lambda item, _: self.boardSelected.emit(item.data(0, Qt.UserRole)) if item else None)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        layout.addWidget(self.tree, 1)
        self.rows = {}
        self.timers = {}
        self.updating = False

    def context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        self.menuRequested.emit(item.data(0, Qt.UserRole), self.tree.viewport().mapToGlobal(pos))

    def current(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def select(self, ident):
        row = self.rows.get(ident)
        if row:
            self.tree.setCurrentItem(row['item'])

    def set_boards(self, entries):
        """Rebuild rows when the set of boards changes; otherwise only update values."""
        idents = [e.ident for e in entries]
        if idents != list(self.rows):
            current = self.current()
            self.tree.clear()
            self.rows = {}
            for entry in entries:
                self.add_row(entry)
            if current in self.rows:
                self.select(current)
            elif entries:
                self.select(entries[0].ident)
        for entry in entries:
            self.update_row(entry)

    def add_row(self, entry):
        item = QTreeWidgetItem([entry.ident] + [''] * (len(COLUMNS) - 1))
        item.setData(0, Qt.UserRole, entry.ident)
        self.tree.addTopLevelItem(item)
        show, parts = QCheckBox(), QCheckBox()
        show.setChecked(entry.visible)
        parts.setChecked(entry.show_parts)
        for box in (show, parts):
            box.toggled.connect(lambda *_, i=entry.ident: self.visibilityChanged.emit(i, self.rows[i]['show'].isChecked(),
                                                                                       self.rows[i]['parts'].isChecked()))
        self.tree.setItemWidget(item, 1, show)
        self.tree.setItemWidget(item, 2, parts)
        spins = []
        for k in range(6):
            spin = QDoubleSpinBox()
            spin.setRange(-10000, 10000) if k < 3 else spin.setRange(-360, 360)
            spin.setDecimals(2)
            spin.setSingleStep(1. if k < 3 else 15.)
            spin.setSuffix(' mm' if k < 3 else '°')
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(lambda *_, i=entry.ident: self.pose_changed(i))
            self.tree.setItemWidget(item, 3 + k, spin)
            spins.append(spin)
        self.rows[entry.ident] = {'item': item, 'show': show, 'parts': parts, 'spins': spins}

    def update_row(self, entry):
        row = self.rows.get(entry.ident)
        if not row:
            return
        self.updating = True
        try:
            values = list(entry.pose[0]) + list(entry.pose[1])
            for key, value in (('show', entry.visible), ('parts', entry.show_parts)):
                if row[key].isChecked() != value:
                    row[key].blockSignals(True)
                    row[key].setChecked(value)
                    row[key].blockSignals(False)
            for spin, value in zip(row['spins'], values):
                if abs(spin.value() - value) > 1e-6:
                    spin.setValue(value)
            status = entry.error or (('meshing board… ' if entry.model is not None else 'loading… ') + str(entry.path) if entry.loading else
                                     (f'{len(entry.model.parts)} parts · ' if entry.model else '') + str(entry.path))
            row['item'].setText(len(COLUMNS) - 1, status)
            row['item'].setToolTip(len(COLUMNS) - 1, str(entry.path))
        finally:
            self.updating = False

    def pose_changed(self, ident):
        if self.updating:
            return
        values = [s.value() for s in self.rows[ident]['spins']]
        self.poseEdited.emit(ident, values[:3], values[3:])


def copy_poses(pose, count, step, rotate_step):
    """Poses of `count` copies: each one `step` further and `rotate_step` degrees more about Z."""
    (position, angles) = pose
    out = []
    for k in range(1, count + 1):
        out.append(([position[i] + k * step[i] for i in range(3)],
                    [angles[0], angles[1], (angles[2] + k * rotate_step + 180) % 360 - 180]))
    return out


def add_copies(source, ident, relative_path, poses, taken):
    """Add board instances (same KiCad file, new ids) with the given poses; returns (source, new ids)."""
    from .kicad_import import board_id, include_names
    taken = set(taken)
    new = []
    base = ident.rstrip('0123456789') or ident
    for pose in poses:
        copy = board_id(base, taken | {ident})
        taken.add(copy)
        include, _ = include_names(copy, Path(relative_path).stem)
        source = add_link(source, copy, relative_path, include)
        source = write_pose(source, copy, *pose)
        new.append(copy)
    return source, new


class BoardPatternDialog(QDialog):
    """Copies of a board: count, step and rotation, with a live preview."""
    previewChanged = Signal(object)

    def __init__(self, parent, ident, pose, size):
        super().__init__(parent)
        self.setWindowTitle(f'Pattern board · {ident}')
        self.pose = pose
        form = QFormLayout(self)
        self.count = QSpinBox()
        self.count.setRange(1, 100)
        self.count.setValue(2)
        form.addRow('Copies', self.count)
        self.step = []
        for axis, value in zip('XYZ', (size[0] + 5., 0., 0.)):
            spin = QDoubleSpinBox()
            spin.setRange(-10000, 10000)
            spin.setDecimals(2)
            spin.setSuffix(' mm')
            spin.setValue(round(value, 2))
            form.addRow(f'Step {axis}', spin)
            self.step.append(spin)
        self.rotate = QDoubleSpinBox()
        self.rotate.setRange(-360, 360)
        self.rotate.setSuffix('°')
        form.addRow('Rotate each copy (Z)', self.rotate)
        note = QLabel('Every copy is a separate board instance (same KiCad file) that you can move on its own.')
        note.setWordWrap(True)
        note.setObjectName('secondary')
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        for widget in [self.count, self.rotate] + self.step:
            widget.valueChanged.connect(lambda *_: self.previewChanged.emit(self.poses()))

    def poses(self):
        return copy_poses(self.pose, self.count.value(), [s.value() for s in self.step], self.rotate.value())
