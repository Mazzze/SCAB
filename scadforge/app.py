import os
import sys
import re
from pathlib import Path
import numpy as np
from PySide6.QtCore import Qt, QTimer, QSettings
from PySide6.QtGui import QAction, QKeySequence, QTextCursor, QColor, QSurfaceFormat, QBrush, QPalette
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QPlainTextEdit,
    QFileDialog, QMessageBox, QInputDialog, QColorDialog, QCheckBox, QComboBox,
    QDoubleSpinBox, QSpinBox, QSlider, QDialog, QFormLayout, QDialogButtonBox,
    QGroupBox, QAbstractItemView, QProgressBar)
from .model import Document, SourceError
from .editor import Editor
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
        self.setWindowTitle('SCAD Forge 0.7')
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
        self.viewer.dimensionEdited.connect(self.apply_dimension)
        self.viewer.transformEdited.connect(self.apply_transform)
        self.viewer.dimensionPreview.connect(self.info)
        self.viewer.planeMoved.connect(self.move_plane)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumHeight(115)
        self.console.setPlaceholderText('OpenSCAD compiler output · F5 to compile')
        self.hint = QLabel('cube(size = [x, y, z], center = false) · Ctrl+Space: completion')
        self.hint.setObjectName('hint')
        self.hint.setWordWrap(True)
        self.editor.hintChanged.connect(lambda s: self.hint.setText(s or 'Ctrl+Space: completion · F5: compile · Esc: normal mode'))
        self.mode_label = QLabel('NORMAL')
        self.editor.modeChanged.connect(self.mode_label.setText)
        self.editor.command.connect(self.vim_command)
        self.editor.textChanged.connect(lambda e=self.editor:self.document_changed(e) if hasattr(self,'buffers') else self.source_changed())
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
        vim=QCheckBox('Vim'); vim.setChecked(True); vim.toggled.connect(self.editor.toggle_vim); toolbar.addWidget(vim)
        compiler=QAction('OpenSCAD path…',self); compiler.triggered.connect(self.configure_compiler)
        self.menuBar().addMenu('Settings').addAction(compiler)
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
        rl.addLayout(vh); rl.addWidget(self.viewer,1)
        nav=QLabel('  Click: select · Left drag: orbit · Shift + left drag: pan · Two-finger scroll: zoom')
        nav.setWordWrap(True); rl.addWidget(nav)
        tools_row=QHBoxLayout()
        tools_row.addWidget(QLabel('Object tool'))
        self.object_tool=QComboBox();self.object_tool.addItems(['Dimensions','Move','Rotate']);self.object_tool.currentTextChanged.connect(self.refresh_handles);tools_row.addWidget(self.object_tool)
        tools_row.addWidget(QLabel('Camera roll'))
        self.roll_control=QDoubleSpinBox();self.roll_control.setRange(-180,180);self.roll_control.setSuffix('°');self.roll_control.valueChanged.connect(self.viewer.set_roll);tools_row.addWidget(self.roll_control)
        self.viewer.rollChanged.connect(self.sync_roll)
        rl.addLayout(tools_row)
        self.dimensions_check=QCheckBox('Show color-coded handles · XYZ: red / green / blue')
        self.dimensions_check.setChecked(True); self.dimensions_check.toggled.connect(self.refresh_handles); rl.addWidget(self.dimensions_check)
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
        self.section_panel=Collapsible('Construction plane / KiCad',section)
        rl.addWidget(self.section_panel)
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
        self.animation_panel=Collapsible('Animation',animation_container)
        rl.addWidget(self.animation_panel); rl.addWidget(self.console)
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
            self.doc=None; self.tree.clear(); self.info(str(e)); return False
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
        self.view_status.setText('COMPILATION FAILED · displaying previous mesh · see output below')
        self.error(text)

    def release_job(self,job):
        if job in self.jobs: self.jobs.remove(job)
        job.deleteLater()

    def compiled(self,generation,frames,seconds,logs,source,export_path,target=None):
        if generation!=self.generation: return
        self.console.setPlainText(logs)
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

    def save(self):
        target=self.path
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
        if command in ('w','write'): self.save()
        elif command=='wq':
            if self.save(): self.close()
        elif command=='q': self.close()
        elif command=='q!': self.editor.document().setModified(False); self.close()
        elif command.startswith('/'):
            self._search=command[1:]
            self.editor.find(self._search)
        elif command.isdigit():
            block=self.editor.document().findBlockByNumber(max(0,int(command)-1))
            if block.isValid(): self.editor.setTextCursor(QTextCursor(block))
        else: self.info('Supported commands: :w :q :wq :q! :line :/search')

    def help(self):
        QMessageBox.information(self,'SCAD Forge 0.7 · Controls & limits',
            'Vim: h j k l, w b e, f F t T ; comma, 0 $, gg G, counts, i a I A o O, v, x, dd yy cc, dw cw yw, p, u, Ctrl+R, Ctrl+D/U.\n'
            'Commands: :w :q :wq :q! :42 :/text. Esc returns to NORMAL. Disable Vim for conventional editing.\n\n'
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
