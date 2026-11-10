"""Several KiCad boards in one design: ids, poses kept in the SCAD source, and the Boards panel."""
from dataclasses import dataclass
import math
from pathlib import Path
import re
import numpy as np
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem, QDoubleSpinBox,
                               QCheckBox, QToolButton, QHeaderView, QDialog, QFormLayout, QSpinBox, QDialogButtonBox,
                               QComboBox, QStackedWidget)
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
    show_traces: bool = False
    color: tuple = None        # board (solder mask) color override, RGB 0..1; None = KiCad stackup color
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
        for v, f, c in self.model.reference_parts(self.color, False):
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


def _link_line(source, ident):
    named = re.compile(r'^([ \t]*//\s*scab\s*:\s*kicad[ \t]+' + re.escape(ident) + r'\s*=\s*\S+)([^\n]*)', re.M)
    m = named.search(source or '')
    if m is None and ident == 'kicad':
        m = re.search(r'^([ \t]*//\s*scab\s*:\s*kicad\s*=\s*\S+)([^\n]*)', source or '', re.M)
    return m


def link_option(source, ident, key):
    """Option written after the path on a board's link line, e.g. `// scab: kicad display = d.kicad_pcb color=#2050c0`."""
    m = _link_line(source, ident)
    if m is None:
        return None
    o = re.search(r'(?:^|\s)' + re.escape(key) + r'=(\S+)', m.group(2))
    return o.group(1) if o else None


def set_link_option(source, ident, key, value):
    """Set (value) or remove (None) an option on a board's link line."""
    m = _link_line(source, ident)
    if m is None:
        return source
    rest = re.sub(r'\s*(?<!\S)' + re.escape(key) + r'=\S+', '', m.group(2))
    if value is not None:
        rest = f' {key}={value}' + rest
    return source[:m.start(2)] + rest + source[m.end(2):]


def parse_rgb(text):
    m = re.fullmatch(r'#?([0-9a-fA-F]{6})', (text or '').strip())
    return tuple(int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4)) if m else None

# ------------------------------------------------------------------- panel
COLUMNS = ['Board', 'Show', 'Parts', 'Traces', 'Color', 'X', 'Y', 'Z', 'Rot X', 'Rot Y', 'Rot Z', 'File']


class BoardsPanel(QWidget):
    addRequested = Signal()
    removeRequested = Signal(str)
    reloadRequested = Signal(str)
    flipRequested = Signal(str)
    resetRequested = Signal(str)
    focusRequested = Signal(str)
    poseEdited = Signal(str, object, object)
    visibilityChanged = Signal(str, bool, bool)
    tracesChanged = Signal(str, bool)
    colorRequested = Signal(str)
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
        for column, width in enumerate((90, 44, 44, 50, 50, 78, 78, 78, 70, 70, 70, 260)):
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
        traces = QCheckBox()
        traces.setChecked(entry.show_traces)
        traces.setToolTip('Copper layers: tracks, vias and zone fills on every layer (loaded on demand; see View → Load PCB copper layers)')
        traces.toggled.connect(lambda on, i=entry.ident: self.tracesChanged.emit(i, on))
        self.tree.setItemWidget(item, 3, traces)
        color = QToolButton()
        color.setToolTip('Board color (only the board, not the components). Default: the solder mask color from the KiCad stackup')
        color.clicked.connect(lambda *_, i=entry.ident: self.colorRequested.emit(i))
        self.tree.setItemWidget(item, 4, color)
        spins = []
        for k in range(6):
            spin = QDoubleSpinBox()
            spin.setRange(-10000, 10000) if k < 3 else spin.setRange(-360, 360)
            spin.setDecimals(2)
            spin.setSingleStep(1. if k < 3 else 15.)
            spin.setSuffix(' mm' if k < 3 else '°')
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(lambda *_, i=entry.ident: self.pose_changed(i))
            self.tree.setItemWidget(item, 5 + k, spin)
            spins.append(spin)
        self.rows[entry.ident] = {'item': item, 'show': show, 'parts': parts, 'traces': traces, 'color': color, 'spins': spins}

    def update_row(self, entry):
        row = self.rows.get(entry.ident)
        if not row:
            return
        self.updating = True
        try:
            values = list(entry.pose[0]) + list(entry.pose[1])
            rgb = entry.color or (entry.model.colors()[0][:3] if entry.model is not None else None)
            if rgb is not None:
                from PySide6.QtGui import QColor
                hexcolor = QColor.fromRgbF(*map(float, rgb[:3])).name()
                row['color'].setStyleSheet(f'QToolButton {{ background: {hexcolor}; border: 1px solid #5d6066; min-width: 28px; }}')
                row['color'].setText('' if entry.color is None else '•')
            for key, value in (('show', entry.visible), ('parts', entry.show_parts), ('traces', entry.show_traces)):
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


