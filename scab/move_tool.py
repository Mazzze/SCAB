"""Move / Rotate on parts and groups: what a click selects, the "move what?" chooser, Ctrl+click grouping.

A click with Move or Rotate selects the *part* under the mouse. A small bar in
the 3D view lists what else the click could move — the group (translate / block)
around the part, a shared group variable, or the whole object — and Ctrl+click
adds parts. Moving several parts makes them a group: neighbours in the text get
one `translate(<name>) { ... }`, parts spread over the file get `translate(<name>)`
each with the same variable, `<name> = [x, y, z];`.
"""
from dataclasses import dataclass, field
import re
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QToolButton, QLabel, QButtonGroup, QInputDialog, QMenu
from . import grouping
from .parameters import rotation


@dataclass
class Target:
    kind: str                  # 'node' | 'variable'
    path: list = None          # model nodes from the top-level object down (kind 'node')
    name: str = ''             # group variable (kind 'variable')
    label: str = ''
    context: object = None     # world matrix where the node sits
    boxes: list = field(default_factory=list)
    root_start: int = -1

    @property
    def node(self):
        return self.path[-1] if self.path else None

    @property
    def whole(self):
        return self.kind == 'node' and len(self.path) == 1


@dataclass
class Pick:
    point: object              # world point that was clicked
    root_start: int
    level: tuple               # which option was chosen: ('part',) / ('whole',) / ('variable', name) / ('group', depth)


class ChooserBar(QWidget):
    """Floating bar in the 3D view: what a Move / Rotate drag acts on."""

    def __init__(self, viewer, window):
        super().__init__(viewer)
        self.window = window
        self.setObjectName('moveChooser')
        self.setStyleSheet('#moveChooser { background: rgba(30, 32, 36, 230); border: 1px solid #3c3f45; }'
                           'QToolButton { padding: 2px 6px; } QToolButton:checked { background: #2f4f86; color: white; }')
        self.layout_ = QHBoxLayout(self)
        self.layout_.setContentsMargins(6, 3, 6, 3)
        self.layout_.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.hide()

    def clear(self):
        while self.layout_.count():
            item = self.layout_.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().setParent(None)
        for button in self.group.buttons():
            self.group.removeButton(button)

    def show_options(self, mode, options, chosen, extra_text='', actions=()):
        self.hide()
        self.clear()
        title = QLabel(f'{mode}:')
        title.setObjectName('secondary')
        self.layout_.addWidget(title)
        for index, (label, _, _) in enumerate(options):
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setChecked(index == chosen)
            button.setToolTip('Click to move this instead · Tab cycles in the 3D view')
            button.clicked.connect(lambda _=False, i=index: self.window.choose_move_option(i))
            self.group.addButton(button, index)
            self.layout_.addWidget(button)
        if extra_text:
            text = QLabel(extra_text)
            text.setObjectName('secondary')
            self.layout_.addWidget(text)
        for label, callback, tip in actions:
            button = QToolButton()
            button.setText(label)
            button.setToolTip(tip)
            button.clicked.connect(callback)
            self.layout_.addWidget(button)
        self.layout_.activate()
        self.adjustSize()
        self.move(8, 8)
        self.show()
        self.raise_()


