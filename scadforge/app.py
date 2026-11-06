import os
import sys
import re
import threading
from pathlib import Path
import numpy as np
from PySide6.QtCore import Qt, QTimer, QSettings, Signal
from PySide6.QtGui import QAction, QKeySequence, QTextCursor, QColor, QSurfaceFormat, QBrush, QPalette
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QPlainTextEdit,
    QFileDialog, QMessageBox, QInputDialog, QColorDialog, QCheckBox, QComboBox,
    QDoubleSpinBox, QSpinBox, QSlider, QDialog, QFormLayout, QDialogButtonBox,
    QGroupBox, QAbstractItemView, QProgressBar, QTabWidget)
from .model import Document, SourceError
from .editor import Editor, Diagnostic
from .diagnostics import parse_log, source_problems, PathMapper, count_text
from .problems import ProblemsPanel
from .inspector_panel import InspectorPanel
from .inspector import Explainer, apply_edit
from .trace import trace as trace_design
from . import __version__
from .compiler import CompileJob, find_openscad
from .viewer import Viewer
from .parameters import handles_for
from .plane import section_source, projection_source
from .project import ProjectMixin
from .panels import Collapsible
from .operations import transform_source,color_source
from .dialogs import PatternDialog,ThreadDialog
from .threads import install_library

STYLE = '''
QMainWindow, QDialog { background:#101720; color:#dce5ef; }
QWidget { color:#dce5ef; font-family:"DejaVu Sans"; font-size:12px; }
QMenuBar, QMenu, QToolBar { background:#17212d; spacing:8px; padding:5px; }
QMenu::item:selected { background:#325269; }
QToolBar { border-bottom:1px solid #2b3848; }
QLineEdit, QTextEdit, QListView, QTableView, QPlainTextEdit, QTreeWidget { background:#151d28; border:0; selection-background-color:#354e64; }
QPlainTextEdit { padding:5px; font-family:"DejaVu Sans Mono"; }
QTabWidget::pane { border:1px solid #2b3a4b; }
QTabBar::tab { background:#1a2736; color:#a8bbce; padding:6px 12px; border:1px solid #2b3a4b; }
QTabBar::tab:selected { background:#294052; color:#92e1d4; }
QHeaderView::section { background:#202d3d; color:#9eb4c8; border:0; padding:6px; }
QTreeWidget::item { padding:5px; }
QTreeWidget::item:selected { background:#304d61; }
QPushButton, QToolButton { background:#253548; border:1px solid #3a4c60; border-radius:5px; padding:6px 10px; }
QPushButton:hover, QToolButton:hover { background:#34536a; }
QPushButton:disabled { color:#647488; }
QComboBox QAbstractItemView { background:#202d3d; color:#dce5ef; selection-background-color:#325269; selection-color:#ffffff; }
QAbstractItemView { background:#151d28; color:#dce5ef; }
QToolTip { background:#202d3d; color:#dce5ef; border:1px solid #506076; }
QComboBox, QDoubleSpinBox, QSpinBox { background:#202d3d; border:1px solid #3b4c60; padding:4px; border-radius:4px; }
QGroupBox { border:1px solid #304052; border-radius:5px; margin-top:14px; padding-top:14px; }
QGroupBox::title { subcontrol-origin:margin; padding:0 6px; color:#84cfc7; }
QLabel#title { font-size:17px; font-weight:bold; color:#82d4ca; padding:7px; }
QLabel#hint { color:#a9cde7; background:#1b2939; padding:7px; }
QStatusBar { background:#1a2a38; }
QProgressBar { background:#202d3d; color:#dce5ef; border:1px solid #506076; text-align:center; border-radius:3px; }
QProgressBar::chunk { background:#287d75; }
QSplitter::handle { background:#2b3a4b; width:3px; }
QSlider::groove:horizontal { height:4px; background:#354a5e; }
QSlider::handle:horizontal { background:#7dd3c7; width:12px; margin:-5px 0; border-radius:5px; }
'''


def button(text, fn, layout):
    b = QPushButton(text)
    b.clicked.connect(fn)
    layout.addWidget(b)
    return b


def form_dialog(parent, title, fields):
    d = QDialog(parent)
    d.setWindowTitle(title)
    form = QFormLayout(d)
    widgets = {}
    for name, value in fields.items():
        if isinstance(value, tuple):
            w = QComboBox(); w.addItems(value)
        elif isinstance(value, int):
            w = QSpinBox(); w.setRange(1,120); w.setValue(value)
        else:
            from PySide6.QtWidgets import QLineEdit
            w = QLineEdit(str(value))
        form.addRow(name, w); widgets[name]=w
    controls = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    controls.accepted.connect(d.accept); controls.rejected.connect(d.reject)
    form.addRow(controls)
    if not d.exec(): return None
    return {k: (w.currentText() if isinstance(w,QComboBox) else w.value() if isinstance(w,QSpinBox) else w.text())
            for k,w in widgets.items()}