def grid_poses(pose, columns, rows, levels, spacing):
    """Poses of a columns × rows × levels grid starting at the board (the board itself is left out)."""
    (position, angles) = pose
    out = []
    for k in range(levels):
        for j in range(rows):
            for i in range(columns):
                if i == j == k == 0:
                    continue
                offset = (i * spacing[0], j * spacing[1], k * spacing[2])
                out.append(([position[a] + offset[a] for a in range(3)], list(angles)))
    return out


def circular_poses(pose, count, center, total_angle=360., rotate_boards=True):
    """`count` boards in total on a circle about a vertical axis through `center` (x, y); copies only."""
    if count < 2:
        return []
    full = abs(abs(total_angle) - 360.) < 1e-9
    step = total_angle / (count if full else count - 1)
    matrix = pose_matrix(*pose)
    out = []
    for k in range(1, count):
        a = math.radians(step * k)
        turn = np.eye(4)
        turn[:2, :2] = [[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]
        c = np.array([center[0], center[1], 0.])
        turn[:3, 3] = c - turn[:3, :3] @ c
        moved = turn @ matrix
        if not rotate_boards:
            moved[:3, :3] = matrix[:3, :3]
        position, angles = pose_from_matrix(moved)
        out.append(([round(float(v), 4) for v in position], [round(float(v), 4) for v in angles]))
    return out


class BoardPatternDialog(QDialog):
    """Copies of a board in a line, a grid or a circle, with a live preview."""
    previewChanged = Signal(object)

    def __init__(self, parent, ident, pose, size, center=None):
        super().__init__(parent)
        self.setWindowTitle(f'Pattern board · {ident}')
        self.pose = pose
        center = list(center) if center is not None else list(pose[0][:2])
        layout = QVBoxLayout(self)
        top = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItems(['Linear', 'Grid', 'Circular'])
        top.addRow('Pattern', self.kind)
        layout.addLayout(top)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages)

        def spin(value, low=-10000, high=10000, suffix=' mm', step=1.):
            box = QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(2)
            box.setSuffix(suffix)
            box.setSingleStep(step)
            box.setValue(round(value, 2))
            return box

        def count(value, low=1):
            box = QSpinBox()
            box.setRange(low, 100)
            box.setValue(value)
            return box
        # linear
        page = QWidget()
        form = QFormLayout(page)
        self.count = count(2)
        form.addRow('Copies', self.count)
        self.step = [spin(v) for v in (size[0] + 5., 0., 0.)]
        for axis, box in zip('XYZ', self.step):
            form.addRow(f'Step {axis}', box)
        self.rotate = spin(0., -360, 360, '°', 15.)
        form.addRow('Rotate each copy (Z)', self.rotate)
        self.pages.addWidget(page)
        # grid
        page = QWidget()
        form = QFormLayout(page)
        self.columns, self.rows, self.levels = count(2), count(2), count(1)
        form.addRow('Columns (X)', self.columns)
        form.addRow('Rows (Y)', self.rows)
        form.addRow('Layers (Z)', self.levels)
        self.spacing = [spin(v) for v in (size[0] + 5., size[1] + 5., max(10., size[2] + 8.))]
        for axis, box in zip('XYZ', self.spacing):
            form.addRow(f'Spacing {axis}', box)
        self.pages.addWidget(page)
        # circular
        page = QWidget()
        form = QFormLayout(page)
        self.total = count(4, 2)
        form.addRow('Boards in total', self.total)
        radius = max(size[0], size[1]) * 0.75 + 5.
        self.center = [spin(center[0] - radius), spin(center[1])]
        form.addRow('Center X', self.center[0])
        form.addRow('Center Y', self.center[1])
        self.angle = spin(360., -360, 360, '°', 15.)
        form.addRow('Total angle', self.angle)
        self.follow = QCheckBox('Rotate the boards with the pattern')
        self.follow.setChecked(True)
        form.addRow(self.follow)
        self.pages.addWidget(page)
        note = QLabel('Every copy is a separate board instance (same KiCad file) that you can move on its own.')
        note.setWordWrap(True)
        note.setObjectName('secondary')
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.kind.currentIndexChanged.connect(self.pages.setCurrentIndex)
        self.kind.currentIndexChanged.connect(lambda *_: self.previewChanged.emit(self.poses()))
        boxes = [self.count, self.rotate, self.columns, self.rows, self.levels, self.total, self.angle] + self.step + self.spacing + self.center
        for widget in boxes:
            widget.valueChanged.connect(lambda *_: self.previewChanged.emit(self.poses()))
        self.follow.toggled.connect(lambda *_: self.previewChanged.emit(self.poses()))

    def poses(self):
        kind = self.kind.currentText()
        if kind == 'Grid':
            return grid_poses(self.pose, self.columns.value(), self.rows.value(), self.levels.value(),
                              [s.value() for s in self.spacing])
        if kind == 'Circular':
            return circular_poses(self.pose, self.total.value(), [c.value() for c in self.center], self.angle.value(),
                                  self.follow.isChecked())
        return copy_poses(self.pose, self.count.value(), [s.value() for s in self.step], self.rotate.value())