class MoveMixin:
    def setup_move_tool(self):
        self.move_targets = []
        self.move_options = []
        self.move_choice = 0
        self.move_source = None
        self.move_picks = []
        self.move_chooser = ChooserBar(self.viewer, self)
        self.viewer.pickedAdd.connect(self.ctrl_pick)

    # ------------------------------------------------------------- helpers
    def move_mode(self):
        mode = self.object_tool.currentText() if hasattr(self, 'object_tool') else ''
        return mode if mode in ('Move', 'Rotate') else None

    def trace_spans(self):
        """Per leaf: the main-file statement spans along its instantiation chain (cached per trace)."""
        result = self.trace_result
        cache = result.__dict__.get('_scab_spans')
        if cache is None:
            cache = []
            for leaf in result.leaves:
                spans = []
                if leaf.node is not None and getattr(leaf.node, 'file', 1) == 0:
                    spans.append((leaf.node.start, leaf.node.end, leaf.matrix))
                frame = leaf.frame
                while frame is not None:
                    node = frame.node
                    if node is not None and getattr(node, 'file', 1) == 0:
                        spans.append((node.start, node.end, frame.matrix))
                    frame = frame.parent
                cache.append(spans)
            result.__dict__['_scab_spans'] = cache
        return cache

    def node_boxes_and_context(self, node):
        """(world boxes of everything under the node, the world matrix where the node sits)."""
        if self.trace_result is None:
            return [], None
        low = high = None
        context = None
        for leaf, spans in zip(self.trace_result.leaves, self.trace_spans()):
            hit = next(((a, b, m) for a, b, m in spans if node.start <= a and b <= node.end), None)
            if hit is None:
                continue
            if context is None:
                exact = next((m for a, b, m in spans if node.start <= a <= node.start + 2 and b <= node.end + 1 and b >= node.end - 1), None)
                if exact is not None:
                    context = exact
            box = leaf.world_box()
            if box is None:
                continue
            low = box[0] if low is None else np.minimum(low, box[0])
            high = box[1] if high is None else np.maximum(high, box[1])
        return ([(low, high)] if low is not None else []), context

    def make_target(self, option):
        label, kind, payload = option
        if kind == 'variable':
            boxes, context = [], None
            for use in grouping.variable_uses(self.doc, payload):
                b, c = self.node_boxes_and_context(use)
                boxes += b
                context = context if context is not None else c
            return Target('variable', name=payload, label=label, boxes=boxes, context=context)
        path = grouping.node_path(self.doc, payload.start, payload.end)
        boxes, context = self.node_boxes_and_context(payload)
        if not boxes:
            chunk = next((s for s in self.viewer.scene if s[0] == path[0].start), None)
            if chunk is not None:
                boxes = [(chunk[2].min(axis=0), chunk[2].max(axis=0))]
        return Target('node', path=path, label=label, context=context, boxes=boxes, root_start=path[0].start)

    @staticmethod
    def option_level(options, index):
        label, kind, payload = options[index]
        if kind == 'variable':
            return ('variable', payload)
        if index == 0:
            return ('part',)
        if label.startswith('Whole object'):
            return ('whole',)
        return ('group', index)

    @staticmethod
    def level_index(options, level):
        for index, (label, kind, payload) in enumerate(options):
            if level[0] == 'variable' and kind == 'variable' and payload == level[1]:
                return index
            if level[0] == 'whole' and label.startswith('Whole object'):
                return index
        if level[0] == 'group':
            groups = [i for i, o in enumerate(options) if o[0].startswith('Group')]
            if groups:
                return groups[0]
        return 0

    # ------------------------------------------------------------- picking
    def statement_for_point(self, start, point):
        """The main-file statement path under a picked world point (needs the trace)."""
        chunk = next((c for c in self.viewer.scene if c[0] == start), None)
        if chunk is None or self.trace_result is None:
            return None
        leaf = self.trace_result.pick(point, (chunk[0], chunk[1]))
        if leaf is None:
            root = next((r for r in self.doc.roots if r.start == start), None)
            return [root] if root is not None else None
        stmt = leaf.node if getattr(leaf.node, 'file', 1) == 0 else None
        frame = leaf.frame
        while stmt is None and frame is not None:
            if frame.node is not None and getattr(frame.node, 'file', 1) == 0:
                stmt = frame.node
            frame = frame.parent
        if stmt is None:
            root = next((r for r in self.doc.roots if r.start == start), None)
            return [root] if root is not None else None
        return grouping.node_path(self.doc, stmt.start, stmt.end) or None

    def pick_move_target(self, start, point, additive=False, level=None):
        """A click on the model with Move / Rotate: select the part (Ctrl: add it)."""
        if not self.move_mode() or point is None or start < 0:
            return
        if not self.view_matches_editor():
            self.info('Compile the current source before moving parts')
            return
        if self.trace_result is None or self.trace_key is None or self.trace_key[0] != self.compiled_source:
            self.info('analysing the design…')
            self.request_trace(lambda: self.pick_move_target(start, point, additive, level))
            return
        if not self.parse():
            return
        path = self.statement_for_point(start, point)
        if not path:
            return
        options = grouping.move_options(self.doc, path)
        if not options:
            return
        if level is None:
            if additive and self.move_picks:
                level = self.move_picks[0].level if self.move_picks[0].level[0] in ('part', 'whole') else ('part',)
            else:
                level = getattr(self, 'move_level', ('part',))
        index = self.level_index(options, level)
        pick = Pick(np.asarray(point, dtype=float), start, self.option_level(options, index))
        target = self.make_target(options[index])
        if additive and self.move_targets:
            same = [t for t in self.move_targets if t.kind == target.kind and
                    (t.name == target.name if t.kind == 'variable' else (t.node.start, t.node.end) == (target.node.start, target.node.end))]
            if same:                                                 # Ctrl+click again removes it
                i = self.move_targets.index(same[0])
                self.move_targets.pop(i)
                self.move_picks.pop(i)
            else:
                self.move_targets.append(target)
                self.move_picks.append(pick)
            self.move_options = []
        else:
            self.move_targets = [target]
            self.move_picks = [pick]
            self.move_options = options
            self.move_choice = index
        self.move_source = self.doc.source
        self.show_move_targets()

    def ctrl_pick(self, start, point):
        """Ctrl+click in the 3D view: add to the selection. From Inspect / Dimensions it switches to Move,
        keeping the object that was clicked before as the first one."""
        if self.move_mode():
            self.pick_move_target(start, point, additive=True)
            return
        if not self.view_matches_editor():
            self.info('Compile the current source before selecting parts')
            return
        first = getattr(self, 'last_pick', None)
        first = first if first and first[0] >= 0 and first[1] is not None else None
        self.object_tool.setCurrentText('Move')
        self.info('Move · Ctrl+click adds objects · drag the arrows to move them together (they become a group) · Ctrl+G names the group')

        def run():
            if first is not None and not self.move_targets:
                self.pick_move_target(first[0], first[1], level=('whole',))
            self.pick_move_target(start, point, additive=bool(self.move_targets), level=('whole',))
        if self.trace_result is None or self.trace_key is None or self.trace_key[0] != self.compiled_source:
            self.request_trace(run)
        else:
            run()

    def choose_move_option(self, index):
        if not self.move_options or not 0 <= index < len(self.move_options):
            return
        self.move_choice = index
        self.move_targets = [self.make_target(self.move_options[index])]
        level = self.option_level(self.move_options, index)
        self.move_level = level if level[0] in ('part', 'whole') else ('part',)
        if self.move_picks:
            self.move_picks = [Pick(self.move_picks[0].point, self.move_picks[0].root_start, level)]
        self.show_move_targets()

    def cycle_move_option(self):
        if self.move_options:
            self.choose_move_option((self.move_choice + 1) % len(self.move_options))

    def targets_from_tree(self, nodes):
        """Tree selection with Move / Rotate: the selected nodes (Ctrl+click in the tree for several)."""
        self.move_targets, self.move_picks, self.move_options = [], [], []
        if not nodes or not self.doc:
            self.show_move_targets()
            return
        if self.trace_result is None or self.trace_key is None or self.trace_key[0] != self.compiled_source:
            if self.compiled_source is not None and self.view_matches_editor():
                self.request_trace(lambda n=list(nodes): self.targets_from_tree(n) if self.move_mode() else None)
        for node in nodes:
            path = grouping.node_path(self.doc, node.start, node.end)
            if not path:
                continue
            label = ('Whole object · ' if len(path) == 1 else 'Part · ') + f'{grouping.describe(self.doc, node)} · L{grouping.line_of(self.doc, node.start)}'
            self.move_targets.append(self.make_target((label, 'node', node)))
        if len(self.move_targets) == 1:
            options = grouping.move_options(self.doc, self.move_targets[0].path)
            self.move_options = options
            self.move_choice = 0
        self.move_source = self.doc.source
        self.show_move_targets()

    def clear_move_targets(self):
        self.move_targets, self.move_picks, self.move_options = [], [], []
        self.move_chooser.hide()
        self.viewer.clear_pick_box()

    def show_move_targets(self):
        mode = self.move_mode()
        if not mode or not self.move_targets:
            self.move_chooser.hide()
            return
        whole = [t.root_start for t in self.move_targets if t.kind == 'node' and t.whole]
        boxes = [b for t in self.move_targets for b in t.boxes]
        self.viewer.set_handles([])
        self.viewer.set_gizmo_targets(mode, whole, boxes)
        self.viewer.highlight(whole[0] if len(whole) == 1 and len(self.move_targets) == 1 else -1)
        if len(self.move_targets) == 1 and self.move_targets[0].kind == 'node' and self.view_matches_editor():
            node = self.move_targets[0].node
            self.editor.select_source(node.start, node.end)
        actions = []
        if len(self.move_targets) > 1:
            shared = self.shared_group_variable()
            text = f'{len(self.move_targets)} parts · ' + (f'group {shared}' if shared else
                                                            f'moving groups them as {self.suggested_group_name()}')
            if not shared:
                actions.append(('Group…', self.group_selection, 'Group them now and name the group (Ctrl+G)'))
            self.move_chooser.show_options(mode, [], -1, text + ' · Ctrl+click adds/removes', actions)
            return
        target = self.move_targets[0]
        if target.kind == 'variable':
            actions.append(('Ungroup', lambda: self.ungroup(target.name), f'Remove {target.name}; members keep their place'))
            actions.append(('Rename…', lambda: self.rename_group(target.name), 'Rename the group variable'))
        options = self.move_options or [(target.label, target.kind, target.node if target.kind == 'node' else target.name)]
        self.move_chooser.show_options(mode, options, self.move_choice if self.move_options else 0,
                                       'Ctrl+click: add parts', actions)

    # -------------------------------------------------------------- groups
    def target_nodes(self):
        nodes = []
        for t in self.move_targets:
            if t.kind == 'node':
                nodes.append(t.node)
            else:
                nodes += grouping.variable_uses(self.doc, t.name)
        return nodes

    def shared_group_variable(self):
        """A group variable all selected targets already belong to (and nothing else does)."""
        names = set()
        for t in self.move_targets:
            if t.kind == 'variable':
                names.add(t.name)
                continue
            found = None
            for node in reversed(t.path):
                name = grouping.translate_variable(self.doc, node)
                if name and len(grouping.variable_uses(self.doc, name)) >= 1 and grouping.variable_value(self.doc, name):
                    found = name
                    break
            if found is None:
                return None
            names.add(found)
        return names.pop() if len(names) == 1 else None

    def suggested_group_name(self):
        return grouping.suggest_group_name(self.doc, self.target_nodes())

    def group_selection(self):
        """Ctrl+G: group the selected parts under a named position variable."""
        if not self.parse():
            return
        nodes = self.target_nodes()
        if not nodes and self.tree.selectedItems():
            nodes = [i.data(0, Qt.UserRole) for i in self.tree.selectedItems()]
        if len(nodes) < 1:
            self.info('Select parts first: Move tool, click a part, Ctrl+click more (or Ctrl+click in the object list)')
            return
        suggestion = grouping.suggest_group_name(self.doc, nodes)
        name, ok = QInputDialog.getText(self, 'Group parts', f'Name of the group position variable ({len(nodes)} parts):', text=suggestion)
        name = name.strip()
        if not ok or not name:
            return
        if not re.fullmatch(r'[A-Za-z_]\w*', name) or re.search(r'\b' + re.escape(name) + r'\b', self.doc.source):
            self.info(f'"{name}" is not a free variable name')
            return
        try:
            source = grouping.group_source(self.doc, nodes, name)
        except ValueError as error:
            self.info(str(error))
            return
        self.replace_source(source)
        self.info(f'Grouped {len(nodes)} parts as {name} · move the group by dragging any of them (choose "Group {name}")')
        self.move_level = ('variable', name)
        self.after_move_edit(np.zeros(3), None, ('variable', name))

    def ungroup(self, name):
        if not self.parse():
            return
        self.replace_source(grouping.ungroup_source(self.doc, name))
        self.info(f'Ungrouped {name}: its offset is now a plain translate on each part')
        self.move_level = ('part',)
        self.after_move_edit(np.zeros(3), None, ('part',))

    def rename_group(self, name):
        new, ok = QInputDialog.getText(self, 'Rename group', 'New name:', text=name)
        new = new.strip()
        if not ok or not new or new == name:
            return
        if not re.fullmatch(r'[A-Za-z_]\w*', new) or re.search(r'\b' + re.escape(new) + r'\b', self.editor.toPlainText()):
            self.info(f'"{new}" is not a free variable name')
            return
        self.replace_source(re.sub(r'\b' + re.escape(name) + r'\b', new, self.editor.toPlainText()))
        self.move_level = ('variable', new)
        self.after_move_edit(np.zeros(3), None, ('variable', new))

    # ------------------------------------------------------------- applying
    def apply_target_transform(self, kind, axis, amount, pivot):
        """Write a finished drag on the selected parts / groups into the source. True when handled."""
        if not self.move_targets:
            return False
        if self.move_source is not None and self.doc is not None and self.doc.source != self.move_source:
            self.clear_move_targets()
            return False
        targets = self.move_targets
        world = np.zeros(3)
        if kind == 'translate':
            world[axis] = amount
        if len(targets) == 1 and targets[0].kind == 'node' and targets[0].whole:
            return False                     # whole object: the classic pose edit
        try:
            if len(targets) == 1 and targets[0].kind == 'variable':
                if kind != 'translate':
                    self.info('A group variable moves; to rotate, choose a part or the whole object')
                    return True
                local = self.local_delta(targets[0].context, world)
                source = grouping.move_variable_source(self.doc, targets[0].name, local)
                level = ('variable', targets[0].name)
            elif len(targets) == 1:
                t = targets[0]
                source = grouping.nudge_source(self.doc, t.path, kind, axis, amount, pivot, t.context)
                level = self.move_picks[0].level if self.move_picks else ('part',)
            else:
                shared = self.shared_group_variable()
                if kind == 'translate' and shared:
                    context = next((t.context for t in targets if t.context is not None), None)
                    source = grouping.move_variable_source(self.doc, shared, self.local_delta(context, world))
                    level = ('variable', shared)
                elif kind == 'translate':
                    name = self.suggested_group_name()
                    context = next((t.context for t in targets if t.context is not None), None)
                    source = grouping.group_source(self.doc, self.target_nodes(), name, self.local_delta(context, world))
                    level = ('variable', name)
                    self.info(f'Grouped {len(targets)} parts as {name} — rename it with Rename… in the bar, or ungroup')
                else:
                    source = self.doc.source
                    for t in sorted(targets, key=lambda t: t.node.start if t.kind == 'node' else 0, reverse=True):
                        if t.kind != 'node':
                            continue
                        path = grouping.node_path(type(self.doc)(source), t.node.start, t.node.end)
                        source = grouping.nudge_source(type(self.doc)(source), path, kind, axis, amount, pivot, t.context)
                    level = ('part',)
        except ValueError as error:
            self.info(str(error))
            return True
        self.replace_source(source)
        self.after_move_edit(world, (kind, axis, amount, pivot), level)
        return True

    @staticmethod
    def local_delta(context, world):
        if context is None:
            return world
        try:
            return np.linalg.inv(np.asarray(context, dtype=float)[:3, :3]) @ world
        except np.linalg.LinAlgError:
            return world

    def after_move_edit(self, world, transform, level):
        """After an edit: compile, then select the same parts again where they now are."""
        picks = []
        for pick in self.move_picks:
            point = pick.point
            if transform is not None:
                kind, axis, amount, pivot = transform
                if kind == 'translate':
                    point = point + world
                else:
                    angles = np.zeros(3)
                    angles[axis] = amount
                    p = np.asarray(pivot, dtype=float)
                    point = rotation(angles) @ (point - p) + p
            picks.append(Pick(point, pick.root_start, level if len(self.move_picks) == 1 or level[0] == 'variable' else pick.level))
        self.pending_reselect = picks
        self.move_targets, self.move_options = [], []
        self.move_chooser.hide()
        self.compile()

    def reselect_after_compile(self):
        """Called when a compile finished: restore the part selection at the moved points."""
        picks = getattr(self, 'pending_reselect', None)
        self.pending_reselect = None
        if not picks or not self.move_mode():
            return
        self.move_picks = []
        def run():
            self.move_targets = []
            for k, pick in enumerate(picks):
                hit = self.viewer_chunk_at(pick.point)
                if hit is None:
                    continue
                self.pick_move_target(hit, pick.point, additive=k > 0 and pick.level[0] != 'variable', level=pick.level)
                if pick.level[0] == 'variable':
                    break
        self.request_trace(run)

    def viewer_chunk_at(self, point):
        """Start of the top-level object whose mesh bounds contain a world point."""
        best = None
        for chunk in self.viewer.scene:
            low, high = chunk[2].min(axis=0), chunk[2].max(axis=0)
            if np.all(point >= low - 1e-3) and np.all(point <= high + 1e-3):
                size = float(np.prod(np.maximum(high - low, 1e-6)))
                if best is None or size < best[0]:
                    best = (size, chunk[0])
        return best[1] if best else None
