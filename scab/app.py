import os
import sys
import re
import threading
from pathlib import Path
import numpy as np
from PySide6.QtCore import Qt, QTimer, QSettings, Signal, QSize, QFileSystemWatcher
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QTextCursor, QColor, QSurfaceFormat, QBrush, QPalette, QFont, QVector3D
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem, QPlainTextEdit,
    QFileDialog, QMessageBox, QInputDialog, QColorDialog, QCheckBox, QComboBox,
    QDoubleSpinBox, QSpinBox, QSlider, QDialog, QFormLayout, QDialogButtonBox,
    QGroupBox, QAbstractItemView, QProgressBar, QTabWidget, QMenuBar, QToolBar, QMenu)
from .model import Document, SourceError
from .editor import Editor, Diagnostic
from .diagnostics import parse_log, source_problems, PathMapper, count_text
from .problems import ProblemsPanel
from .clearance_panel import ClearancePanel
from .boards import BoardsPanel, BoardEntry, read_pose, write_pose, add_link, remove_link, pose_from_matrix
from .inspector_panel import InspectorPanel
from .inspector import Explainer, apply_edit
from .trace import trace as trace_design
from . import __version__
from .icons import icon
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
QMainWindow, QDialog { background:#1e1f22; color:#cfd1d4; }
QWidget { color:#cfd1d4; font-size:12px; }
QMenuBar { background:#2b2d30; border:0; padding:0; }
QMenuBar::item { padding:6px 9px; background:transparent; }
QMenuBar::item:selected { background:#3c3f44; }
QMenu { background:#2b2d30; border:1px solid #45484d; padding:2px 0; }
QMenu::item { padding:4px 26px 4px 26px; }
QMenu::item:selected { background:#2f5fbf; color:#ffffff; }
QMenu::item:disabled { color:#6b6e75; }
QMenu::separator { height:1px; background:#45484d; margin:3px 0; }
QMenu::icon { padding-left:6px; }
QWidget#header { background:#2b2d30; border-bottom:1px solid #1a1b1e; }
QToolBar { background:#2b2d30; border:0; spacing:0; padding:1px 2px; }
QToolBar::separator { background:#45484d; width:1px; margin:5px 4px; }
QToolButton { background:transparent; border:1px solid transparent; border-radius:0; padding:3px; }
QToolButton:hover { background:#3c3f44; border-color:#4e5157; }
QToolButton:checked { background:#2d4270; border-color:#3d5a96; }
QToolButton:pressed { background:#24262a; }
QPushButton { background:#393b40; border:1px solid #4e5157; border-radius:0; padding:3px 10px; }
QPushButton:hover { background:#43464b; }
QPushButton:pressed { background:#2b2d30; }
QPushButton:disabled { color:#6b6e75; }
QLineEdit, QTextEdit, QListView, QTableView, QTreeWidget { background:#1e1f22; border:1px solid #393b40; border-radius:0; selection-background-color:#2f5fbf; selection-color:#ffffff; }
QPlainTextEdit { background:#1e1f22; border:0; selection-background-color:#214283; }
QLineEdit:focus { border-color:#3574f0; }
QComboBox, QSpinBox, QDoubleSpinBox { background:#2b2d30; border:1px solid #4e5157; border-radius:0; padding:2px 4px; }
QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color:#6b6e75; }
QComboBox QAbstractItemView { background:#2b2d30; border:1px solid #4e5157; selection-background-color:#2f5fbf; selection-color:#ffffff; }
QAbstractItemView { background:#1e1f22; color:#cfd1d4; }
QTabWidget::pane { border:0; border-top:1px solid #393b40; }
QTabBar::tab { background:#2b2d30; color:#9a9da3; padding:5px 12px; border:0; border-right:1px solid #1e1f22; }
QTabBar::tab:selected { background:#1e1f22; color:#e6e7e9; border-top:2px solid #3574f0; }
QTabBar::tab:hover:!selected { color:#e6e7e9; }
QHeaderView::section { background:#2b2d30; color:#9a9da3; border:0; border-right:1px solid #393b40; border-bottom:1px solid #393b40; padding:3px 6px; }
QTreeWidget::item { padding:2px 0; }
QTreeWidget::item:selected { background:#2d4270; color:#ffffff; }
QGroupBox { border:1px solid #393b40; border-radius:0; margin-top:6px; padding-top:4px; }
QStatusBar { background:#2b2d30; color:#9a9da3; border-top:1px solid #1a1b1e; }
QStatusBar::item { border:0; }
QProgressBar { background:#1e1f22; border:1px solid #393b40; border-radius:0; text-align:center; color:#cfd1d4; }
QProgressBar::chunk { background:#2f5fbf; }
QSplitter::handle { background:#1a1b1e; }
QSplitter::handle:horizontal { width:1px; }
QSplitter::handle:vertical { height:1px; }
QScrollBar:vertical { background:#1e1f22; width:10px; margin:0; }
QScrollBar:horizontal { background:#1e1f22; height:10px; margin:0; }
QScrollBar::handle { background:#45484d; min-height:24px; min-width:24px; }
QScrollBar::handle:hover { background:#5a5d63; }
QScrollBar::add-line, QScrollBar::sub-line { width:0; height:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }
QSlider::groove:horizontal { height:2px; background:#4e5157; }
QSlider::handle:horizontal { background:#cfd1d4; width:8px; margin:-6px 0; border-radius:0; }
QToolTip { background:#2b2d30; color:#e6e7e9; border:1px solid #4e5157; padding:3px; }
QLabel#secondary { color:#8c8f95; padding:2px 6px; }
QLabel#panelTitle { color:#9a9da3; padding:4px 6px; background:#2b2d30; border-bottom:1px solid #393b40; }
'''


def tab_close_style():
    """Quiet tab close buttons (Qt's default is a bright orange cross)."""
    import tempfile
    folder=Path(tempfile.gettempdir())/'scab-ui';folder.mkdir(exist_ok=True)
    for name,color in (('close','#8c8f95'),('close-hover','#e6e7e9')):
        (folder/f'{name}.svg').write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12" width="12" height="12">'
                                          f'<path d="M3 3l6 6M9 3l-6 6" stroke="{color}" stroke-width="1.4"/></svg>')
    url=folder.as_posix()
    return (f'\nQTabBar::close-button {{ image:url({url}/close.svg); subcontrol-position:right; margin:2px; }}'
            f'\nQTabBar::close-button:hover {{ image:url({url}/close-hover.svg); background:#43464b; }}\n')


def swatch(colors):
    """Small square color chip (one color, or up to four side by side)."""
    from PySide6.QtGui import QPixmap,QPainter,QIcon
    colors=colors if isinstance(colors,list) else [colors]
    pm=QPixmap(12,12);pm.fill(QColor('#1e1f22'));p=QPainter(pm)
    width=12/len(colors)
    for i,c in enumerate(colors):p.fillRect(int(i*width),0,int(width+.99),12,c)
    p.setPen(QColor('#5a5d63'));p.drawRect(0,0,11,11);p.end()
    return QIcon(pm)


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
    partsExported=Signal(object,object)
    exportProgress=Signal(str)
    boardLoaded=Signal(str,object,object)

    def __init__(self, path=None):
        super().__init__()
        self.settings = QSettings('SCAB','SCAB')
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
        self.setWindowTitle(f'SCAB {__version__}')
        self.resize(1550,950)
        self.editor = Editor()
        self.viewer = Viewer()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Object','Color'])
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
        self.clearance_panel = ClearancePanel()
        self.clearance_panel.checkRequested.connect(self.run_clearance)
        self.clearance_panel.partSelected.connect(self.select_board_part)
        self.boards = {}
        self.selected_board = None
        self.boards_panel = BoardsPanel()
        self.boards_panel.addRequested.connect(self.load_kicad)
        self.boards_panel.removeRequested.connect(self.remove_board)
        self.boards_panel.reloadRequested.connect(lambda ident: self.reload_board(ident))
        self.boards_panel.flipRequested.connect(self.flip_board)
        self.boards_panel.resetRequested.connect(lambda ident: self.set_board_pose(ident,[0.,0.,0.],[0.,0.,0.]))
        self.boards_panel.focusRequested.connect(self.focus_board)
        self.boards_panel.poseEdited.connect(self.set_board_pose)
        self.boards_panel.visibilityChanged.connect(self.board_visibility)
        self.boards_panel.boardSelected.connect(self.select_board)
        self.boards_panel.kicadRequested.connect(lambda ident:self.open_board_in_kicad(ident))
        self.board_watcher = QFileSystemWatcher(self)
        self.board_watcher.fileChanged.connect(self.board_file_changed)
        self.board_timer = QTimer(self); self.board_timer.setSingleShot(True); self.board_timer.timeout.connect(self.reload_kicad)
        self.source_watcher = QFileSystemWatcher(self)
        self.source_watcher.fileChanged.connect(self.source_file_changed)
        self.boardLoaded.connect(self.board_loaded, Qt.QueuedConnection)
        self.partsExported.connect(self.parts_exported, Qt.QueuedConnection)
        self.exportProgress.connect(self.info, Qt.QueuedConnection)
        self.viewer.measured.connect(lambda text: self.info(text))
        self.viewer.boardPicked.connect(self.board_picked)
        self.viewer.boardTransformed.connect(lambda ident,matrix:self.set_board_pose(ident,*pose_from_matrix(matrix)))
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

    def make_action(self,name,text,fn=None,shortcut='',checkable=False,tip=''):
        action=QAction(icon(name),text,self)
        if fn is not None:action.triggered.connect(fn)
        if shortcut:action.setShortcut(QKeySequence(shortcut));action.setShortcutContext(Qt.WindowShortcut)
        action.setCheckable(checkable)
        action.setToolTip(text+(f'  ({shortcut})' if shortcut else '')+(f'\n{tip}' if tip else ''))
        action.setStatusTip(tip or text)
        self.actions[name]=action
        return action

    def build_ui(self):
        self.actions={}
        A=self.make_action
        # State widgets kept off-screen; the header actions drive them.
        self.draft_mesh=QCheckBox();self.draft_mesh.setChecked(os.environ.get('SCAB_DRAFT','0')=='1')
        self.draft_mesh.toggled.connect(lambda *_:self.compile_timer.start(100))
        self.auto=QCheckBox();self.auto.setChecked(True)
        self.vim_check=QCheckBox();self.vim_check.setChecked(self.vim_enabled);self.vim_check.toggled.connect(self.set_vim)
        self.editor.toggle_vim(self.vim_enabled)
        self.object_tool=QComboBox();self.object_tool.addItems(['Dimensions','Move','Rotate','Inspect']);self.object_tool.setCurrentText('Inspect')
        self.object_tool.currentTextChanged.connect(self.refresh_handles)
        self.roll_control=QDoubleSpinBox();self.roll_control.setRange(-180,180);self.roll_control.valueChanged.connect(self.viewer.set_roll)
        self.viewer.rollChanged.connect(self.sync_roll)
        self.dimensions_check=QCheckBox();self.dimensions_check.setChecked(True);self.dimensions_check.toggled.connect(self.refresh_handles)

        A('new','New file',self.new_file,'Ctrl+N');A('open','Open…',self.open_file,'Ctrl+O');A('save','Save',self.save,'Ctrl+S')
        A('undo','Undo',lambda:self.editor.undo(),'Ctrl+Z');A('redo','Redo',lambda:self.editor.redo(),'Ctrl+Shift+Z')
        A('build','Build',self.compile,'F5',tip='Compile the model with OpenSCAD');A('stop','Cancel build',self.cancel_jobs,'Shift+F5')
        A('export','Export STL…',self.export_stl,'F6')
        A('export_parts','Export chassis parts (one STL per object)…',self.export_parts,'Shift+F6',
          tip='Every top-level object to its own STL; the KiCad board and %/* objects are left out')
        A('draft','Draft mesh',checkable=True,tip='Cap $fn at 24 for the preview only').toggled.connect(self.draft_mesh.setChecked)
        self.actions['draft'].setChecked(self.draft_mesh.isChecked())
        A('auto','Build while typing',checkable=True).toggled.connect(self.auto.setChecked);self.actions['auto'].setChecked(True)
        A('vim','Vim mode',checkable=True).toggled.connect(self.vim_check.setChecked);self.actions['vim'].setChecked(self.vim_enabled)
        tools=QActionGroup(self);tools.setExclusive(True)
        for name,text,mode,key,tip in (('inspect','Inspect','Inspect','I','Click the model to see and edit the parameters behind it'),
                                       ('dimensions','Dimension handles','Dimensions','D','Drag sizes of the selected object'),
                                       ('move','Move','Move','G','Drag the selected object along an axis'),
                                       ('rotate','Rotate','Rotate','R','Rotate the selected object about an axis')):
            action=A(name,text,checkable=True,tip=tip);tools.addAction(action)
            action.triggered.connect(lambda _=False,m=mode:self.object_tool.setCurrentText(m))
        self.actions['inspect'].setChecked(True)
        A('color','Color…',self.color,tip='Set the color of the selected object')
        A('duplicate','Duplicate…',self.duplicate,'Ctrl+D')
        A('pattern','Pattern…',self.pattern,tip='Linear, rectangular, circular or helix pattern')
        A('boolean','Boolean…',self.boolean,tip='Combine objects selected in the tree (Ctrl+click)')
        A('thread','Metric thread…',self.add_thread)
        A('animate','Animate variable…',self.bind_animation)
        A('measure','Measure',checkable=True,tip='Click two points on the model').toggled.connect(self.set_measure)
        A('section','Section plane',checkable=True).toggled.connect(self.toggle_section)
        A('kicad_import','Add KiCad board…',self.load_kicad,'Ctrl+Shift+L',tip='Add a .kicad_pcb from any project; it updates live when KiCad saves. Several boards can be added and moved.')
        A('export_step','Export STEP (model + all boards)…',self.export_step,'Ctrl+Shift+E',tip='One .step with every SCAD object and every visible KiCad board with its 3D parts')
        A('kicad_export','Export KiCad board…',self.export_kicad,'Ctrl+Shift+K')
        A('clearance','Check PCB clearance',self.run_clearance,'Ctrl+Shift+C',tip='Parts of the loaded KiCad board that hit or come close to the model')
        A('kicad','Insert PCB library',self.insert_pcb_template)
        A('open_kicad','Open board in KiCad',lambda:self.open_board_in_kicad(),tip='Open the selected board in the KiCad PCB editor; saving there updates it here')
        A('open_kicad_project','Open KiCad project',lambda:self.open_board_in_kicad(project=True),tip='Open the selected board\'s KiCad project (schematic, PCB, libraries)')
        A('fit','Fit view',self.viewer.fit,'F');A('view_iso','Isometric',lambda:self.set_view(28,-48),'0')
        A('view_top','Top',lambda:self.set_view(90,-90),'7');A('view_front','Front',lambda:self.set_view(0,-90),'1')
        A('view_right','Right',lambda:self.set_view(0,0),'3')
        A('camera','Save view as PNG…',self.save_screenshot)
        A('settings','OpenSCAD path…',self.configure_compiler);A('help','Controls',self.help,'F1')
        A('reload','Reload KiCad board',self.reload_kicad)
        A('go_source','Go to source',self.goto_selected_source)
        A('find','Find…',lambda:self.find_bar.open(False),'Ctrl+F',tip='Find in the current file (Vim normal mode: use /)')
        A('replace','Find and replace…',lambda:self.find_bar.open(True),'Ctrl+H')
        self.actions['find_next']=QAction(icon('redo'),'Find next',self);self.actions['find_next'].setShortcut(QKeySequence('F3'))
        self.actions['find_next'].triggered.connect(lambda:self.find_next(False))
        self.actions['find_previous']=QAction(icon('undo'),'Find previous',self);self.actions['find_previous'].setShortcut(QKeySequence('Shift+F3'))
        self.actions['find_previous'].triggered.connect(lambda:self.find_next(True))
        self.actions['goto_line']=QAction('Go to line…',self);self.actions['goto_line'].setShortcut(QKeySequence('Ctrl+G'))
        self.actions['goto_line'].triggered.connect(self.goto_line_dialog)
        self.actions['comment']=QAction('Toggle comment',self);self.actions['comment'].setShortcut(QKeySequence('Ctrl+/'))
        self.actions['comment'].triggered.connect(lambda:self.editor.toggle_comment_lines())
        for name in ('fit','view_iso','view_top','view_front','view_right','inspect','dimensions','move','rotate'):
            self.actions[name].setShortcutContext(Qt.WidgetWithChildrenShortcut);self.viewer.addAction(self.actions[name])

        # ---- one header row: menus, then tools
        header=QWidget();header.setObjectName('header')
        row=QHBoxLayout(header);row.setContentsMargins(0,0,4,0);row.setSpacing(0)
        menus=QMenuBar();row.addWidget(menus)
        def menu(title,names):
            m=menus.addMenu(title)
            for name in names:
                if name=='-':m.addSeparator()
                else:m.addAction(self.actions[name])
            return m
        self.file_menu=menu('File',['new','open','save','-','export','export_parts','export_step','camera','-','settings'])
        self.recent_menu=self.file_menu.addMenu('Open recent');self.refresh_recent_menu()
        menu('Edit',['undo','redo','-','find','replace','find_next','find_previous','goto_line','comment','-','vim','auto'])
        menu('View',['fit','view_iso','view_top','view_front','view_right','-','draft','section','-','camera'])
        menu('Model',['inspect','dimensions','move','rotate','-','color','duplicate','pattern','boolean','thread','animate','-','measure','build','stop'])
        menu('PCB',['kicad_import','open_kicad','open_kicad_project','reload','clearance','-','export_parts','export_step','-','kicad','kicad_export'])
        self.project_menu_bar=menus
        self.help_menu=menu('Help',['help'])
        bar=QToolBar();bar.setIconSize(QSize(18,18));bar.setToolButtonStyle(Qt.ToolButtonIconOnly)
        for name in ['new','open','save','|','undo','redo','|','build','stop','export','|','inspect','dimensions','move','rotate','|',
                     'color','duplicate','pattern','boolean','thread','|','measure','section','|','kicad_import','clearance','export_step','kicad_export',
                     '|','fit','view_iso','view_top','view_front','view_right']:
            if name=='|':bar.addSeparator()
            else:bar.addAction(self.actions[name])
        row.addWidget(bar,1)
        toggles=QToolBar();toggles.setIconSize(QSize(18,18))
        for name in ('draft','auto','vim'):toggles.addAction(self.actions[name])
        row.addWidget(toggles)
        self.setMenuWidget(header)

        # ---- left: objects + project
        left=QWidget(); ll=QVBoxLayout(left); ll.setContentsMargins(0,0,0,0); ll.setSpacing(0)
        objects_title=QLabel('Objects');objects_title.setObjectName('panelTitle');ll.addWidget(objects_title)
        self.file_label=QLabel('');self.file_label.hide()
        ll.addWidget(self.tree,2)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(lambda pos:self.model_menu(self.tree.viewport().mapToGlobal(pos)))
        middle=QWidget(); ml=QVBoxLayout(middle); ml.setContentsMargins(0,0,0,0); ml.setSpacing(0)
        self.setup_documents(ll,ml)
        self.hint.setObjectName('secondary');ml.addWidget(self.hint)
        # ---- right: viewer over inspector / problems / output
        right=QWidget(); rl=QVBoxLayout(right); rl.setContentsMargins(0,0,0,0); rl.setSpacing(0)
        self.right_split=QSplitter(Qt.Vertical)
        viewer_box=QWidget(); viewer_layout=QVBoxLayout(viewer_box); viewer_layout.setContentsMargins(0,0,0,0); viewer_layout.setSpacing(0)
        viewer_layout.addWidget(self.viewer,1)
        self.viewer.setToolTip('')
        self.viewer.contextRequested.connect(self.viewer_context)
        self.right_split.addWidget(viewer_box)
        self.bottom_tabs=QTabWidget();self.bottom_tabs.setDocumentMode(True)
        self.bottom_tabs.addTab(self.inspector,'Inspector')
        self.bottom_tabs.addTab(self.problems_panel,'Problems')
        self.bottom_tabs.addTab(self.boards_panel,'Boards')
        self.bottom_tabs.addTab(self.clearance_panel,'Clearance')
        self.bottom_tabs.addTab(self.console,'Output')
        rl.addWidget(self.right_split,1)
        section=QWidget(); section_layout=QVBoxLayout(section); section_layout.setContentsMargins(6,4,6,4)
        sl=QHBoxLayout(); section_layout.addLayout(sl)
        self.section_check=QCheckBox('Show plane'); self.section_check.toggled.connect(self.section_changed); sl.addWidget(self.section_check)
        self.section_check.toggled.connect(lambda on:self.actions['section'].setChecked(on))
        self.clip_check=QCheckBox('Clip'); self.clip_check.setChecked(True); self.clip_check.toggled.connect(self.section_changed); sl.addWidget(self.clip_check)
        self.axis=QComboBox(); self.axis.addItems(['X','Y','Z']); self.axis.setCurrentText('Z'); self.axis.currentTextChanged.connect(self.section_changed); sl.addWidget(self.axis)
        self.section_slider=QSlider(Qt.Horizontal); self.section_slider.setRange(-1000,1000); self.section_slider.valueChanged.connect(self.section_slide); sl.addWidget(self.section_slider,1)
        self.section_value=QDoubleSpinBox(); self.section_value.setRange(-1e6,1e6); self.section_value.setDecimals(3); self.section_value.setSuffix(' mm'); self.section_value.setToolTip('Distance from the origin along the plane normal'); self.section_value.valueChanged.connect(self.section_number); sl.addWidget(self.section_value)
        self.flip=QCheckBox('Flip'); self.flip.toggled.connect(self.section_changed); sl.addWidget(self.flip)
        lower=QHBoxLayout(); section_layout.addLayout(lower)
        self.plane_angles=[]
        for label in ('Tilt X','Tilt Y','Tilt Z'):
            angle=QDoubleSpinBox(); angle.setRange(-180,180); angle.setSuffix('°'); angle.setPrefix(label+' '); angle.valueChanged.connect(self.section_changed)
            self.plane_angles.append(angle); lower.addWidget(angle)
        button('Center',self.add_plane,lower)
        button('Write cut to code',self.commit_section,lower)
        button('Export DXF…',self.export_section,lower)
        self.section_panel=Collapsible('Section plane',section,collapsed=True)
        viewer_layout.addWidget(self.section_panel)
        animation=QWidget(); al=QHBoxLayout(animation); al.setContentsMargins(6,4,6,4)
        self.play_button=button('Play',self.play,al)
        self.timeline=QSlider(Qt.Horizontal); self.timeline.setRange(0,1000); self.timeline.valueChanged.connect(self.scrub); al.addWidget(self.timeline,1)
        self.time_label=QLabel('0.000'); al.addWidget(self.time_label)
        self.frame_count=QSpinBox(); self.frame_count.setRange(2,120); self.frame_count.setValue(24); self.frame_count.setSuffix(' frames'); al.addWidget(self.frame_count)
        self.fps=QSpinBox(); self.fps.setRange(1,60); self.fps.setValue(12); self.fps.setSuffix(' fps'); al.addWidget(self.fps)
        button('Bake',self.bake,al); button('Cancel',self.cancel_jobs,al)
        self.animation_status=QLabel('');self.animation_status.setObjectName('secondary')
        self.animation_status.setWordWrap(True)
        animation_container=QWidget();animation_layout=QVBoxLayout(animation_container);animation_layout.setContentsMargins(0,0,0,0);animation_layout.setSpacing(0)
        animation_layout.addWidget(animation);animation_layout.addWidget(self.animation_status)
        self.animation_panel=Collapsible('Animation ($t)',animation_container,collapsed=True)
        viewer_layout.addWidget(self.animation_panel)
        self.right_split.addWidget(self.bottom_tabs)
        self.right_split.setSizes([560,380])
        self.fps.valueChanged.connect(lambda value:self.play_timer.setInterval(round(1000/value)))
        self.frame_count.valueChanged.connect(self.animation_count_changed)
        split=QSplitter(); split.addWidget(left); split.addWidget(middle); split.addWidget(right)
        split.setSizes([230,540,830]); split.setStretchFactor(0,0); split.setStretchFactor(1,1); split.setStretchFactor(2,2)
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
        if hasattr(self,'view_status'):self.view_status.setText('Edited · previous model shown until the build finishes')
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
            item=QTreeWidgetItem([label,color.name()]);item.setIcon(1,swatch(color))
            item.setForeground(1,QBrush(QColor('#8c8f95')));item.setData(0,Qt.UserRole,node)
            parent.addChild(item) if parent else self.tree.addTopLevelItem(item)
            if node.start==selected_start:
                item.setSelected(True); self.node=node
            for child in node.children:
                if child.kind=='geometry': add(child,item,color)
        for node in self.doc.roots: add(node,None)
        self.tree.expandToDepth(1); self.tree.blockSignals(False)
        self.refresh_material_swatches()
        if hasattr(self,'project_timer'):self.project_timer.start(200)
        if hasattr(self,'boards_panel'):self.sync_boards()
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
                item.setText(1,f'{len(palette)} colors');item.setIcon(1,swatch([QColor.fromRgbF(*c) for c in palette[:4]]))
                item.setToolTip(1,'Evaluated module materials: '+', '.join(QColor.fromRgbF(*c).name() for c in palette)+'. Double-click to override the entire object.')
            elif palette:
                color=QColor.fromRgbF(*palette[0]);item.setText(1,color.name());item.setIcon(1,swatch(color))

    def tree_selected(self):
        self.selected_board=None
        self.viewer.select_board(None)
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
        if root is None and getattr(self,'selected_board',None) in getattr(self,'boards',{}) and mode in ('Move','Rotate'):
            self.viewer.set_handles([]);self.viewer.set_board_gizmo(mode,self.selected_board);return
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
        self.view_status.setText('Preview · temporary values, source unchanged until Apply')
        self.console.setPlainText(logs);self.info('Preview · source unchanged until Apply')

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

    # ------------------------------------------------------------- KiCad live
    def load_kicad(self):
        """Add a KiCad board (any project) to this design."""
        last=next(iter(reversed(self.boards.values())),None)
        start=str(last.path.parent if last else (self.path.parent if self.path else Path.cwd()))
        name,_=QFileDialog.getOpenFileName(self,'Add KiCad board',start,'KiCad board (*.kicad_pcb);;KiCad project (*.kicad_pro)')
        if not name:return
        path=Path(name).resolve()
        if path.suffix=='.kicad_pro':path=path.with_suffix('.kicad_pcb')
        from .kicad_import import board_id,include_names
        if not self.path:
            self.info('Save this file first: the board link, pose and include are stored in it');
            if not self.save():return
        taken=set(self.boards)
        ident='kicad' if not taken else board_id(path.parent.name if path.stem=='board' else path.stem,taken)
        default=ident
        text,ok=QInputDialog.getText(self,'Board name','Name used in OpenSCAD (letters, digits, _):',text=default)
        if not ok:return
        ident=board_id(text,taken) if text.strip() else default
        relative=os.path.relpath(path,self.path.parent).replace(os.sep,'/')
        include,_=include_names(ident,path.stem)
        source=add_link(self.editor.toPlainText(),ident,relative,include)
        if ident!='kicad' and self.boards:
            # place the new board next to the others instead of on top of them
            right=max((self.viewer.board_bounds(i)[1][0] for i in self.boards if self.viewer.board_bounds(i) is not None),default=0.)
            source=write_pose(source,ident,[round(right+10.,1),0.,0.],[0.,0.,0.])
        self.replace_source(source)
        self.sync_boards()
        self.info(f'Board "{ident}" added · move it in the Boards tab or with Move (G) · include <kicad/{include}>')

    def open_board(self,path,ident='kicad'):
        entry=self.boards.get(ident)
        if entry is None:
            entry=self.boards[ident]=BoardEntry(ident,Path(path).resolve())
        entry.path=Path(path).resolve()
        if str(entry.path) not in self.board_watcher.files():self.board_watcher.addPath(str(entry.path))
        self.reload_board(ident)

    def board_file_changed(self,name):
        # KiCad saves by replacing the file: watch the new inode as well.
        if Path(name).exists() and name not in self.board_watcher.files():self.board_watcher.addPath(name)
        for entry in self.boards.values():
            if str(entry.path)==name or (entry.model is not None and name in {str(f) for f in getattr(entry.model,'files',())}):
                timer=getattr(entry,'timer',None)
                if timer is None:
                    timer=entry.timer=QTimer(self);timer.setSingleShot(True);timer.timeout.connect(lambda i=entry.ident:self.reload_board(i))
                timer.start(250)

    def reload_kicad(self):
        if not self.boards:
            self.info('Add a KiCad board first (PCB → Add KiCad board…)');return
        for ident in list(self.boards):self.reload_board(ident)

    def reload_board(self,ident):
        entry=self.boards.get(ident)
        if entry is None:return
        if entry.loading:
            entry.reload_again=True;return
        entry.loading=True;entry.error=''
        path=entry.path;executable=self.executable
        cache=(self.project_root or (self.path.parent if self.path else path.parent))/'.scab-cache'
        self.boards_panel.set_boards(list(self.boards.values()))
        def work():
            from .kicad_import import load_board
            # 1) fast: parse + 3D parts -> include and SCAD rebuild right after the save
            try:quick=load_board(path,executable,cache,meshes=False)
            except (OSError,ValueError,RuntimeError) as exc:
                self.boardLoaded.emit(ident,None,str(exc));return
            self.boardLoaded.emit(ident,quick,None)
            # 2) complete: OpenSCAD meshes for the board body, pads and silkscreen
            try:result,error=load_board(path,executable,cache),None
            except (OSError,ValueError,RuntimeError) as exc:result,error=None,str(exc)
            self.boardLoaded.emit(ident,result,error)
        threading.Thread(target=work,daemon=True,name='kicad').start()

    def board_loaded(self,ident,model,error):
        entry=self.boards.get(ident)
        if entry is None:return
        partial=model is not None and getattr(model,'partial',False)
        if partial and entry.model is not None:
            # keep showing the previous board body until the new meshes arrive
            model.body,model.pads,model.silk=entry.model.body,entry.model.pads,entry.model.silk
        elif partial:
            model.notes=[n for n in model.notes]
        entry.loading=partial
        if not partial and getattr(entry,'reload_again',False):
            entry.reload_again=False;self.reload_board(ident);return
        if model is None:
            entry.error=f'failed: {error}';self.boards_panel.set_boards(list(self.boards.values()))
            self.error(f'KiCad board {ident}: {error}');return
        first=not any(b.model for b in self.boards.values())
        entry.model=model
        for file in getattr(model,'files',()):
            if Path(file).exists() and str(file) not in self.board_watcher.files():self.board_watcher.addPath(str(file))
        self.update_board_view()
        if first:self.viewer.fit()
        from .kicad_import import write_include,include_names
        target=(self.path.parent if self.path else model.board.path.parent)/'kicad'
        include=target/include_names(ident,model.board.path.stem)[0]
        before=include.read_text(encoding='utf-8') if include.exists() else None
        try:write_include(model,target,ident)
        except (OSError,ValueError) as exc:model.notes.append(f'Include not written: {exc}')
        changed=include.exists() and include.read_text(encoding='utf-8')!=before
        self.boards_panel.set_boards(list(self.boards.values()))
        if not partial:
            self.info(f'KiCad board {ident} loaded: {model.board.path.name}'+(' · '+' · '.join(model.notes) if model.notes else ''))
        if changed and f'kicad/{include.name}' in self.editor.toPlainText():self.compile()
        else:self.run_clearance()

    def update_board_view(self):
        boards=[]
        for entry in self.boards.values():
            if entry.model is None:continue
            boards.append((entry.ident,entry.model.reference_parts(),entry.matrix,entry.visible,entry.show_parts,entry.model.part_offset))
        self.viewer.set_boards(boards)
        if self.selected_board and self.object_tool.currentText() in ('Move','Rotate'):
            self.viewer.set_board_gizmo(self.object_tool.currentText(),self.selected_board)

    def sync_boards(self):
        """Match the boards to the `// scab: kicad` links and pose variables in the current file."""
        from .kicad_import import find_kicad_links
        source=self.editor.toPlainText()
        links=dict(find_kicad_links(source,self.path))
        for ident in [i for i in self.boards if i not in links]:
            entry=self.boards.pop(ident)
            if str(entry.path) in self.board_watcher.files() and not any(e.path==entry.path for e in self.boards.values()):
                self.board_watcher.removePath(str(entry.path))
        moved=False
        for ident,path in links.items():
            pose=read_pose(source,ident) or ([0.,0.,0.],[0.,0.,0.])
            entry=self.boards.get(ident)
            if entry is None or entry.path!=path:
                self.boards[ident]=BoardEntry(ident,path,pose=pose)
                self.open_board(path,ident)
            elif [list(entry.pose[0]),list(entry.pose[1])]!=[list(pose[0]),list(pose[1])]:
                entry.pose=pose;moved=True
                self.viewer.set_board_pose(ident,entry.matrix)
        if moved and self.selected_board in self.boards and self.object_tool.currentText() in ('Move','Rotate'):
            self.viewer.set_board_gizmo(self.object_tool.currentText(),self.selected_board)
        if links or self.viewer.reference_items:
            if not links:self.viewer.set_boards([])
        self.boards_panel.set_boards(list(self.boards.values()))
        if moved:self.clearance_timer_start()

    def clearance_timer_start(self):
        if not hasattr(self,'clearance_timer'):
            self.clearance_timer=QTimer(self);self.clearance_timer.setSingleShot(True);self.clearance_timer.timeout.connect(self.run_clearance)
        self.clearance_timer.start(250)

    def set_board_pose(self,ident,position,angles):
        """Write a board pose into the SCAD source (the source is the single source of truth)."""
        entry=self.boards.get(ident)
        if entry is None:return
        position=[round(float(v),4) for v in position];angles=[round(float(v),4) for v in angles]
        entry.pose=(position,angles)
        self.viewer.set_board_pose(ident,entry.matrix)
        source=self.editor.toPlainText()
        updated=write_pose(source,ident,position,angles)
        if updated!=source:
            from PySide6.QtGui import QTextCursor as _Cursor
            prefix=0
            while prefix<min(len(source),len(updated)) and source[prefix]==updated[prefix]:prefix+=1
            suffix=0
            while suffix<min(len(source),len(updated))-prefix and source[-1-suffix]==updated[-1-suffix]:suffix+=1
            cursor=_Cursor(self.editor.document());cursor.beginEditBlock()
            cursor.setPosition(self.editor.buffer.to_qt(prefix));cursor.setPosition(self.editor.buffer.to_qt(len(source)-suffix),_Cursor.KeepAnchor)
            cursor.insertText(updated[prefix:len(updated)-suffix]);cursor.endEditBlock()
        self.boards_panel.set_boards(list(self.boards.values()))
        if self.selected_board==ident and self.object_tool.currentText() in ('Move','Rotate'):
            self.viewer.set_board_gizmo(self.object_tool.currentText(),ident)
        self.clearance_timer_start()

    def flip_board(self,ident):
        entry=self.boards.get(ident)
        if entry is None:return
        from .boards import pose_matrix
        bounds=self.viewer.board_bounds(ident)
        center=(bounds[0]+bounds[1])/2 if bounds is not None else np.zeros(3)
        flip=np.eye(4);flip[:3,:3]=np.diag([1.,-1.,-1.]);flip[:3,3]=center-flip[:3,:3]@center
        self.set_board_pose(ident,*pose_from_matrix(flip@entry.matrix))

    def remove_board(self,ident):
        if ident not in self.boards:return
        if QMessageBox.question(self,'Remove board',f'Unlink board "{ident}" from this file? (The KiCad project is not touched.)')!=QMessageBox.Yes:return
        self.replace_source(remove_link(self.editor.toPlainText(),ident))
        self.sync_boards();self.update_board_view();self.run_clearance()

    def board_visibility(self,ident,visible,parts):
        entry=self.boards.get(ident)
        if entry is None:return
        entry.visible,entry.show_parts=visible,parts
        self.update_board_view()

    def select_board(self,ident):
        self.selected_board=ident
        self.viewer.select_board(ident)
        mode=self.object_tool.currentText()
        if ident and mode in ('Move','Rotate'):self.viewer.set_board_gizmo(mode,ident)

    def focus_board(self,ident):
        bounds=self.viewer.board_bounds(ident)
        if bounds is None:return
        low,high=bounds
        self.viewer.opts['center']=QVector3D(*map(float,(low+high)/2))
        self.viewer.setCameraPosition(distance=max(10.,float(np.linalg.norm(high-low))*1.8));self.viewer.update()

    def board_picked(self,ident,index,point):
        entry=self.boards.get(ident)
        if entry is None or entry.model is None:return
        self.selected_board=ident;self.boards_panel.select(ident);self.viewer.select_board(ident)
        self.tree.blockSignals(True);self.tree.clearSelection();self.tree.blockSignals(False);self.node=None
        model=entry.model
        if index>=model.part_offset:
            part=model.parts[index-model.part_offset];fp=part.footprint
            self.info(f'{ident} · {part.ref} · {fp.value} · {fp.lib} · {fp.side}'+('' if part.has_model else ' · no 3D model'))
        else:
            self.info(f'Board {ident} · {model.board.path.name} · {len(model.parts)} parts · Move (G) / Rotate (R) to place it')
        mode=self.object_tool.currentText()
        if mode in ('Move','Rotate'):self.viewer.set_board_gizmo(mode,ident)

    def model_meshes_for_clearance(self):
        source=self.compiled_source or ''
        meshes=[]
        for chunk in self.viewer.scene:
            if 'kicad_pcb(' in source[chunk[0]:chunk[1]] or '_pcb(' in source[chunk[0]:chunk[1]]:continue
            meshes.append((chunk[2],chunk[3]))
        return meshes

    def run_clearance(self):
        loaded=[e for e in self.boards.values() if e.model is not None and e.visible]
        if not loaded:
            self.clearance_panel.set_results([],self.clearance_panel.minimum.value());return
        from .clearance import check
        scad=self.model_meshes_for_clearance()
        world={e.ident:e.world_parts() for e in loaded}
        results=[]
        for entry in loaded:
            model=entry.model;offset=model.part_offset
            # headroom is measured away from the board: a flipped board turns its top side down
            upside_down=(entry.matrix[:3,:3]@np.array([0.,0.,1.]))[2]<0
            side=lambda p:('bottom' if p.footprint.side=='top' else 'top') if upside_down else p.footprint.side
            parts=[(p.ref,world[entry.ident][offset+i][0],p.has_model,side(p)) for i,p in enumerate(model.parts)]
            # other boards (body and parts) are obstacles too: stacked boards must not touch
            others=[(v,f) for other,items in world.items() if other!=entry.ident for k,(v,f,_) in enumerate(items) if k!=1 and k!=2 and len(f)]
            for r in check(parts,scad,model.board.thickness,obstacles=others):
                lo,hi=model.parts[r.index].box     # height above its own board, in board coordinates
                r.height=float(hi[2]-model.board.thickness) if model.parts[r.index].footprint.side=='top' else float(-lo[2])
                results.append((entry.ident,r))
        minimum=self.clearance_panel.minimum.value()
        self.clearance_panel.set_results(results,minimum)
        colors={'collision':(0.9,0.2,0.22,1.),'tight':(0.95,0.68,0.2,1.)}
        groups={}
        for ident,r in results:
            status=r.status(minimum)
            if status in colors:
                entry=self.boards[ident];faces=len(entry.model.parts[r.index].faces)
                groups[(ident,entry.model.part_offset+r.index)]=[(np.ones(faces,dtype=bool),colors[status])]
        self.viewer.highlight_reference(groups)
        counts=self.clearance_panel.summary(minimum)
        index=self.bottom_tabs.indexOf(self.clearance_panel)
        bad=counts['collision']+counts['tight']
        self.bottom_tabs.setTabText(index,'Clearance'+(f' ({bad})' if bad else ''))
        if counts['collision']:
            self.info(f'Clearance: {counts["collision"]} part(s) collide, {counts["tight"]} tight · see the Clearance tab')

    def select_board_part(self,ident,index):
        entry=self.boards.get(ident)
        if entry is None or entry.model is None or index is None or index>=len(entry.model.parts):return
        part=entry.model.parts[index]
        v=entry.world_parts()[entry.model.part_offset+index][0]
        lo,hi=v.min(axis=0),v.max(axis=0)
        self.viewer.show_pick_box(lo,hi)
        self.viewer.opts['center']=QVector3D(*map(float,(lo+hi)/2));self.viewer.update()
        fp=part.footprint
        self.info(f'{ident} · {part.ref} · {fp.value} · {fp.lib} · {fp.side} · top at {hi[2]:.2f} mm' + ('' if part.has_model else ' · no 3D model (courtyard box)'))

    def export_parts(self):
        if not self.parse():return
        if not self.executable or not Path(self.executable).is_file():
            self.error('OpenSCAD was not found. Settings → OpenSCAD path…');return
        from .export_parts import chassis_parts
        parts=chassis_parts(self.doc)
        if not parts:
            self.info('Nothing to export: no top-level objects besides the KiCad board');return
        start=str((self.path.parent if self.path else Path.cwd())/'stl')
        folder=QFileDialog.getExistingDirectory(self,'Folder for the chassis STL files',start)
        if not folder:return
        prefix=(self.path.stem+'-') if self.path else ''
        source,directory,snapshot,target=self.render_context()
        doc=Document(self.editor.toPlainText())
        executable=self.executable;t=self.timeline.value()/1000
        names=', '.join(name for _,name in parts)
        self.info(f'Exporting {len(parts)} part(s): {names} …')
        def work():
            from .export_parts import export
            from .compiler import stage_project
            cleanup=lambda:None;where=directory
            try:
                if snapshot:where,_,cleanup=stage_project(snapshot,threading.Event())
                results=export(doc,where,executable,folder,prefix,t,
                               progress=lambda i,n,name:self.exportProgress.emit(f'Exporting {name} ({i+1}/{n}) …'))
                self.partsExported.emit(results,folder)
            finally:
                cleanup()
        threading.Thread(target=work,daemon=True,name='export-parts').start()

    def parts_exported(self,results,folder):
        ok=[name for name,path,_ in results if path]
        empty=[name for name,path,message in results if not path and message=='empty object']
        failed=[f'{name}: {message}' for name,path,message in results if not path and message!='empty object']
        lines=[f'Exported to {folder}:']+[f'  {path.name}' for _,path,_ in results if path]
        if empty:lines+=['Skipped (no geometry): '+', '.join(empty)]
        if failed:lines+=['Failed:']+['  '+f for f in failed]
        self.console.setPlainText('\n'.join(lines));self.bottom_tabs.setCurrentWidget(self.console)
        self.info(f'Exported {len(ok)} chassis part(s) to {folder}'+(f' · {len(empty)} empty skipped' if empty else '')+(f' · {len(failed)} failed' if failed else ''))

    def export_step(self):
        if self.compiled_source is None and not self.boards:
            self.info('Build the model first (F5)');return
        default=(self.path.with_suffix('.step') if self.path else Path('assembly.step'))
        name,_=QFileDialog.getSaveFileName(self,'Export STEP (model + boards)',str(default),'STEP (*.step *.stp)')
        if not name:return
        target=Path(name if name.lower().endswith(('.step','.stp')) else name+'.step')
        bodies=self.step_bodies()
        if not bodies:self.info('Nothing to export');return
        title=self.path.stem if self.path else 'SCAB assembly'
        self.info(f'Writing {target.name} ({sum(len(b[2]) for b in bodies):,} faces) …')
        def work():
            from .stepwrite import write_step
            try:
                count=write_step(target,bodies,title)
                self.exportProgress.emit(f'Exported {target} · {count} solids (SCAD model + {len([e for e in self.boards.values() if e.model and e.visible])} board(s))')
            except (OSError,ValueError) as error:
                self.exportProgress.emit(f'STEP export failed: {error}')
        threading.Thread(target=work,daemon=True,name='step').start()

    def step_bodies(self):
        """[(name, vertices, faces, rgba)]: SCAD objects split by color, each board body and each part."""
        bodies=[]
        source=self.compiled_source or ''
        from .export_parts import part_name
        try:roots={n.start:n for n in Document(source).roots}
        except SourceError:roots={}
        for index,chunk in enumerate(self.viewer.scene):
            text=source[chunk[0]:chunk[1]]
            if '_pcb(' in text:continue
            node=roots.get(chunk[0])
            label=part_name(source,node,index) if node is not None else f'scad-{index+1}'
            render=getattr(chunk,'render',None)
            if render is not None:
                vertices,faces,colors=render
                keys=np.round(colors,3)
                for color in np.unique(keys,axis=0):
                    mask=np.all(keys==color,axis=1)
                    bodies.append((f'{label}-{QColor.fromRgbF(*map(float,color[:3])).name()[1:]}',vertices,faces[mask],tuple(color)))
            else:
                bodies.append((label,chunk[2],chunk[3],tuple(getattr(chunk,'color',(.72,.74,.78,1.)))))
        for entry in self.boards.values():
            if entry.model is None or not entry.visible:continue
            world=entry.world_parts();model=entry.model
            v,f,c=world[0]
            if len(f)>1:bodies.append((f'{entry.ident}-board',v,f,tuple(c[0])))
            if entry.show_parts:
                for i,part in enumerate(model.parts):
                    v,f,c=world[model.part_offset+i]
                    # one solid per colour keeps the model colours in CAD tools
                    keys=np.round(c,3)
                    for color in np.unique(keys,axis=0):
                        mask=np.all(keys==color,axis=1)
                        bodies.append((f'{entry.ident}-{part.ref}',v,f[mask],tuple(color)))
        return bodies

    def check_kicad_link(self):
        self.sync_boards()

    def set_view(self,elevation,azimuth):
        self.viewer.set_roll(0.);self.viewer.setCameraPosition(elevation=elevation,azimuth=azimuth)

    def set_measure(self,on):
        self.viewer.set_measure(on)
        self.info('Measure: click two points on the model or the board' if on else 'Measure off')

    def toggle_section(self,on):
        if self.section_check.isChecked()!=on:self.section_check.setChecked(on)
        self.section_panel.set_expanded(on)
        if on:self.add_plane()

    def save_screenshot(self):
        default=(self.path.with_suffix('.png') if self.path else Path('view.png'))
        name,_=QFileDialog.getSaveFileName(self,'Save view as PNG',str(default),'PNG image (*.png)')
        if not name:return
        image=self.viewer.grabFramebuffer()
        if image.save(name if name.lower().endswith('.png') else name+'.png'):self.info(f'Saved {name}')
        else:self.error(f'Could not save {name}')

    def goto_selected_source(self):
        node=self.selected()
        if node:self.editor.select_source(node.start,node.end);self.editor.setFocus()

    def model_menu(self,global_pos):
        menu=QMenu(self)
        node=self.node
        if node is not None:
            title=menu.addAction(f'{node.name}  ·  line {self.doc.source.count(chr(10),0,node.start)+1}' if self.doc else node.name)
            title.setEnabled(False);menu.addSeparator()
        for name in ('go_source','-','color','duplicate','pattern','boolean','thread','animate','-','move','rotate','dimensions','-','measure','section'):
            if name=='-':menu.addSeparator()
            else:menu.addAction(self.actions[name])
        menu.exec(global_pos)

    def viewer_context(self,global_pos,pos):
        found=self.viewer.hit(pos)
        if found is not None and found[0]=='reference':
            ident,index=found[1]
            self.board_picked(ident,index,found[2])
            self.board_menu(global_pos,ident,index)
            return
        self.viewer.pick(pos)
        self.model_menu(global_pos)

    def board_menu(self,global_pos,ident,index=None):
        entry=self.boards.get(ident)
        if entry is None:return
        menu=QMenu(self)
        title=f'Board {ident} · {entry.path.name}'
        if entry.model is not None and index is not None and index>=entry.model.part_offset:
            part=entry.model.parts[index-entry.model.part_offset]
            title=f'{part.ref} · {part.footprint.value} · on {ident}'
        header=menu.addAction(title);header.setEnabled(False);menu.addSeparator()
        menu.addAction(icon('kicad'),'Open board in KiCad PCB editor',lambda:self.open_board_in_kicad(ident))
        menu.addAction(icon('open'),'Open KiCad project',lambda:self.open_board_in_kicad(ident,project=True))
        menu.addAction(icon('reload'),'Reload from disk',lambda:self.reload_board(ident))
        menu.addSeparator()
        menu.addAction(self.actions['move']);menu.addAction(self.actions['rotate'])
        menu.addAction(icon('rotate'),'Flip board',lambda:self.flip_board(ident))
        menu.addAction(icon('fit'),'Focus view on board',lambda:self.focus_board(ident))
        hide=menu.addAction('Hide board');hide.triggered.connect(lambda:self.board_visibility(ident,False,entry.show_parts) or self.boards_panel.rows[ident]['show'].setChecked(False))
        menu.exec(global_pos)

    def open_board_in_kicad(self,ident=None,project=False):
        ident=ident or self.selected_board or next(iter(self.boards),None)
        entry=self.boards.get(ident)
        if entry is None:
            self.info('Add a KiCad board first (PCB → Add KiCad board…)');return
        from .kicad_launch import open_in_kicad
        self.info(open_in_kicad(entry.path,project))

    # ---------------------------------------------------------- recent files
    def remember_recent(self,path):
        if path is None:return
        items=[p for p in (self.settings.value('recent',[]) or []) if p!=str(path)]
        self.settings.setValue('recent',[str(path)]+items[:9]);self.refresh_recent_menu()

    def refresh_recent_menu(self):
        if not hasattr(self,'recent_menu'):return
        self.recent_menu.clear()
        items=[p for p in (self.settings.value('recent',[]) or []) if Path(p).exists()]
        for item in items:
            action=self.recent_menu.addAction(item);action.triggered.connect(lambda _=False,p=item:self.load_file(Path(p)))
        self.recent_menu.setEnabled(bool(items))

    def watch_source(self):
        files=self.source_watcher.files()
        if files:self.source_watcher.removePaths(files)
        for editor,path in getattr(self,'buffers',{}).items():
            if path is not None and Path(path).exists():self.source_watcher.addPath(str(path))

    def source_file_changed(self,name):
        """Reload a file changed by another program (unless it has unsaved edits here)."""
        path=Path(name)
        if path.exists() and name not in self.source_watcher.files():self.source_watcher.addPath(name)
        for editor,existing in self.buffers.items():
            if existing is not None and self.same_file(existing,path):
                try:text=path.read_text(encoding='utf-8-sig')
                except (OSError,UnicodeError):return
                if text==editor.toPlainText():return
                if editor.document().isModified():
                    self.info(f'{path.name} changed on disk; you have unsaved edits here (use :e! to reload)');return
                from PySide6.QtGui import QTextCursor as _Cursor
                cursor=_Cursor(editor.document());cursor.beginEditBlock();cursor.select(_Cursor.Document);cursor.insertText(text);cursor.endEditBlock()
                editor.document().setModified(False);self.info(f'Reloaded {path.name} (changed on disk)')
                return

    # ------------------------------------------------------------------- KiCad
    def insert_pcb_template(self):
        if not self.path and not self.save():return
        import shutil
        library=Path(__file__).resolve().parent.parent/'assets'/'scab'/'pcb.scad'
        root=getattr(self,'project_root',None) or self.path.parent
        target=root/'lib'/'scab'/'pcb.scad'
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
            target.write_text(write_kicad_pcb(board,title=(self.path.stem if self.path else 'SCAB board')),encoding='utf-8')
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
        with tempfile.TemporaryDirectory(prefix='scab-kicad-') as tmp:
            scad=Path(tempfile.mkstemp(prefix='.scab-',suffix='.scad',dir=directory)[1])
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
        self.view_status.setText('Building '+(target.name if target else 'Untitled.scad')+' …')
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
            self.view_status.setText(f'Build failed · {where}{first.message}')
            self.info(f'Compilation failed · {where}{first.message} · click it in Problems or press :cn')
            self.bottom_tabs.setCurrentWidget(self.problems_panel)
        else:
            self.view_status.setText('Build failed · see Output')
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
        self.view_status.setText((target.name if target else 'Untitled.scad')+(' · built with warnings' if warning else ' · built')+(' · draft mesh' if self.draft_mesh.isChecked() else ''))
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
        if any(b.model for b in self.boards.values()):QTimer.singleShot(0,self.run_clearance)
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
        self.setWindowTitle(name+(' *' if self.editor.document().isModified() else '')+' — SCAB 0.7')
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
        if (path.parent/'scab-project.json').is_file() and self.project_root!=path.parent:
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
            with tempfile.NamedTemporaryFile('w',dir=target.parent,suffix='.scab-save',delete=False,encoding='utf-8') as f:
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
        QMessageBox.information(self,f'SCAB {__version__} · Controls & limits',
            'VIM (complete modal editing): motions h j k l w W b B e E ge gE 0 ^ $ g_ gg G H M L { } ( ) % [( [{ ]) ]} f F t T ; , * # n N / ? marks ` \' and Ctrl-O/Ctrl-I jumps.\n'
            'Operators d c y > < = g~ gu gU gc (comment) with any motion or text object iw aw iW aW ip ap i( a( i{ a{ i[ a[ i" a" i\' a\'. '
            'Visual v V Ctrl-V (block insert I/A). Registers "a–"z, "0–"9, "+ (system clipboard; yank/put use it by default), dot repeat ., macros q/@, Ctrl-A/Ctrl-X on numbers, R replace, J join, u / Ctrl-R undo.\n'
            'Ex: :w :q :wq :x :e file :s/a/b/g :%s :g/pat/d :v :normal :sort :m :t :d :> :noh :set (ic scs sw ts rnu…) :reg :marks :make :cn :cp (jump between errors) :bn :bp.\n\n'
            'INSPECTOR: click any part of the model. The panel shows the primitive, every module call above it (in any file), its colors, loop values and the variables it uses. '
            'Edit a value and press Enter (mouse wheel / ↑↓ nudge numbers). "definition" edits where the value is written (shared); "only this call" overrides just that call.\n'
            'KICAD: PCB → Add KiCad board (several, from any project) shows boards with their 3D models next to your chassis and reloads them whenever KiCad saves. '
            'Move/rotate boards in the Boards tab or with Move/Rotate; poses are stored as <name>_pose in the file. File → Export STEP writes model + boards. '
            'include <kicad/board.scad> gives the outline, holes and part positions; the Clearance tab lists parts that collide or are too close.\n'
            'EDITING (Vim off or Insert mode): Ctrl+F find, Ctrl+H replace, F3 next, Ctrl+G go to line, Ctrl+/ comment, Alt+Up/Down move line, Ctrl+D duplicate.\n'
            'BOARDS: right-click a board → Open in KiCad; saving in KiCad updates the board and the SCAD model.\n'
            'TOOLS: header icons or right-click the model / object list / editor. I inspect, D dimensions, G move, R rotate, F fit, 0/7/1/3 views. Measure: click two points.\n'
            'PROBLEMS: syntax errors are shown while typing; OpenSCAD errors and warnings after compiling — red/yellow markers in the gutter, wavy underline, hover for the message, click in Problems or :cn to jump.\n\n'
            'Touchpad: left drag = orbit; Shift+left drag = pan; two-finger scroll/pinch = zoom. Middle/right navigation also supported.\n'
            'Object tool: Dimensions / Move / Rotate. Drag colored axes; release commits SCAD. Ctrl+left drag rolls camera. Panel arrows and cube minus minimize.\n'
            'Project: open folder, file tabs, F12 module definition, Ctrl+B saves project files and builds entry. Double-click a variable: type the new value, Enter applies, Shift+Enter previews.\n'
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
    app.setApplicationName('SCAB');app.setOrganizationName('SCAB')
    palette=QPalette()
    for role,color in ((QPalette.Window,'#1e1f22'),(QPalette.Base,'#1e1f22'),(QPalette.AlternateBase,'#25272a'),
        (QPalette.WindowText,'#cfd1d4'),(QPalette.Text,'#cfd1d4'),(QPalette.Button,'#393b40'),
        (QPalette.ButtonText,'#cfd1d4'),(QPalette.Highlight,'#2f5fbf'),(QPalette.HighlightedText,'#ffffff'),
        (QPalette.ToolTipBase,'#2b2d30'),(QPalette.ToolTipText,'#e6e7e9'),(QPalette.PlaceholderText,'#6b6e75'),
        (QPalette.Mid,'#45484d'),(QPalette.Dark,'#1a1b1e'),(QPalette.Light,'#4e5157')):
        palette.setColor(role,QColor(color))
    app.setPalette(palette);app.setStyleSheet(STYLE+tab_close_style())
    window=Window(sys.argv[1] if len(sys.argv)>1 else None)
    window.show()
    sys.exit(app.exec())