class Window(ProjectMixin,QMainWindow):
    traceReady=Signal(object,object)

    def __init__(self, path=None):
        super().__init__()
        self.settings = QSettings('SCADForge','SCADForge')
        self.executable = self.settings.value('openscad', '') or find_openscad()
        self.path = None
        self.doc = None
        self.node = None
        self.jobs = []
        self.generation = 0
        self.compiled_source = None
        self.frames = []
        self.frame_index = 0
        self.autoplay_generation = None
        self.loading = False
        self._search = ''
        self.trace_result = None
        self.trace_key = None
        self.trace_pending = None
        self.trace_cancel = threading.Event()
        self.last_pick = None
        self.repick_after_compile = False
        self.problem_sets = {}
        self.compile_context = {}
        self.vim_enabled = self.settings.value('vim', 'true') not in ('false', False, '0')
        self.traceReady.connect(self.trace_finished, Qt.QueuedConnection)
        self.setWindowTitle(f'SCAD Forge {__version__}')
        self.resize(1550,950)
        self.editor = Editor()
        self.viewer = Viewer()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['OBJECTS','COLOR'])
        self.tree.setColumnWidth(0,145);self.tree.setColumnWidth(1,90)
        self.tree.itemDoubleClicked.connect(self.tree_color)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self.tree_selected)
        self.viewer.picked.connect(self.viewport_selected)
        self.viewer.pickedPoint.connect(self.inspect_point)
        self.viewer.dimensionEdited.connect(self.apply_dimension)
        self.viewer.transformEdited.connect(self.apply_transform)
        self.viewer.dimensionPreview.connect(self.info)
        self.viewer.planeMoved.connect(self.move_plane)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setPlaceholderText('OpenSCAD compiler output · F5 to compile')
        self.problems_panel = ProblemsPanel()
        self.problems_panel.goto.connect(self.goto_problem)
        self.inspector = InspectorPanel()
        self.inspector.editRequested.connect(self.apply_inspector_edit)
        self.inspector.gotoRequested.connect(self.goto_traced)
        self.inspector.colorPreview.connect(self.preview_inspector_color)
        self.hint = QLabel('cube(size = [x, y, z], center = false) · Ctrl+Space: completion')
        self.hint.setObjectName('hint')
        self.hint.setWordWrap(True)
        self.mode_label = QLabel('NORMAL')
        self.mode_label.setMinimumWidth(160)
        self.wire_editor(self.editor)
        self.parse_timer = QTimer(self); self.parse_timer.setSingleShot(True)
        self.parse_timer.timeout.connect(self.parse)
        self.compile_timer = QTimer(self); self.compile_timer.setSingleShot(True)
        self.compile_timer.timeout.connect(self.compile)
        self.play_timer = QTimer(self); self.play_timer.timeout.connect(self.next_frame)
        self.build_ui()
        self.compile_bar=QProgressBar();self.compile_bar.setRange(0,100);self.compile_bar.setValue(0)
        self.compile_bar.setFixedWidth(230);self.compile_bar.setFormat('Ready')
        self.statusBar().addPermanentWidget(self.compile_bar)
        self.statusBar().addPermanentWidget(self.mode_label)
        self.statusBar().showMessage('Ready · millimetres · OpenSCAD source')
        example = Path(__file__).resolve().parent.parent/'examples'/'parametric_mount.scad'
        if path: self.load_file(Path(path))
        else:
            self.editor.setPlainText(example.read_text(encoding='utf-8'))
            self.editor.document().setModified(False)
            self.parse()
            self.compile_timer.start(150)

    def build_ui(self):
        toolbar=self.addToolBar('Model tools'); toolbar.setMovable(False)
        self.draft_mesh=QCheckBox('Draft mesh (24)');self.draft_mesh.setChecked(os.environ.get('SCADFORGE_DRAFT','0')=='1');self.draft_mesh.setToolTip('Lower preview tessellation only; saved SCAD and exports retain full source resolution. Requires rebuild.')
        self.draft_mesh.toggled.connect(lambda *_:self.compile_timer.start(100));toolbar.addWidget(self.draft_mesh)
        for text,fn,shortcut in [
            ('New',self.new_file,'Ctrl+N'),('Open',self.open_file,'Ctrl+O'),('Save',self.save,'Ctrl+S'),
            ('Undo',lambda:self.editor.undo(),'Ctrl+Z'),('Redo',lambda:self.editor.redo(),'Ctrl+Shift+Z'),
            ('Compile',self.compile,'F5'),('Export STL',self.export_stl,'F6')]:
            a=QAction(text,self); a.triggered.connect(fn); a.setShortcut(QKeySequence(shortcut)); toolbar.addAction(a)
        toolbar.addSeparator()
        self.auto=QCheckBox('Auto compile'); self.auto.setChecked(True); toolbar.addWidget(self.auto)
        self.vim_check=QCheckBox('Vim'); self.vim_check.setChecked(self.vim_enabled); self.vim_check.toggled.connect(self.set_vim); toolbar.addWidget(self.vim_check)
        self.editor.toggle_vim(self.vim_enabled)
        compiler=QAction('OpenSCAD path…',self); compiler.triggered.connect(self.configure_compiler)
        self.menuBar().addMenu('Settings').addAction(compiler)
        pcb_menu=self.menuBar().addMenu('PCB')
        for title,fn,shortcut in [('Insert PCB library + example board',self.insert_pcb_template,''),
                                  ('Export KiCad board (.kicad_pcb + placement CSV)…',self.export_kicad,'Ctrl+Shift+K'),
                                  ('Export section as KiCad Edge.Cuts DXF…',self.export_section,'')]:
            action=QAction(title,self);action.triggered.connect(fn)
            if shortcut:action.setShortcut(shortcut)
            pcb_menu.addAction(action)
        help_action=QAction('Controls & limits',self); help_action.triggered.connect(self.help)
        self.menuBar().addMenu('Help').addAction(help_action)
        left=QWidget(); ll=QVBoxLayout(left); ll.setContentsMargins(0,0,0,0)
        title=QLabel('SCAD / FORGE'); title.setObjectName('title'); ll.addWidget(title)
        self.file_label=QLabel('  Untitled.scad'); ll.addWidget(self.file_label)
        ll.addWidget(self.tree,2)
        tools=QGroupBox('Source operations'); tl=QVBoxLayout(tools)
        for text,fn in [('Color…',self.color),('Move / rotate…',self.transform),
                        ('Duplicate…',self.duplicate),('Linear / circular pattern…',self.pattern),
                        ('Boolean: difference / union…',self.boolean),('Bind variable to animation…',self.bind_animation),('Modeled metric thread…',self.add_thread)]:
            button(text,fn,tl)
        ll.addWidget(tools)
        expl=QLabel('Select in the view or source tree.\nEvery model operation edits the .scad text.'); expl.setWordWrap(True); ll.addWidget(expl)
        middle=QWidget(); ml=QVBoxLayout(middle); ml.setContentsMargins(0,0,0,0)
        self.setup_documents(ll,ml)
        ml.addWidget(self.hint)
        right=QWidget(); rl=QVBoxLayout(right); rl.setContentsMargins(0,0,0,0)
        vh=QHBoxLayout(); vh.addWidget(QLabel('  MODEL / GPU VIEW'),1)
        button('Fit',self.viewer.fit,vh)
        for text,elev,azim in [('Top',90,-90),('Front',0,-90),('Iso',28,-48)]:
            button(text,lambda _,e=elev,a=azim:self.viewer.setCameraPosition(elevation=e,azimuth=a),vh)
        rl.addLayout(vh)
        self.right_split=QSplitter(Qt.Vertical)
        viewer_box=QWidget(); viewer_layout=QVBoxLayout(viewer_box); viewer_layout.setContentsMargins(0,0,0,0)
        viewer_layout.addWidget(self.viewer,1)
        self.right_split.addWidget(viewer_box)
        self.bottom_tabs=QTabWidget()
        self.bottom_tabs.addTab(self.inspector,'Inspector')
        self.bottom_tabs.addTab(self.problems_panel,'Problems')
        self.bottom_tabs.addTab(self.console,'Output')
        rl.addWidget(self.right_split,1)
        nav=QLabel('  Click: inspect & select · Left drag: orbit · Shift + left drag: pan · Two-finger scroll: zoom')
        nav.setWordWrap(True); viewer_layout.addWidget(nav)
        tools_row=QHBoxLayout(); viewer_layout.addLayout(tools_row)
        tools_row.addWidget(QLabel('Object tool'))
        self.object_tool=QComboBox();self.object_tool.addItems(['Dimensions','Move','Rotate']);self.object_tool.currentTextChanged.connect(self.refresh_handles);tools_row.addWidget(self.object_tool)
        tools_row.addWidget(QLabel('Camera roll'))
        self.roll_control=QDoubleSpinBox();self.roll_control.setRange(-180,180);self.roll_control.setSuffix('°');self.roll_control.valueChanged.connect(self.viewer.set_roll);tools_row.addWidget(self.roll_control)
        self.viewer.rollChanged.connect(self.sync_roll)
        self.dimensions_check=QCheckBox('Show color-coded handles · XYZ: red / green / blue')
        self.dimensions_check.setChecked(True); self.dimensions_check.toggled.connect(self.refresh_handles); viewer_layout.addWidget(self.dimensions_check)
        section=QGroupBox('Construction plane / section / KiCad'); section_layout=QVBoxLayout(section)
        sl=QHBoxLayout(); section_layout.addLayout(sl)
        self.section_check=QCheckBox('Show plane'); self.section_check.toggled.connect(self.section_changed); sl.addWidget(self.section_check)
        self.clip_check=QCheckBox('Clip preview'); self.clip_check.setChecked(True); self.clip_check.toggled.connect(self.section_changed); sl.addWidget(self.clip_check)
        self.axis=QComboBox(); self.axis.addItems(['X','Y','Z']); self.axis.setCurrentText('Z'); self.axis.currentTextChanged.connect(self.section_changed); sl.addWidget(self.axis)
        self.section_slider=QSlider(Qt.Horizontal); self.section_slider.setRange(-1000,1000); self.section_slider.valueChanged.connect(self.section_slide); sl.addWidget(self.section_slider,1)
        self.section_value=QDoubleSpinBox(); self.section_value.setRange(-1e6,1e6); self.section_value.setDecimals(3); self.section_value.setSuffix(' mm'); self.section_value.setToolTip('Distance from world origin along the plane normal'); self.section_value.valueChanged.connect(self.section_number); sl.addWidget(self.section_value)
        self.flip=QCheckBox('Flip'); self.flip.toggled.connect(self.section_changed); sl.addWidget(self.flip)
        lower=QHBoxLayout(); section_layout.addLayout(lower)
        self.plane_angles=[]
        for label in ('Tilt X','Tilt Y','Tilt Z'):
            angle=QDoubleSpinBox(); angle.setRange(-180,180); angle.setSuffix('°'); angle.setPrefix(label+' '); angle.valueChanged.connect(self.section_changed)
            self.plane_angles.append(angle); lower.addWidget(angle)
        button('Add / center plane',self.add_plane,lower)
        button('Write cut to code',self.commit_section,lower)
        button('Export KiCad DXF…',self.export_section,lower)
        plane_help=QLabel('Blue square: move plane · Gold lines: section contour · Preview leaves .scad unchanged')
        plane_help.setWordWrap(True); section_layout.addWidget(plane_help)
        section.setTitle('')
        self.section_panel=Collapsible('Construction plane / KiCad',section,collapsed=True)
        viewer_layout.addWidget(self.section_panel)
        animation=QGroupBox('Animation · $t'); al=QHBoxLayout(animation)
        self.play_button=button('▶ Play',self.play,al)
        self.timeline=QSlider(Qt.Horizontal); self.timeline.setRange(0,1000); self.timeline.valueChanged.connect(self.scrub); al.addWidget(self.timeline,1)
        self.time_label=QLabel('0.000'); al.addWidget(self.time_label)
        self.frame_count=QSpinBox(); self.frame_count.setRange(2,120); self.frame_count.setValue(24); self.frame_count.setSuffix(' frames'); al.addWidget(self.frame_count)
        self.fps=QSpinBox(); self.fps.setRange(1,60); self.fps.setValue(12); self.fps.setSuffix(' fps'); al.addWidget(self.fps)
        button('Bake',self.bake,al); button('Cancel',self.cancel_jobs,al)
        self.animation_status=QLabel('Play prepares missing frames automatically · bind a variable to $t first')
        self.animation_status.setWordWrap(True)
        animation.setTitle('')
        animation_container=QWidget();animation_layout=QVBoxLayout(animation_container);animation_layout.setContentsMargins(0,0,0,0)
        animation_layout.addWidget(animation);animation_layout.addWidget(self.animation_status)
        self.animation_panel=Collapsible('Animation',animation_container,collapsed=True)
        viewer_layout.addWidget(self.animation_panel)
        self.right_split.addWidget(self.bottom_tabs)
        self.right_split.setSizes([520,420])
        self.fps.valueChanged.connect(lambda value:self.play_timer.setInterval(round(1000/value)))
        self.frame_count.valueChanged.connect(self.animation_count_changed)
        split=QSplitter(); split.addWidget(left); split.addWidget(middle); split.addWidget(right)
        split.setSizes([235,520,795]); split.setStretchFactor(0,0); split.setStretchFactor(1,1); split.setStretchFactor(2,2)
        self.setCentralWidget(split)

    def info(self,text): self.statusBar().showMessage(text)
    def error(self,text):
        self.console.setPlainText(text)
        self.info('Action failed · see compiler output')

    def source_changed(self):
        self.generation+=1
        self.cancel_jobs(silent=True)
        self.frames=[]
        self.compiled_source=None
        self.node=None
        if hasattr(self,'view_status'):self.view_status.setText('SOURCE CHANGED · displaying previous mesh until compilation finishes')
        self.viewer.set_handles([])
        self.viewer.set_gizmo('Dimensions')
        if hasattr(self,'inspector') and not self.repick_after_compile:
            self.trace_cancel.set();self.trace_pending=None
            if self.inspector.groups or 'analysing' in self.inspector.title.text():
                self.inspector.clear('source changed · click the model again after it recompiles')
            self.viewer.clear_pick_box()
        if hasattr(self,'project_timer'):self.project_timer.start(350)
        self.parse_timer.start(250)
        if self.auto.isChecked():self.compile_timer.start(500)
        if hasattr(self,'animation_status'): self.animation_status.setText('Source changed · Play will prepare new frames')
        self.info('Source changed · building project entry with unsaved module edits' if self.pinned_view() else 'Source changed · displayed mesh may be out of date')
        self.update_title()

    def parse(self):
        current=self.editor.toPlainText()
        selected_start=self.node.start if self.node and self.doc and self.doc.source==current else None
        try: self.doc=Document(current)
        except SourceError as e:
            self.doc=None; self.tree.clear()
            problems=source_problems(e,current,self.path or Path('<current>'))
            for problem in problems: problem.message='Syntax: '+problem.message
            self.show_problems('syntax',problems)
            where=f' (line {problems[0].line+1})' if problems and problems[0].line is not None else ''
            self.info(f'Syntax error{where}: {e}'); return False
        if self.problem_sets.get('syntax'):self.show_problems('syntax',[])
        self.tree.blockSignals(True); self.tree.clear()
        def add(node,parent,inherited=None):
            line=self.doc.source.count('\n',0,node.start)+1
            label=f'{node.name}  ·  L{line}'
            from .viewer import material_color
            color=QColor.fromRgbF(*material_color(self.doc.source,node.start,node.end)) if node.name=='color' or inherited is None else inherited
            item=QTreeWidgetItem([label,color.name()]);item.setBackground(1,QBrush(color))
            light=.2126*color.redF()+.7152*color.greenF()+.0722*color.blueF()
            item.setForeground(1,QBrush(QColor('#08101a' if light>.55 else '#ffffff')));item.setData(0,Qt.UserRole,node)
            parent.addChild(item) if parent else self.tree.addTopLevelItem(item)
            if node.start==selected_start:
                item.setSelected(True); self.node=node
            for child in node.children:
                if child.kind=='geometry': add(child,item,color)
        for node in self.doc.roots: add(node,None)
        self.tree.expandToDepth(1); self.tree.blockSignals(False)
        self.refresh_material_swatches()
        if hasattr(self,'project_timer'):self.project_timer.start(200)
        return True

    def refresh_material_swatches(self):
        if not self.view_matches_editor():return
        chunks={chunk[0]:chunk for chunk in self.viewer.scene}
        for index in range(self.tree.topLevelItemCount()):
            item=self.tree.topLevelItem(index);node=item.data(0,Qt.UserRole)
            chunk=chunks.get(node.start)
            if chunk is None:continue
            palette=getattr(chunk,'palette',())
            if len(palette)>1:
                item.setText(1,f'{len(palette)} colors');item.setBackground(1,QBrush(QColor('#202d3d')))
                item.setForeground(1,QBrush(QColor('#dce5ef')))
                item.setToolTip(1,'Evaluated module materials: '+', '.join(QColor.fromRgbF(*c).name() for c in palette)+'. Double-click to override the entire object.')
            elif palette:
                color=QColor.fromRgbF(*palette[0]);item.setText(1,color.name());item.setBackground(1,QBrush(color))
                light=.2126*color.redF()+.7152*color.greenF()+.0722*color.blueF()
                item.setForeground(1,QBrush(QColor('#08101a' if light>.55 else '#ffffff')))

    def tree_selected(self):
        items=self.tree.selectedItems()
        self.node=items[0].data(0,Qt.UserRole) if items else None
        if self.node:
            self.editor.select_source(self.node.start,self.node.end)
            root=next((r for r in self.doc.roots if r.start<=self.node.start and self.node.end<=r.end),None)
            self.viewer.highlight(root.start if root and self.view_matches_editor() else -1)
        else: self.viewer.highlight(-1)
        self.refresh_handles()

    def viewport_selected(self,start):
        if not self.view_matches_editor():
            target=getattr(self,'compiled_path',None)
            if target and self.compiled_source==self.source_for(target):
                self.open_document(target)
            if not self.view_matches_editor():
                self.info('Compile the current source before selecting geometry in the view.');return
        self.tree.clearSelection()
        if start<0: return
        for i in range(self.tree.topLevelItemCount()):
            item=self.tree.topLevelItem(i)
            if item.data(0,Qt.UserRole).start==start:
                item.setSelected(True); self.tree.scrollToItem(item); break

    def refresh_handles(self,*_):
        if not hasattr(self,'dimensions_check') or not self.dimensions_check.isChecked() or not self.doc:
            self.viewer.set_handles([]); self.viewer.set_gizmo('Dimensions'); return
        # A stale mesh must not retain writable source ranges.
        if not self.view_matches_editor():
            self.viewer.set_handles([]); return
        mode=self.object_tool.currentText() if hasattr(self,'object_tool') else 'Dimensions'
        root=next((r for r in self.doc.roots if self.node and r.start<=self.node.start and self.node.end<=r.end),None)
        self.viewer.set_gizmo(mode,root.start if root else None)
        self.viewer.set_handles(handles_for(self.doc,self.node,self.timeline.value()/1000) if mode=='Dimensions' else [])

    def apply_dimension(self,handle,value):
        if not self.view_matches_editor():
            self.info('Compile current code before dragging a dimension.'); return
        source=self.editor.toPlainText()
        updated=handle.replace(source,value)
        selected_start=self.node.start if self.node else handle.node_start
        if handle.ref.end<=selected_start: selected_start+=len(updated)-len(source)
        self.replace_source(updated)
        self.node=next((n for n in self.doc.walk() if n.start==selected_start),None)
        self.info(f'{handle.label} set to {value:.6g} mm · compiling after release')
        # One source transaction and one compile, not one CSG solve per mouse move.
        self.compile()

    def move_plane(self,value):
        self.section_value.setValue(value)

    def add_plane(self):
        self.section_check.setChecked(True)
        # Center in the model, including models translated away from world origin.
        if self.viewer.scene:
            center=np.mean(self.viewer.bounds,axis=0)
            self.section_value.setValue(float(center@self.viewer.section_plane.normal))
        self.section_changed()

    def selected(self):
        if self.parse_timer.isActive():
            self.parse_timer.stop(); self.parse()
            self.node=None
        if not self.doc:
            self.info('Fix the source syntax first.'); return None
        if self.node: return self.node
        position=self.editor.source_position()
        matches=[n for n in self.doc.walk() if n.start<=position<n.end]
        if matches: return min(matches,key=lambda n:n.end-n.start)
        self.info('Select a source object in the tree or view first.'); return None

    def replace_source(self,source):
        c=self.editor.textCursor(); c.beginEditBlock()
        c.select(QTextCursor.Document); c.insertText(source); c.endEditBlock()
        self.editor.setTextCursor(c)
        self.parse_timer.stop(); self.parse()

    def sync_roll(self,value):
        self.roll_control.blockSignals(True);self.roll_control.setValue(value);self.roll_control.blockSignals(False)

    def tree_color(self,item,column):
        if column!=1:return
        self.node=item.data(0,Qt.UserRole);self.color()

    def color(self):
        node=self.selected()
        if not node:return
        root=next((r for r in self.doc.roots if r.start<=node.start and node.end<=r.end),None)
        if not root:return
        from .viewer import material_color
        chunk=next((c for c in self.viewer.scene if c[0]==root.start),None) if self.view_matches_editor() else None
        initial=chunk.palette[0] if chunk is not None and len(getattr(chunk,'palette',()))==1 else material_color(self.doc.source,root.start,root.end)
        dialog=QColorDialog(QColor.fromRgbF(*initial),self);dialog.setWindowTitle('Object color override · Apply writes color()')
        dialog.currentColorChanged.connect(lambda c:self.viewer.preview_color(root.start,c.getRgbF()))
        if dialog.exec():self.replace_source(color_source(self.doc,node,dialog.currentColor().name()))
        else:self.viewer.preview_color(root.start,None)

    def apply_transform(self,kind,axis,amount,pivot):
        if not self.view_matches_editor():return
        root=next((r for r in self.doc.roots if r.start==self.viewer.gizmo_selected),None)
        if root is None:return
        index=self.doc.roots.index(root)
        self.replace_source(transform_source(self.doc,root,kind,axis,amount,pivot))
        self.node=self.doc.roots[index] if index<len(self.doc.roots) else None
        self.compile()

    def add_thread(self):
        dialog=ThreadDialog(self)
        if not dialog.exec():return
        if not self.path and not self.save():return
        try:includes=install_library(self.path,getattr(self,'project_root',None))
        except (OSError,ValueError) as error:self.error(str(error));return
        source=self.editor.toPlainText();code=dialog.code
        if dialog.mode.currentIndex()==2:
            node=self.selected()
            if not node:return
            root=next((r for r in self.doc.roots if r.start<=node.start and node.end<=r.end),None)
            mesh=next((m for m in self.viewer.scene if root and m[0]==root.start),None)
            if mesh:
                low,high=mesh[2].min(axis=0),mesh[2].max(axis=0)
                origin=[float((low[0]+high[0])/2),float((low[1]+high[1])/2),float(low[2])-.01]
                code=f'translate({origin}) '+code
            source=source[:node.start]+'difference() {\n'+source[node.start:node.end]+'\n'+code+'\n}'+source[node.end:]
        else:source+='\n'+code+'\n'
        if '/screws.scad>' not in source:source=includes+source
        self.replace_source(source);self.compile()

    def preview_source(self,source):
        if not self.executable:return
        self.compile_timer.stop();self.cancel_jobs(silent=True);self.generation+=1
        generation=self.generation
        render_source,directory,snapshot,target=self.render_context()
        if target==self.path:render_source=source
        if snapshot and self.path and self.path.is_relative_to(self.project_root):
            snapshot['overrides'][str(self.path.relative_to(self.project_root))]=source
        job=CompileJob(generation,render_source,directory,self.executable,[self.timeline.value()/1000],project_snapshot=snapshot,draft=self.draft_mesh.isChecked())
        job.failed.connect(self.compile_failed,Qt.QueuedConnection)
        job.totalProgress.connect(lambda value,text,g=generation:self.total_compile_progress(g,value,text),Qt.QueuedConnection)
        self.compile_bar.setValue(0);self.compile_bar.setFormat('Preview · 0%')
        job.ready.connect(lambda g,frames,seconds,logs:self.show_source_preview(g,frames,render_source,logs),Qt.QueuedConnection)
        job.finished.connect(lambda j=job:self.release_job(j),Qt.QueuedConnection)
        self.jobs.append(job);job.start()

    def show_source_preview(self,generation,frames,source,logs):
        if generation!=self.generation:return
        self.compiled_source=None;self.viewer.set_handles([]);self.viewer.set_gizmo('Dimensions')
        self.viewer.clear_cache();self.viewer.set_scene(frames[0],source)
        self.compile_bar.setValue(100);self.compile_bar.setFormat('Preview ready · 100%')
        self.view_status.setText('PREVIEW · temporary values · source unchanged until Apply')
        self.console.setPlainText(logs);self.info('PREVIEW · source unchanged until Apply')

    def transform(self):
        node=self.selected()
        if not node: return
        values=form_dialog(self,'Transform object',{'Operation':('translate','rotate','scale'),'Vector':'[10, 0, 0]'})
        if not values:return
        if values['Operation']=='scale':
            self.replace_source(self.doc.wrap(node,f'scale({values["Vector"]})'));return
        try:
            from .parameters import Scope,evaluate
            vector=evaluate(values['Vector'],Scope(self.doc.source,{name:(a,b) for name,a,b in self.doc.variables()}))
            if not isinstance(vector,list) or len(vector)!=3:raise ValueError('Enter a three-component vector')
            root=next(r for r in self.doc.roots if r.start<=node.start and node.end<=r.end)
            index=self.doc.roots.index(root);source=self.doc.source
            for axis,value in enumerate(vector):
                if float(value):
                    document=Document(source)
                    source=transform_source(document,document.roots[index],values['Operation'],axis,float(value),[0,0,0])
            self.replace_source(source);self.compile()
        except (ValueError,TypeError,KeyError,StopIteration) as error:self.error(str(error))

    def duplicate(self):
        node=self.selected()
        if not node: return
        offset,ok=QInputDialog.getText(self,'Duplicate object','Offset [x, y, z] / mm:',text='[15, 0, 0]')
        if ok: self.replace_source(self.doc.duplicate(node,offset))

    def pattern(self):
        node=self.selected()
        if not node:return
        if node not in self.doc.roots or not self.view_matches_editor():
            self.info('Compile, then select a top-level object for live pattern preview.');return
        if getattr(self,'pattern_dialog',None):self.pattern_dialog.close()
        dialog=PatternDialog(self,self.doc,node);self.pattern_dialog=dialog
        dialog.accepted.connect(lambda:self.replace_source(dialog.result_source) if dialog.result_source else None)
        dialog.finished.connect(lambda *_:setattr(self,'pattern_dialog',None))
        dialog.show()

    def boolean(self):
        if not self.doc: return
        nodes=sorted([i.data(0,Qt.UserRole) for i in self.tree.selectedItems()],key=lambda n:n.start)
        if len(nodes)<2 or any(n not in self.doc.roots for n in nodes):
            self.info('Ctrl+click at least two top-level objects in the tree. First in source = base; later objects = cutters.'); return
        operation,ok=QInputDialog.getItem(self,'Boolean operation','First source object is the base:', ['difference','union','intersection','hull'],0,False)
        if not ok: return
        src=self.doc.source
        combined=operation+'() {\n'+'\n'.join(src[n.start:n.end] for n in nodes)+'\n}'
        for n in reversed(nodes): src=src[:n.start]+(combined if n is nodes[0] else '')+src[n.end:]
        self.replace_source(src)

    def bind_animation(self):
        if not self.doc: return
        variables=self.doc.variables()
        variables=[v for v in variables if not v[0].startswith('$')]
        variables.sort(key=lambda v:(v[0]!='angle',v[0]))
        if not variables:
            self.info('Add a top-level variable, for example angle = 0;'); return
        values=form_dialog(self,'Animate a variable · writes standard $t expression',{
            'Variable':tuple(v[0] for v in variables),'Minimum':'0','Maximum':'360',
            'Curve':('Linear','Ping-pong','Sine','Custom expression'),'Custom expression':'45 * sin(360 * $t)'})
        if not values: return
        low,high=values['Minimum'],values['Maximum']
        curve={'Linear':'$t','Ping-pong':'(1 - abs(2 * $t - 1))','Sine':'((1 - cos(360 * $t)) / 2)'}
        expression=values['Custom expression'] if values['Curve']=='Custom expression' else f'({low}) + (({high}) - ({low})) * {curve[values["Curve"]]}'
        name,start,end=next(v for v in variables if v[0]==values['Variable'])
        src=self.doc.source
        self.replace_source(src[:start]+' '+expression+src[end:])
        self.animation_status.setText(f'{name} = {expression} · click Play to prepare and run')
        return True

    def section_slide(self,value):
        self.section_value.blockSignals(True); self.section_value.setValue(value*self.viewer.extent/1000); self.section_value.blockSignals(False)
        self.section_changed()

    def section_number(self,value):
        self.section_slider.blockSignals(True); self.section_slider.setValue(round(value/self.viewer.extent*1000)); self.section_slider.blockSignals(False)
        self.section_changed()

    def section_changed(self,*_):
        self.viewer.plane_visible=self.section_check.isChecked()
        self.viewer.section_on=self.section_check.isChecked() and self.clip_check.isChecked()
        self.viewer.section_axis='XYZ'.index(self.axis.currentText())
        self.viewer.section_pos=self.section_value.value()
        self.viewer.section_flip=-1. if self.flip.isChecked() else 1.
        self.viewer.section_angles=tuple(w.value() for w in self.plane_angles)
        self.viewer.update_section()

    def commit_section(self):
        if not self.doc or not self.doc.roots: return
        if not self.section_check.isChecked():
            self.add_plane(); self.info('Inspect the plane first, then click Write cut to code.'); return
        extent=self.viewer.extent
        if not self.view_matches_editor():
            self.info('Compile first so the cut covers the complete current model.'); return
        if abs(self.section_value.value()) > extent/2:
            self.info('Move the plane within the model bounds before writing the cut.'); return
        self.replace_source(section_source(self.doc,self.viewer.section_plane,extent,self.flip.isChecked()))
        self.section_check.setChecked(False)

    def export_section(self):
        if not self.doc or not self.doc.roots: return
        if not self.section_check.isChecked():
            self.add_plane(); self.info('Inspect the plane, then click Export KiCad DXF again.'); return
        path,_=QFileDialog.getSaveFileName(self,'Export plane cross-section for KiCad Edge.Cuts','section.dxf','DXF (*.dxf)')
        if path:
            if not path.lower().endswith('.dxf'): path+='.dxf'
            self.compile(export_path=path,source_override=projection_source(self.doc,self.viewer.section_plane))

    # ------------------------------------------------------------------- KiCad
    def insert_pcb_template(self):
        if not self.path and not self.save():return
        import shutil
        library=Path(__file__).resolve().parent.parent/'assets'/'scadforge'/'pcb.scad'
        root=getattr(self,'project_root',None) or self.path.parent
        target=root/'lib'/'scadforge'/'pcb.scad'
        try:
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists() or target.read_bytes()!=library.read_bytes():shutil.copy2(library,target)
        except OSError as error:self.error(str(error));return
        relative=os.path.relpath(target,self.path.parent).replace(os.sep,'/')
        source=self.editor.toPlainText()
        include=f'include <{relative}>\n'
        if include.strip() not in source:source=include+source
        if 'pcb_board(' not in source:
            source+=('\n// Board: outline + holes. Edit sizes here or click the model and use the Inspector.\n'
                     'pcb_board(size = [80, 50], radius = 3) {\n'
                     '    for (p = [[4, 4], [76, 4], [4, 46], [76, 46]]) pcb_mounting_hole(str("H", p[0] + p[1]), p);\n'
                     '}\n'
                     'pcb_part(ref = "U1", footprint = "Package_QFP:LQFP-48_7x7mm_P0.5mm", pos = [40, 25], rot = 45, body = [7, 7, 1.4]);\n'
                     'pcb_connector(ref = "J1", footprint = "Connector_USB:USB_C_Receptacle_GCT_USB4085", pos = [76, 25], rot = 90, body = [9, 7.5, 3.3]);\n'
                     'pcb_part(ref = "C1", footprint = "Capacitor_SMD:C_0603_1608Metric", pos = [30, 15], side = "bottom", value = "100n", body = [1.6, 0.8, 0.8]);\n')
        self.replace_source(source);self.compile()
        self.info(f'PCB library installed in {target.parent} · PCB → Export KiCad board when the shape is ready')

    def export_kicad(self):
        if not self.parse():return
        from .kicad import from_trace,write_kicad_pcb,write_placement_csv,outline_from_svg
        default=(self.path.with_suffix('.kicad_pcb') if self.path else Path('board.kicad_pcb'))
        name,_=QFileDialog.getSaveFileName(self,'Export KiCad board',str(default),'KiCad PCB (*.kicad_pcb)')
        if not name:return
        target=Path(name if name.endswith('.kicad_pcb') else name+'.kicad_pcb')
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            texts=self.trace_snapshot()
            def loader(file):
                resolved=str(Path(file).resolve())
                return texts[resolved] if resolved in texts else Path(file).read_text(encoding='utf-8-sig')
            result=trace_design(self.path,self.editor.toPlainText(),loader=loader,t=self.timeline.value()/1000)
            board=from_trace(result)
            if not board.has_outline:
                board.polygons=self.section_outline()
                if board.polygons:board.notes.append('Outline from an OpenSCAD cross-section at the construction plane / model mid-height.')
            if not board.has_outline:
                self.error('No board outline found. Use pcb_board() (PCB → Insert PCB library) or show the construction plane through the board.');return
            target.write_text(write_kicad_pcb(board,title=(self.path.stem if self.path else 'SCAD Forge board')),encoding='utf-8')
            csv_path=target.with_name(target.stem+'-placement.csv')
            csv_path.write_text(write_placement_csv(board),encoding='utf-8')
        except (OSError,RuntimeError,ValueError) as error:
            self.error(str(error));return
        finally:
            QGuiApplication.restoreOverrideCursor()
        unassigned=[f.ref for f in board.footprints if not f.footprint]
        message=(f'Exported {target.name}: outline, {len(board.circles)} holes, {len(board.footprints)} footprints'
                 f' + {csv_path.name}. In KiCad: open the board, then Tools → Update Footprints from Library to load pads.')
        if unassigned:message+=f' Parts without footprint=: {", ".join(unassigned[:8])}.'
        self.console.setPlainText(message+('\n'+'\n'.join(board.notes) if board.notes else ''))
        self.bottom_tabs.setCurrentWidget(self.console);self.info(message)

    def section_outline(self):
        """Board outline polygons from OpenSCAD's cut through the model (fallback)."""
        import subprocess,tempfile
        from .kicad import outline_from_svg
        from .plane import Plane
        if not self.executable or not self.doc or not self.doc.roots:return []
        if self.section_check.isChecked():plane=self.viewer.section_plane
        else:
            z=float(np.mean(self.viewer.bounds,axis=0)[2]) if self.viewer.scene else 0.
            low=float(self.viewer.bounds[0][2]) if self.viewer.scene else 0.
            plane=Plane('Z',low+min(.8,max(.01,(z-low))),(0.,0.,0.))
        source=projection_source(self.doc,plane)
        directory=self.path.parent if self.path else Path.cwd()
        with tempfile.TemporaryDirectory(prefix='scadforge-kicad-') as tmp:
            scad=Path(tempfile.mkstemp(prefix='.scadforge-',suffix='.scad',dir=directory)[1])
            try:
                scad.write_text(source,encoding='utf-8')
                svg=Path(tmp)/'outline.svg'
                env=dict(os.environ,QT_QPA_PLATFORM='offscreen')
                subprocess.run([self.executable,'-o',str(svg),str(scad)],cwd=directory,env=env,capture_output=True,timeout=300)
                return outline_from_svg(svg.read_text()) if svg.exists() else []
            finally:
                scad.unlink(missing_ok=True)

    def cancel_jobs(self,*_,silent=False):
        for job in self.jobs:
            if job.isRunning(): job.cancel()
        self.play_timer.stop()
        self.autoplay_generation=None
        if hasattr(self,'play_button'): self.play_button.setText('▶ Play')
        if not silent:
            self.compile_timer.stop(); self.info('Compilation / playback cancelled')
            self.compile_bar.setFormat('Cancelled')

    def compile(self,*_,times=None,export_path=None,source_override=None,autoplay=False):
        self.compile_timer.stop()
        self.parse_timer.stop()
        if not self.parse(): return
        if not self.executable or not Path(self.executable).is_file():
            self.error('OpenSCAD was not found. Install OpenSCAD, then Settings → OpenSCAD path…'); return
        self.cancel_jobs(silent=True)
        self.generation+=1
        generation=self.generation
        self.autoplay_generation=generation if autoplay else None
        if autoplay: self.play_button.setText('Cancel bake')
        if times is not None and len(times)>1: self.animation_status.setText(f'Preparing {len(times)} frames in background…')
        source,directory,snapshot,target=self.render_context(source_override)
        if times is None: times=[self.timeline.value()/1000]
        self.info('Compiling in background · orbit remains available')
        self.view_status.setText('COMPILING · '+(target.name if target else 'Untitled.scad')+' · previous mesh remains available')
        job=CompileJob(generation,source,directory,self.executable,times,export_path,project_snapshot=snapshot,draft=self.draft_mesh.isChecked())
        self.compile_context={'generation':generation,'job':job,'target':target,'directory':directory}
        job.progress.connect(lambda s,g=generation:self.compile_progress(g,s), Qt.QueuedConnection)
        job.totalProgress.connect(lambda value,text,g=generation:self.total_compile_progress(g,value,text),Qt.QueuedConnection)
        self.compile_bar.setValue(0);self.compile_bar.setFormat('Compiling · 0%')
        job.failed.connect(self.compile_failed, Qt.QueuedConnection)
        job.ready.connect(lambda g,frames,seconds,logs:self.compiled(g,frames,seconds,logs,source,export_path,target), Qt.QueuedConnection)
        job.finished.connect(lambda j=job:self.release_job(j), Qt.QueuedConnection)
        self.jobs.append(job); job.start()

    def total_compile_progress(self,generation,value,text):
        if generation!=self.generation:return
        self.compile_bar.setValue(max(self.compile_bar.value(),min(99,value)))
        self.compile_bar.setFormat('Compiling · %p%');self.compile_bar.setToolTip(text)

    def compile_progress(self,generation,text):
        if generation!=self.generation: return
        self.info(text)
        if self.autoplay_generation==generation: self.animation_status.setText(text)

    def compile_failed(self,generation,text):
        if generation!=self.generation: return
        self.autoplay_generation=None
        self.play_button.setText('▶ Play')
        self.animation_status.setText('Compilation failed · see output below')
        self.compile_bar.setFormat('Failed · see output')
        self.compiled_source=None
        self.viewer.set_handles([]);self.viewer.set_gizmo('Dimensions')
        self.console.setPlainText(text)
        problems=self.compile_problems(text)
        self.show_problems('compile',problems)
        first=next((p for p in problems if p.severity=='error'),None)
        if first is not None:
            where=f'{first.location} · ' if first.location else ''
            self.view_status.setText(f'COMPILATION FAILED · {where}{first.message} · previous mesh shown')
            self.info(f'Compilation failed · {where}{first.message} · click it in Problems or press :cn')
            self.bottom_tabs.setCurrentWidget(self.problems_panel)
        else:
            self.view_status.setText('COMPILATION FAILED · displaying previous mesh · see Output')
            self.info('Action failed · see compiler output')
            self.bottom_tabs.setCurrentWidget(self.console)

    def release_job(self,job):
        if job in self.jobs: self.jobs.remove(job)
        job.deleteLater()

    def compiled(self,generation,frames,seconds,logs,source,export_path,target=None):
        if generation!=self.generation: return
        problems=self.compile_problems(logs)
        if not export_path and any(p.severity=='error' for p in problems) and not any(len(c[3]) for frame in frames for c in frame):
            # OpenSCAD can exit 0 after ERROR (assert, recursion…) with an empty
            # model: report a failure and keep the previous mesh.
            self.compile_failed(generation,logs);return
        self.console.setPlainText(logs)
        self.show_problems('compile',problems)
        if any(p.severity=='error' for p in problems):self.bottom_tabs.setCurrentWidget(self.problems_panel)
        self.compile_bar.setValue(100);self.compile_bar.setFormat('Complete · 100%')
        if export_path:
            self.info(f'Exported {export_path} · {seconds:.2f}s'+(' · KiCad: File → Import → Graphics, Edge.Cuts, mm, scale 1' if str(export_path).lower().endswith('.dxf') else '')); return
        fit=not bool(self.viewer.scene)
        self.compiled_source=source
        self.compiled_path=target
        warning=any(term in logs for term in ('WARNING:','ERROR:'))
        self.view_status.setText(('COMPILED WITH WARNINGS · see output · ' if warning else 'GPU VIEW · ')+(target.name if target else 'Untitled.scad')+(' · DRAFT 24 segments' if self.draft_mesh.isChecked() else ''))
        self.frames=frames if len(frames)>1 else []
        self.frame_index=0
        self.viewer.clear_cache()
        self.viewer.set_scene(frames[0],source,fit)
        self.refresh_material_swatches()
        if self.frames:
            self.timeline.blockSignals(True); self.timeline.setValue(0); self.timeline.blockSignals(False)
            self.time_label.setText('0.000')
        self.refresh_handles()
        self.trace_result=None;self.trace_key=None
        if self.repick_after_compile and self.last_pick:
            self.repick_after_compile=False
            self.inspect_point(*self.last_pick)
        else:
            self.viewer.clear_pick_box()
            QTimer.singleShot(50,self.request_trace)
        triangles=sum(len(data[3]) for data in frames[0])
        self.info(f'Compiled · {seconds:.2f}s · {triangles:,} triangles · {len(frames)} frame(s) · GPU view ready')
        if self.frames:
            changed=any(not self.same_scene(frames[0],frame) for frame in frames[1:])
            self.animation_status.setText(f'{len(frames)} frames ready · '+('animated' if changed else 'no visible movement: check variable binding/range'))
            if self.autoplay_generation==generation:
                self.autoplay_generation=None
                self.start_playback()
        else:
            self.play_button.setText('▶ Play')

    @staticmethod
    def same_scene(first,second):
        if len(first)!=len(second): return False
        for a,b in zip(first,second):
            if any(x.shape!=y.shape or not np.array_equal(x,y) for x,y in zip(a[2:],b[2:])):return False
            if getattr(a,'color',None)!=getattr(b,'color',None):return False
            ra,rb=getattr(a,'render',None),getattr(b,'render',None)
            if (ra is None)!=(rb is None):return False
            if ra is not None and any(x.shape!=y.shape or not np.array_equal(x,y) for x,y in zip(ra,rb)):return False
        return True

    def animation_count_changed(self,*_):
        self.cancel_jobs(silent=True); self.generation+=1; self.frames=[]
        self.animation_status.setText('Frame count changed · click Play to prepare again')

    def bake(self):
        self.compile(times=np.linspace(0,1,self.frame_count.value(),endpoint=False).tolist())

    def start_playback(self):
        if not self.frames: return
        self.play_timer.start(round(1000/self.fps.value())); self.play_button.setText('⏸ Pause')
        self.viewer.set_handles([])

    def play(self):
        if self.play_timer.isActive():
            self.play_timer.stop(); self.play_button.setText('▶ Play'); self.refresh_handles(); return
        if self.autoplay_generation is not None:
            self.generation+=1; self.cancel_jobs(); self.animation_status.setText('Preparation cancelled'); return
        if not self.frames:
            self.parse_timer.stop()
            if not self.parse(): return
            if not any(t.value=='$t' for t in self.doc.tokens):
                self.info('Bind a variable to $t to create motion.')
                if not self.bind_animation(): return
            self.compile(times=np.linspace(0,1,self.frame_count.value(),endpoint=False).tolist(),autoplay=True)
            return
        self.start_playback()

    def next_frame(self):
        if not self.frames: self.play_timer.stop(); return
        self.frame_index=(self.frame_index+1)%len(self.frames)
        value=round(self.frame_index/len(self.frames)*1000)
        self.timeline.blockSignals(True); self.timeline.setValue(value); self.timeline.blockSignals(False)
        self.time_label.setText(f'{value/1000:.3f}')
        self.viewer.set_scene(self.frames[self.frame_index],self.compiled_source)

    def scrub(self,value):
        self.time_label.setText(f'{value/1000:.3f}')
        if self.frames:
            self.frame_index=min(len(self.frames)-1,int(value/1000*len(self.frames)))
            self.viewer.set_scene(self.frames[self.frame_index],self.compiled_source)
        else:
            self.generation+=1
            self.cancel_jobs(silent=True)
            self.compile_timer.start(300)
        if not self.play_timer.isActive(): self.refresh_handles()

    # ------------------------------------------------------------ diagnostics
    def compile_problems(self,text):
        context=self.compile_context or {}
        job=context.get('job')
        target=context.get('target')
        mapper=PathMapper(target or Path('<current>'),context.get('directory'),
                          getattr(job,'stage_root',None),getattr(self,'project_root',None))
        return parse_log(text,mapper)

    def same_file(self,a,b):
        if a is None or b is None:return False
        try:return Path(a).resolve()==Path(b).resolve()
        except OSError:return str(a)==str(b)

    def show_problems(self,kind,problems):
        self.problem_sets[kind]=list(problems)
        everything=self.problem_sets.get('syntax',[])+self.problem_sets.get('compile',[])
        self.problems_panel.set_problems(everything)
        for editor,path in getattr(self,'buffers',{self.editor:self.path}).items():
            mine=[]
            for p in everything:
                if p.line is None:continue
                current=str(p.file)=='<current>'
                if (current and (editor is self.editor or path is None)) or (path is not None and self.same_file(p.file,path)):
                    mine.append(Diagnostic(p.line,p.column,p.severity,p.message+(f'  —  {p.hint}' if p.hint else '')))
            editor.set_diagnostics(mine)
        label='Problems'+(f' ({len(everything)})' if everything else '')
        index=self.bottom_tabs.indexOf(self.problems_panel)
        if index>=0:self.bottom_tabs.setTabText(index,label)
        errors=sum(p.severity=='error' for p in everything)
        if errors:self.compile_bar.setFormat(count_text(everything))

    def goto_problem(self,file,line,column=0):
        if file is None:return
        if str(file)!='<current>':
            if not self.same_file(file,self.path) and not self.open_document(file):return
        self.editor.goto_line(line,column)

    def next_problem(self,step=1):
        problems=[p for p in self.problems_panel.problems if p.line is not None]
        if not problems:self.info('No problems');return
        self.problem_index=(getattr(self,'problem_index',-1)+step)%len(problems)
        p=problems[self.problem_index]
        self.goto_problem(p.file,p.line,p.column or 0)
        self.info(f'({self.problem_index+1} of {len(problems)}) {p.severity}: {p.message}')

    # ---------------------------------------------------------------- inspector
    def trace_snapshot(self):
        texts={}
        for editor,path in getattr(self,'buffers',{}).items():
            if path is not None:texts[str(Path(path).resolve())]=editor.toPlainText()
        return texts

    def request_trace(self,then=None):
        if self.compiled_source is None:return
        key=(self.compiled_source,str(getattr(self,'compiled_path',None)),self.timeline.value())
        if self.trace_key==key and self.trace_result is not None:
            if then:then()
            return
        if self.trace_pending==key:
            if then:self.trace_then=then
            return
        self.trace_pending=key;self.trace_then=then
        self.trace_cancel.set();cancel=self.trace_cancel=threading.Event()
        texts=self.trace_snapshot();source=self.compiled_source;path=getattr(self,'compiled_path',None)
        t=self.timeline.value()/1000
        def loader(file):
            resolved=str(Path(file).resolve())
            return texts[resolved] if resolved in texts else Path(file).read_text(encoding='utf-8-sig')
        def work():
            result=trace_design(path,source,loader=loader,t=t,cancel=cancel)
            if not cancel.is_set():self.traceReady.emit(key,result)
        threading.Thread(target=work,daemon=True,name='trace').start()

    def trace_finished(self,key,result):
        if key!=self.trace_pending:return
        self.trace_pending=None;self.trace_key=key;self.trace_result=result
        then=getattr(self,'trace_then',None);self.trace_then=None
        if then:then()

    def inspect_point(self,start,point):
        if point is None or start<0:
            self.inspector.clear();self.viewer.clear_pick_box();return
        if self.compiled_source is None:
            self.inspector.clear('compile first, then click the model');return
        self.last_pick=(start,point)
        if self.trace_result is None or self.trace_key is None or self.trace_key[0]!=self.compiled_source:
            self.inspector.clear('analysing the design…')
            self.bottom_tabs.setCurrentWidget(self.inspector)
            self.request_trace(lambda:self.pick_leaf(start,point))
            return
        self.pick_leaf(start,point)

    def pick_leaf(self,start,point):
        result=self.trace_result
        chunk=next((c for c in self.viewer.scene if c[0]==start),None)
        if result is None or chunk is None:return
        leaf=result.pick(point,(chunk[0],chunk[1]))
        self.bottom_tabs.setCurrentWidget(self.inspector)
        if leaf is None:
            note=' (partial analysis: design too large or uses unsupported features)' if result.partial else ''
            self.inspector.clear('no source primitive found at this point'+note+' — imported meshes and some library internals cannot be traced')
            self.viewer.clear_pick_box();return
        groups=Explainer(result).explain(leaf)
        self.inspector.show_leaf(leaf,groups,result.files)
        box=leaf.world_box()
        if box is not None:self.viewer.show_pick_box(*box,cutter=leaf.cutter)
        user=next((g for g in groups if g.kind!='variables' and not g.library and g.file==0),None)
        if user is not None and self.view_matches_editor():
            self.editor.select_source(user.position,user.position)
        self.info('Inspector: edit a value and press Enter · changes are written to the .scad source')

    def preview_inspector_color(self,rgba):
        if self.last_pick:self.viewer.preview_color(self.last_pick[0],rgba)

    def editor_for_path(self,path,create=True):
        for editor,existing in self.buffers.items():
            if (path is None and editor is self.editor and existing is None) or (path is not None and existing is not None and self.same_file(existing,path)):
                return editor
        if not create or path is None:return None
        current=self.editor_tabs.currentIndex()
        if not self.open_document(path):return None
        editor=self.editor
        if current>=0:self.editor_tabs.setCurrentIndex(current)
        return editor

    def apply_inspector_edit(self,target,text):
        result=self.trace_result
        if result is None:return
        path,traced=result.files[target.file]
        compiled_path=getattr(self,'compiled_path',None)
        if path is None:path=compiled_path
        editor=self.editor_for_path(path) if path is not None else self.editor
        if editor is None:self.error(f'Cannot open {path}');return
        if editor.toPlainText()!=traced:
            self.info('The source changed since this part was analysed · click the model again');return
        try:_,new=apply_edit(result.files,target,text)
        except ValueError as error:self.error(str(error));return
        prefix=0
        while prefix<min(len(traced),len(new)) and traced[prefix]==new[prefix]:prefix+=1
        suffix=0
        while suffix<min(len(traced),len(new))-prefix and traced[-1-suffix]==new[-1-suffix]:suffix+=1
        from PySide6.QtGui import QTextCursor
        self.repick_after_compile=True
        cursor=QTextCursor(editor.document());cursor.beginEditBlock()
        cursor.setPosition(editor.buffer.to_qt(prefix))
        cursor.setPosition(editor.buffer.to_qt(len(traced)-suffix),QTextCursor.KeepAnchor)
        cursor.insertText(new[prefix:len(new)-suffix]);cursor.endEditBlock()
        in_project=bool(self.project_root and path is not None and Path(path).resolve().is_relative_to(self.project_root))
        if editor is not self.editor and not in_project and path is not None:
            self.save_buffer(editor)   # OpenSCAD reads non-project dependencies from disk
        where=f'{Path(path).name}:{traced.count(chr(10),0,target.start)+1}' if path else 'source'
        self.info(f'Inspector edit written to {where} · recompiling (u in that file undoes it)')
        self.repick_after_compile=True
        if editor is self.editor:self.parse_timer.stop();self.parse()
        self.refresh_tab_titles()
        self.compile()

    def goto_traced(self,file_index,position):
        result=self.trace_result
        if result is None:return
        path=result.files[file_index][0] or getattr(self,'compiled_path',None)
        if path is not None and not self.same_file(path,self.path):
            if not self.open_document(path):return
        self.editor.select_source(position,position);self.editor.setFocus()

    def set_vim(self,enabled):
        self.vim_enabled=bool(enabled);self.settings.setValue('vim','true' if enabled else 'false')
        for editor in getattr(self,'buffers',{self.editor:None}):editor.toggle_vim(self.vim_enabled)
        self.mode_label.setText(self.editor.mode)

    def configure_compiler(self):
        path,_=QFileDialog.getOpenFileName(self,'Choose OpenSCAD executable',self.executable or '')
        if path: self.executable=path; self.settings.setValue('openscad',path); self.compile()

    def export_stl(self):
        path,_=QFileDialog.getSaveFileName(self,'Export combined OpenSCAD solid','','STL (*.stl)')
        if path:
            if not path.lower().endswith('.stl'): path+='.stl'
            self.compile(export_path=path)

    def update_title(self):
        name=self.path.name if self.path else 'Untitled.scad'
        self.setWindowTitle(name+(' *' if self.editor.document().isModified() else '')+' — SCAD Forge 0.7')
        if hasattr(self,'file_label'): self.file_label.setText('  '+name)

    def maybe_save(self):
        if not self.editor.document().isModified(): return True
        result=QMessageBox.question(self,'Unsaved source','Save changes before continuing?',QMessageBox.Save|QMessageBox.Discard|QMessageBox.Cancel)
        if result==QMessageBox.Cancel: return False
        return self.save() if result==QMessageBox.Save else True

    def new_file(self):
        editor=Editor();self.wire_editor(editor);self.buffers[editor]=None
        self.switching=True
        index=self.editor_tabs.addTab(editor,'Untitled.scad')
        editor.setPlainText('// New OpenSCAD model\n$fn=64;\ncube([20,20,20],center=true);\n');editor.document().setModified(False)
        self.switching=False;self.editor_tabs.setCurrentIndex(index);self.activate_document(index)

    def open_file(self):
        name,_=QFileDialog.getOpenFileName(self,'Open OpenSCAD source','','OpenSCAD (*.scad);;All files (*)')
        if name:self.load_file(Path(name))

    def load_file(self,path):
        path=Path(path).resolve()
        if path.is_dir():self.set_project(path);return True
        if (path.parent/'forge-project.json').is_file() and self.project_root!=path.parent:
            self.set_project(path.parent)
        return self.open_document(path)

    def save_as(self,target):
        return self.save(Path(target))

    def save(self,target=None):
        target=target if isinstance(target,Path) else self.path
        if target is None:
            name,_=QFileDialog.getSaveFileName(self,'Save portable OpenSCAD source','model.scad','OpenSCAD (*.scad)')
            if not name: return False
            target=Path(name if name.lower().endswith('.scad') else name+'.scad')
        try:
            # Atomic replacement avoids truncating an existing design on a failed write.
            import tempfile
            with tempfile.NamedTemporaryFile('w',dir=target.parent,suffix='.scadforge-save',delete=False,encoding='utf-8') as f:
                f.write(self.editor.toPlainText()); temp=f.name
            try: os.replace(temp,target)
            finally: Path(temp).unlink(missing_ok=True)
            self.path=target.resolve(); self.editor.document().setModified(False)
            if hasattr(self,'buffers'):self.buffers[self.editor]=self.path;self.refresh_tab_titles();self.refresh_project()
            self.update_title(); self.info(f'Saved {target.name}'); return True
        except OSError as e: self.error(str(e)); return False

    def vim_command(self,command):
        """Ex commands the Vim engine forwards to the application; True if handled."""
        command=command.strip()
        m=re.match(r'([A-Za-z]+)(!?)\s*(.*)$',command)
        if not m:return False
        name,bang,arg=m[1],m[2]=='!',m[3].strip()
        if name in ('w','write'):
            if arg:
                self.path=None
                target=Path(arg).expanduser()
                if not target.is_absolute():target=(self.path.parent if self.path else Path.cwd())/target
                return self.save_as(target) or True
            self.save();return True
        if name in ('wa','wall'):self.save_all();return True
        if name in ('x','xit','wq','exit'):
            if self.save():self.close()
            return True
        if name in ('xa','wqa','xall','wqall'):
            if self.save_all():self.close()
            return True
        if name in ('q','quit','qa','qall','quita'):
            if bang:
                for editor in self.buffers:editor.document().setModified(False)
            self.close();return True
        if name in ('e','edit'):
            if not arg:
                if bang and self.path:
                    text=self.path.read_text(encoding='utf-8-sig');self.replace_source(text);self.editor.document().setModified(False)
                return True
            target=Path(arg).expanduser()
            if not target.is_absolute():target=(self.path.parent if self.path else Path.cwd())/target
            if not target.exists():target.write_text('',encoding='utf-8')
            self.load_file(target);return True
        if name in ('mak','make','compile','comp','build'):self.compile();return True
        if name in ('cn','cnext','cne'):self.next_problem(1);return True
        if name in ('cp','cprevious','cprev','cN'):self.next_problem(-1);return True
        if name in ('cope','copen','cl','clist','cw','cwindow'):self.bottom_tabs.setCurrentWidget(self.problems_panel);return True
        if name in ('inspect','insp'):self.bottom_tabs.setCurrentWidget(self.inspector);return True
        if name in ('bn','bnext','tabn','tabnext'):self.editor_tabs.setCurrentIndex((self.editor_tabs.currentIndex()+1)%self.editor_tabs.count());return True
        if name in ('bp','bprevious','bN','tabp','tabprevious','tabN'):self.editor_tabs.setCurrentIndex((self.editor_tabs.currentIndex()-1)%self.editor_tabs.count());return True
        if name in ('b','buffer') and arg.isdigit():self.editor_tabs.setCurrentIndex(max(0,min(self.editor_tabs.count()-1,int(arg)-1)));return True
        if name in ('bd','bdelete','tabc','tabclose'):self.close_document(self.editor_tabs.currentIndex());return True
        if name in ('export','exp'):self.export_stl();return True
        if name in ('kicad',):self.export_kicad();return True
        if name in ('h','help'):self.help();return True
        return False

    def help(self):
        QMessageBox.information(self,f'SCAD Forge {__version__} · Controls & limits',
            'VIM (complete modal editing): motions h j k l w W b B e E ge gE 0 ^ $ g_ gg G H M L { } ( ) % [( [{ ]) ]} f F t T ; , * # n N / ? marks ` \' and Ctrl-O/Ctrl-I jumps.\n'
            'Operators d c y > < = g~ gu gU gc (comment) with any motion or text object iw aw iW aW ip ap i( a( i{ a{ i[ a[ i" a" i\' a\'. '
            'Visual v V Ctrl-V (block insert I/A). Registers "a–"z, "0–"9, "+ (system clipboard; yank/put use it by default), dot repeat ., macros q/@, Ctrl-A/Ctrl-X on numbers, R replace, J join, u / Ctrl-R undo.\n'
            'Ex: :w :q :wq :x :e file :s/a/b/g :%s :g/pat/d :v :normal :sort :m :t :d :> :noh :set (ic scs sw ts rnu…) :reg :marks :make :cn :cp (jump between errors) :bn :bp.\n\n'
            'INSPECTOR: click any part of the model. The panel shows the primitive, every module call above it (in any file), its colors, loop values and the variables it uses. '
            'Edit a value and press Enter (mouse wheel / ↑↓ nudge numbers). "definition" edits where the value is written (shared); "only this call" overrides just that call.\n'
            'PROBLEMS: syntax errors are shown while typing; OpenSCAD errors and warnings after compiling — red/yellow markers in the gutter, wavy underline, hover for the message, click in Problems or :cn to jump.\n\n'
            'Touchpad: left drag = orbit; Shift+left drag = pan; two-finger scroll/pinch = zoom. Middle/right navigation also supported.\n'
            'Object tool: Dimensions / Move / Rotate. Drag colored axes; release commits SCAD. Ctrl+left drag rolls camera. Panel arrows and cube minus minimize.\n'
            'Project: open folder, file tabs, F12 module definition, Ctrl+B saves project files and builds entry.\n'
            'Select a visible top-level object in the viewport; select nested expressions in the tree. Ctrl+click tree objects for booleans.\n\n'
            'OpenSCAD is required. Sources remain ordinary .scad. F5 compiles solid meshes; F6 exports the combined STL.\n'
            'Show plane previews before code edits. Drag its blue square; set axis/tilt. Export KiCad DXF exports the true section for Edge.Cuts, in mm.\n'
            'Play prepares missing animation frames automatically. Bind a variable to $t to create movement. Complex models take longer.\n'
            'Nested module colors, color variables and loop materials are evaluated by OpenSCAD. STL export contains geometry only. Material-generating hull/minkowski/offset operations use the first material unless color() wraps the result.\n'
            'The lexical source tree is not a complete OpenSCAD language server. Full Neovim compatibility is not implemented.')

    def closeEvent(self,event):
        if not self.maybe_save_all(): event.ignore(); return
        self.cancel_jobs(silent=True)
        if getattr(self,'pattern_dialog',None):self.pattern_dialog.reject()
        self.viewer.contour_cancel.set(); self.viewer.contour_timer.stop()
        for job in list(self.jobs): job.wait()
        event.accept()


def main():
    fmt=QSurfaceFormat(); fmt.setDepthBufferSize(24); fmt.setSamples(4)
    QSurfaceFormat.setDefaultFormat(fmt)
    app=QApplication(sys.argv); app.setStyle('Fusion')
    palette=QPalette()
    for role,color in ((QPalette.Window,'#101720'),(QPalette.Base,'#151d28'),(QPalette.AlternateBase,'#202d3d'),
        (QPalette.WindowText,'#dce5ef'),(QPalette.Text,'#dce5ef'),(QPalette.Button,'#253548'),
        (QPalette.ButtonText,'#dce5ef'),(QPalette.Highlight,'#325269'),(QPalette.HighlightedText,'#ffffff'),(QPalette.ToolTipBase,'#202d3d'),(QPalette.ToolTipText,'#dce5ef')):
        palette.setColor(role,QColor(color))
    app.setPalette(palette);app.setStyleSheet(STYLE)
    window=Window(sys.argv[1] if len(sys.argv)>1 else None)
    window.show()
    sys.exit(app.exec())
