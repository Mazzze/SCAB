"""Multi-document project tree with ordinary files, modules, variables and colors."""
import json
import re
from pathlib import Path
from PySide6.QtCore import Qt,QTimer,QObject,QEvent,QUrl
from PySide6.QtGui import QAction,QTextCursor,QColor,QBrush,QDesktopServices
from PySide6.QtWidgets import (QApplication,QMenu,QToolButton,QWidget,QTreeWidget,QTreeWidgetItem,QTabWidget,QFileDialog,QInputDialog,
    QMessageBox,QComboBox,QDialog,QVBoxLayout,QHBoxLayout,QLineEdit,QLabel,QDialogButtonBox,QPushButton)
from .editor import Editor
from .model import Document,SourceError
from .icons import icon

class ProjectMixin:
    def setup_documents(self,left_layout,middle_layout):
        self.project_root=None;self.project_entry=None;self.buffers={self.editor:None};self.switching=False
        self.project_tree=QTreeWidget();self.project_tree.setHeaderLabels(['Project','Value'])
        self.project_tree.setColumnWidth(0,145);self.project_tree.setColumnWidth(1,90)
        self.project_tree.setMinimumHeight(60)
        self.project_tree.itemDoubleClicked.connect(self.project_item_open)
        self.project_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.project_tree.customContextMenuRequested.connect(self.project_context_menu)
        self.project_tree.setToolTip('Double-click to open / edit · right-click for actions')
        left_layout.addWidget(self.project_header())
        left_layout.addWidget(self.project_tree,1)
        self.editor_tabs=QTabWidget();self.editor_tabs.setTabsClosable(True);self.editor_tabs.setDocumentMode(True)
        self.editor_tabs.addTab(self.editor,'Untitled.scad')
        self.editor_tabs.currentChanged.connect(self.activate_document)
        self.editor_tabs.tabCloseRequested.connect(self.close_document)
        self.view_target=QComboBox();self.view_target.addItems(['Current file','Project entry'])
        self.view_target.setToolTip('What the 3D view shows, independent of the tab being edited. Project entry includes unsaved module edits.')
        view_row=QHBoxLayout();view_row.setContentsMargins(6,3,6,3);show=QLabel('Show:');show.setObjectName('secondary');view_row.addWidget(show);view_row.addWidget(self.view_target);view_row.addStretch(1)
        self.view_status=QLabel('Waiting for compilation');self.view_status.setObjectName('secondary');view_row.addWidget(self.view_status,3)
        middle_layout.addLayout(view_row)
        self.view_target.currentIndexChanged.connect(self.view_target_changed)
        middle_layout.addWidget(self.editor_tabs,1)
        from .findbar import FindBar
        self.find_bar=FindBar(lambda:self.editor)
        middle_layout.addWidget(self.find_bar)
        menu=QMenu('Project',self);self.project_menu_bar.insertMenu(self.help_menu.menuAction(),menu)
        for title,fn,shortcut in [('New project…',self.new_project,''),('Open project…',self.open_project,''),
            ('New module file…',self.new_module_file,''),('Save all',self.save_all,'Ctrl+Alt+S'),
            ('Build entry (saves all)',self.build_project,'Ctrl+B'),('Go to module definition',self.goto_definition,'F12')]:
            action=QAction(title,self);action.triggered.connect(fn)
            if shortcut:action.setShortcut(shortcut)
            menu.addAction(action)
        self.project_timer=QTimer(self);self.project_timer.setSingleShot(True);self.project_timer.timeout.connect(self.refresh_project)
        self.project_timer.start(100)

    # ------------------------------------------------------------ panel header
    def project_header(self):
        """Project switcher (current + recent projects and files), tool buttons and a filter."""
        box=QWidget();layout=QVBoxLayout(box);layout.setContentsMargins(0,0,0,0);layout.setSpacing(0)
        title=QLabel('Project');title.setObjectName('panelTitle');layout.addWidget(title)
        row=QHBoxLayout();row.setContentsMargins(4,3,4,2);row.setSpacing(2)
        self.project_switcher=QComboBox();self.project_switcher.setMinimumWidth(80)
        self.project_switcher.setToolTip('Switch project: recent project folders and files')
        self.project_switcher.activated.connect(self.project_switcher_chosen)
        row.addWidget(self.project_switcher,1)
        def tool(name,tip,fn,menu=None):
            button=QToolButton();button.setIcon(icon(name));button.setToolTip(tip)
            if menu is not None:button.setMenu(menu);button.setPopupMode(QToolButton.InstantPopup)
            else:button.clicked.connect(fn)
            row.addWidget(button);return button
        tool('open_recent','Open recent project or file',None,self.recent_menu)
        tool('open','Open project folder…',lambda:self.open_project())
        tool('new','New module file in this project…',lambda:self.new_module_file())
        tool('reload','Refresh the project tree',lambda:self.refresh_project())
        layout.addLayout(row)
        self.project_filter=QLineEdit();self.project_filter.setPlaceholderText('Filter files, variables, modules, boards…')
        self.project_filter.setClearButtonEnabled(True);self.project_filter.textChanged.connect(self.apply_project_filter)
        filter_row=QHBoxLayout();filter_row.setContentsMargins(4,0,4,3);filter_row.addWidget(self.project_filter)
        layout.addLayout(filter_row)
        return box

    def refresh_project_switcher(self):
        if not hasattr(self,'project_switcher'):return
        combo=self.project_switcher;combo.blockSignals(True);combo.clear()
        current=self.project_root or self.path
        if current is not None:
            combo.addItem(icon('folder' if self.project_root else 'file'),current.name,('current',current))
            combo.setItemData(0,str(current),Qt.ToolTipRole)
        else:
            combo.addItem(icon('file'),'Untitled.scad',('current',None))
        for kind,path in self.recent_entries():
            if current is not None and Path(path).resolve()==Path(current).resolve():continue
            combo.addItem(icon('folder' if kind=='project' else 'file'),f'{path.name}  ·  {path.parent.name}',(kind,path))
            combo.setItemData(combo.count()-1,str(path),Qt.ToolTipRole)
        combo.insertSeparator(combo.count())
        combo.addItem(icon('open'),'Open project folder…',('open_folder',None))
        combo.addItem(icon('open'),'Open file…',('open_file',None))
        combo.setCurrentIndex(0);combo.blockSignals(False)

    def project_switcher_chosen(self,index):
        data=self.project_switcher.itemData(index)
        self.project_switcher.setCurrentIndex(0)
        if not data:return
        kind,path=data
        if kind=='open_folder':self.open_project()
        elif kind=='open_file':self.open_file()
        elif kind in ('project','file'):self.switch_project(path)

    def switch_project(self,path):
        """Jump to another project folder or file; unmodified tabs of the previous project are closed."""
        path=Path(path)
        if not path.exists():
            self.info(f'{path} no longer exists');self.refresh_recent_menu();return
        new_root=path if path.is_dir() else (path.parent if (path.parent/'scab-project.json').is_file() else None)
        keep=lambda p:p is not None and new_root is not None and Path(p).resolve().is_relative_to(new_root.resolve())
        for index in reversed(range(self.editor_tabs.count())):
            editor=self.editor_tabs.widget(index);existing=self.buffers.get(editor)
            if editor.document().isModified() or keep(existing) or (path.is_file() and existing==path.resolve()):continue
            if self.editor_tabs.count()>1:self.close_document(index)
        if new_root is None and self.project_root is not None and not path.resolve().is_relative_to(self.project_root):
            self.project_root=None;self.project_entry=None;self.view_target.setCurrentIndex(0)
        self.load_file(path)
        self.refresh_project();self.refresh_project_switcher()

    def forget_recent(self,path):
        for key in ('recent','recent_projects'):
            self.settings.setValue(key,[p for p in self.settings_list(key) if Path(p)!=Path(path)])
        self.refresh_recent_menu();self.refresh_project()

    def apply_project_filter(self,*_):
        text=self.project_filter.text().strip().lower() if hasattr(self,'project_filter') else ''
        def visit(item):
            own=not text or text in item.text(0).lower() or text in item.text(1).lower()
            child_match=False
            for k in range(item.childCount()):
                child_match=visit(item.child(k)) or child_match
            show=own or child_match
            item.setHidden(not show)
            if text and child_match:item.setExpanded(True)
            return show
        for k in range(self.project_tree.topLevelItemCount()):
            top=self.project_tree.topLevelItem(k)
            if top.data(0,Qt.UserRole) and top.data(0,Qt.UserRole)[0]=='recent_group' and not text:
                top.setHidden(False);visit(top);continue
            visit(top)

    def wire_editor(self,editor):
        editor.hintChanged.connect(lambda text:self.hint.setText(text or 'Ctrl+Space: completion · F5: compile · Esc: normal mode · :help'))
        editor.modeChanged.connect(lambda text,e=editor:self.mode_label.setText(text) if e is self.editor else None)
        editor.message.connect(self.info)
        editor.findRequested.connect(lambda replace:self.find_bar.open(replace))
        editor.findNextRequested.connect(self.find_next)
        editor.gotoLineRequested.connect(self.goto_line_dialog)
        editor.ex_handler=self.vim_command
        editor.definition_handler=self.goto_definition_word
        editor.context_actions=lambda:[self.actions[n] if n!='-' else None for n in ('go_source','-','color','duplicate','pattern','boolean','thread','animate','-','build')] if hasattr(self,'actions') else []
        editor.toggle_vim(getattr(self,'vim_enabled',True))
        editor.textChanged.connect(lambda e=editor:self.document_changed(e))

    def document_changed(self,editor):
        if editor is self.editor and not self.switching:self.source_changed()
        self.refresh_tab_titles()

    def refresh_tab_titles(self):
        if not hasattr(self,'editor_tabs'):return
        for index in range(self.editor_tabs.count()):
            editor=self.editor_tabs.widget(index);path=self.buffers.get(editor)
            self.editor_tabs.setTabText(index,(path.name if path else 'Untitled.scad')+(' *' if editor.document().isModified() else ''))
            self.editor_tabs.setTabToolTip(index,str(path) if path else 'Unsaved file')

    def pinned_view(self):
        return bool(self.project_entry and self.view_target.currentIndex()==1)

    def view_matches_editor(self):
        return self.compiled_source==self.editor.toPlainText() and getattr(self,'compiled_path',None)==self.path

    def view_target_changed(self,*_):
        self.compiled_source=None;self.viewer.set_handles([]);self.viewer.set_gizmo('Dimensions')
        self.compile_timer.start(100)

    def render_context(self,source_override=None):
        target=self.project_entry if self.pinned_view() and source_override is None else self.path
        source=self.source_for(target) if source_override is None else source_override
        snapshot=None
        if self.project_root and target and target.is_relative_to(self.project_root):
            snapshot=self.project_snapshot()
            snapshot['entry']=str(target.relative_to(self.project_root))
        return source,str(target.parent if target else Path.cwd()),snapshot,target

    def activate_document(self,index):
        if index<0 or self.switching:return
        pinned=self.pinned_view()
        if not pinned:
            self.cancel_jobs(silent=True);self.generation+=1;self.compiled_source=None;self.frames=[]
        self.editor=self.editor_tabs.widget(index);self.path=self.buffers.get(self.editor)
        self.node=None;self.doc=None
        self.parse_timer.stop()
        self.parse();self.update_title();self.mode_label.setText(self.editor.mode if self.editor.vim else 'EDIT')
        self.viewer.set_handles([]);self.viewer.set_gizmo('Dimensions')
        if pinned:
            self.info('Editing '+self.path.name+' · GPU view stays on '+self.project_entry.name if self.path else 'Project entry view pinned')
        else:self.compile_timer.start(150)
        self.refresh_project()

    def open_document(self,path):
        path=Path(path).resolve()
        for editor,existing in self.buffers.items():
            if existing==path:
                self.editor_tabs.setCurrentWidget(editor);return True
        try:text=path.read_text(encoding='utf-8-sig')
        except (OSError,UnicodeError) as error:self.error(str(error));return False
        self.switching=True
        # Reuse the blank startup tab only if it is empty; never discard another buffer.
        blank=self.editor_tabs.widget(0) if self.editor_tabs.count()==1 else None
        if blank is not None and self.buffers.get(blank) is None and not blank.document().isModified() and not blank.toPlainText().strip():
            editor=blank;self.buffers[editor]=path;index=0;self.editor_tabs.setTabText(0,path.name)
        else:
            editor=Editor();self.wire_editor(editor)
            self.buffers[editor]=path;index=self.editor_tabs.addTab(editor,path.name)
        editor.setPlainText(text);editor.document().setModified(False)
        self.switching=False;self.editor_tabs.setCurrentIndex(index);self.activate_document(index)
        if index==0 and editor is self.editor_tabs.widget(0):self.editor=editor;self.path=path;self.update_title()
        self.remember_recent(path);self.watch_source();self.check_kicad_link()
        return True

    def save_buffer(self,editor):
        path=self.buffers.get(editor)
        if path is None:
            self.editor_tabs.setCurrentWidget(editor)
            if not self.save():return False
            self.buffers[editor]=self.path;return True
        try:
            import os,tempfile
            with tempfile.NamedTemporaryFile('w',dir=path.parent,delete=False,encoding='utf-8') as f:
                f.write(editor.toPlainText());temporary=Path(f.name)
            try:os.replace(temporary,path)
            finally:temporary.unlink(missing_ok=True)
            editor.document().setModified(False);self.refresh_tab_titles();return True
        except OSError as error:self.error(str(error));return False

    def save_all(self,project_only=False):
        current=self.editor
        for editor in list(self.buffers):
            path=self.buffers.get(editor)
            if project_only and (path is None or self.project_root is None or not path.is_relative_to(self.project_root)):continue
            if editor.document().isModified() and not self.save_buffer(editor):return False
        self.editor_tabs.setCurrentWidget(current);self.refresh_project();return True

    def close_document(self,index):
        editor=self.editor_tabs.widget(index)
        if editor.document().isModified():
            answer=QMessageBox.question(self,'Unsaved file','Save this file before closing?',QMessageBox.Save|QMessageBox.Discard|QMessageBox.Cancel)
            if answer==QMessageBox.Cancel:return
            if answer==QMessageBox.Save and not self.save_buffer(editor):return
        if self.editor_tabs.count()==1:
            editor.clear();editor.document().setModified(False);self.buffers[editor]=None;self.path=None;self.refresh_tab_titles();self.update_title();return
        self.buffers.pop(editor,None);self.editor_tabs.removeTab(index);editor.deleteLater()

    def new_project(self):
        parent=QFileDialog.getExistingDirectory(self,'Choose parent directory for a new project')
        if not parent:return
        name,ok=QInputDialog.getText(self,'New project','Project folder name:',text='MyCADProject')
        if not ok or not re.fullmatch(r'[\w -]+',name):return
        root=Path(parent)/name
        try:
            root.mkdir();(root/'modules').mkdir()
            (root/'main.scad').write_text(self.editor.toPlainText(),encoding='utf-8')
            if self.path:self.copy_project_dependencies(self.path.parent,root,self.editor.toPlainText())
            (root/'scab-project.json').write_text(json.dumps({'entry':'main.scad'},indent=2),encoding='utf-8')
        except OSError as error:self.error(str(error));return
        self.set_project(root)

    def copy_project_dependencies(self,origin,target,source):
        import shutil
        origin=Path(origin).resolve();seen=set()
        def copy_references(text,parent):
            refs=re.findall(r'(?:include|use)\s*<([^>]+)>|import\s*\(\s*"([^"]+)"',text)
            for include,imported in refs:
                relative=include or imported
                file=(parent/relative).resolve()
                if file in seen or len(seen)>2000 or not file.is_file() or not file.is_relative_to(origin):continue
                seen.add(file);destination=target/file.relative_to(origin);destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(file,destination)
                for metadata in ('LICENSE','UPSTREAM.txt'):
                    if (file.parent/metadata).is_file():shutil.copy2(file.parent/metadata,destination.parent/metadata)
                if file.suffix.lower()=='.scad':copy_references(file.read_text(encoding='utf-8-sig'),file.parent)
        copy_references(source,origin)

    def open_project(self):
        directory=QFileDialog.getExistingDirectory(self,'Open project folder')
        if directory:self.set_project(Path(directory))

    def set_project(self,root):
        root=Path(root).resolve();entry=root/'main.scad'
        manifest=root/'scab-project.json'
        if not manifest.exists() and (root/'forge-project.json').exists():manifest=root/'forge-project.json'
        try:
            if manifest.exists():entry=(root/json.loads(manifest.read_text())['entry']).resolve()
            if not entry.is_relative_to(root):raise ValueError('Entry must be inside project')
            if not entry.is_file():
                files=sorted(p for p in root.glob('*.scad'))
                if not files:raise ValueError('No .scad entry file in project')
                entry=files[0]
            self.project_root=root;self.project_entry=entry
            self.open_document(entry);self.view_target.setCurrentIndex(1);self.remember_project(root);self.refresh_project()
        except (OSError,ValueError,KeyError) as error:self.error(str(error))

    def new_module_file(self):
        if self.project_root is None:
            self.info('Create or open a project first.');return
        name,ok=QInputDialog.getText(self,'New module','Module / file name:',text='part')
        if not ok or not re.fullmatch(r'[A-Za-z_]\w*',name):return
        file=self.project_root/'modules'/(name+'.scad');file.parent.mkdir(exist_ok=True)
        if file.exists():self.open_document(file);return
        file.write_text(f'module {name}(size=10) {{\n    cube(size, center=true);\n}}\n',encoding='utf-8')
        self.open_document(self.project_entry)
        relative=file.relative_to(self.project_root).as_posix()
        if f'<{relative}>' not in self.editor.toPlainText():self.replace_source(f'use <{relative}>\n'+self.editor.toPlainText())
        self.open_document(file);self.refresh_project()

    def project_snapshot(self,active_source=None):
        if not self.project_root or not self.project_entry:return None
        overrides={str(path.relative_to(self.project_root)):editor.toPlainText() for editor,path in self.buffers.items()
                   if path and path.is_relative_to(self.project_root)}
        if active_source is not None and self.path and self.path.is_relative_to(self.project_root):
            overrides[str(self.path.relative_to(self.project_root))]=active_source
        return {'root':str(self.project_root),'entry':str(self.project_entry.relative_to(self.project_root)),'overrides':overrides}

    def build_project(self):
        if not self.project_entry:
            self.info('Open a project folder first.');return
        if not self.save_all(project_only=True):return
        self.view_target.setCurrentIndex(1);self.compile()

    def project_files(self):
        if self.project_root:
            # Exclude generated/library/environment trees from the editable index.
            return sorted(p for p in self.project_root.rglob('*.scad')
                          if not any(s.startswith('.') or s in ('lib','BOSL2','__pycache__') for s in p.relative_to(self.project_root).parts))[:500]
        return [self.path] if self.path else [None]

    def source_for(self,path):
        if path is None:return self.editor.toPlainText()
        for editor,existing in self.buffers.items():
            if existing==path:return editor.toPlainText()
        return Path(path).read_text(encoding='utf-8-sig')

    def definitions(self):
        result=[]
        for path in self.project_files():
            try:
                source=self.source_for(path)
                doc=Document(source)
                for node in doc.nodes:
                    if node.name=='module':
                        tokens=[t for t in doc.tokens if node.start<=t.start<node.end]
                        result.append((tokens[1].value,path,node.start,node.end))
            except (OSError,SourceError):continue
        return result

    def refresh_project(self):
        if not hasattr(self,'project_tree'):return
        self.refresh_tab_titles();self.project_tree.clear()
        root=QTreeWidgetItem([self.project_root.name if self.project_root else 'Current files',''])
        self.project_tree.addTopLevelItem(root)
        definitions=self.definitions()
        for path in self.project_files():
            try:
                source=self.source_for(path);doc=Document(source)
                label=str(path.relative_to(self.project_root)) if path and self.project_root else path.name if path else 'Untitled.scad'
                fileitem=QTreeWidgetItem([label,'']);fileitem.setData(0,Qt.UserRole,('file',path));root.addChild(fileitem)
                for name,a,b in doc.variables():
                    value=source[a:b].strip();item=QTreeWidgetItem([name,value]);item.setData(0,Qt.UserRole,('variable',path,name));fileitem.addChild(item)
                for name,file,a,b in definitions:
                    if file==path:
                        item=QTreeWidgetItem(['module '+name,'↗']);item.setData(0,Qt.UserRole,('module',path,a,b));fileitem.addChild(item)
            except (OSError,SourceError):continue
        boards=getattr(self,'boards',{})
        if boards:
            group=QTreeWidgetItem(['KiCad boards',str(len(boards))]);group.setData(0,Qt.UserRole,('boards',None))
            self.project_tree.addTopLevelItem(group)
            for ident,entry in boards.items():
                state='loading…' if entry.loading else 'error' if entry.error else '' if entry.visible else 'hidden'
                item=QTreeWidgetItem([ident,state or entry.path.name]);item.setData(0,Qt.UserRole,('board',None,ident))
                item.setToolTip(0,str(entry.path));item.setToolTip(1,entry.error or str(entry.path))
                if not entry.visible:item.setForeground(0,QBrush(QColor('#7d8188')))
                group.addChild(item)
        current=self.project_root or self.path
        recent=[(k,p) for k,p in (self.recent_entries() if hasattr(self,'recent_entries') else [])
                if current is None or Path(p).resolve()!=Path(current).resolve()]
        if recent:
            group=QTreeWidgetItem(['Recent',str(len(recent))]);group.setData(0,Qt.UserRole,('recent_group',None))
            group.setToolTip(0,'Double-click a project or file to switch to it')
            self.project_tree.addTopLevelItem(group)
            for kind,path in recent[:15]:
                item=QTreeWidgetItem([path.name,path.parent.name]);item.setIcon(0,icon('folder' if kind=='project' else 'file'))
                item.setData(0,Qt.UserRole,('recent',None,path));item.setToolTip(0,str(path));group.addChild(item)
        self.project_tree.expandToDepth(1)
        for k in range(self.project_tree.topLevelItemCount()):
            top=self.project_tree.topLevelItem(k)
            if top.data(0,Qt.UserRole) and top.data(0,Qt.UserRole)[0]=='recent_group':top.setExpanded(False)
        self.apply_project_filter()
        self.refresh_project_switcher()

    def project_item_open(self,item,column=0):
        data=item.data(0,Qt.UserRole)
        if not data:return
        kind,path,*args=data
        if path is not None:self.open_document(path)
        if kind=='module':self.editor.select_source(args[0],args[0]+len('module'));self.editor.setFocus()
        elif kind=='variable':self.edit_tree_variable(args[0])
        elif kind=='recent':
            self.switch_project(args[0]);return
        elif kind=='board':
            ident=args[0]
            self.select_board(ident);self.boards_panel.select(ident);self.focus_board(ident)

    def project_context_menu(self,pos):
        item=self.project_tree.itemAt(pos)
        global_pos=self.project_tree.viewport().mapToGlobal(pos)
        data=item.data(0,Qt.UserRole) if item else None
        menu=QMenu(self)
        kind=data[0] if data else None
        if kind=='recent':
            path=data[2]
            menu.addAction(icon('open'),'Switch to this '+('project' if Path(path).is_dir() else 'file'),lambda:self.switch_project(path))
            menu.addAction('Show in file manager',lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if Path(path).is_dir() else Path(path).parent))))
            menu.addAction('Copy path',lambda:QApplication.clipboard().setText(str(path)))
            menu.addAction('Remove from recent',lambda:self.forget_recent(path))
        elif kind=='recent_group':
            menu.addAction('Clear recent',self.clear_recent)
        elif kind=='board':
            if not self.fill_board_menu(menu,data[2]):return
        elif kind=='file':
            path=data[1]
            menu.addAction(icon('open'),'Open',lambda:self.open_document(path) if path else None).setEnabled(path is not None)
            if path is not None:
                menu.addAction('Show in file manager',lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent))))
                menu.addAction('Copy path',lambda:QApplication.clipboard().setText(str(path)))
                if self.project_root and self.project_entry and Path(path)!=self.project_entry:
                    menu.addAction('Set as project entry',lambda:self.set_project_entry(path))
            menu.addSeparator()
            self.fill_project_menu(menu)
        elif kind=='variable':
            path,name=data[1],data[2]
            menu.addAction(icon('settings'),f'Edit value of {name}…',lambda:(self.open_document(path) if path else None,self.edit_tree_variable(name)))
            menu.addAction(icon('go_source'),'Go to definition',lambda:self.tree_goto(path,name))
            menu.addAction(icon('find'),'Find usages',lambda:self.tree_find_usages(path,name))
            menu.addAction('Copy name',lambda:QApplication.clipboard().setText(name))
        elif kind=='module':
            path,a,b=data[1],data[2],data[3]
            name=item.text(0).split(' ',1)[-1]
            menu.addAction(icon('go_source'),'Go to definition',lambda:self.project_item_open(item))
            menu.addAction('Insert call at cursor',lambda:self.tree_insert_call(name))
            menu.addAction(icon('find'),'Find usages',lambda:self.tree_find_usages(None,name))
            menu.addAction('Copy name',lambda:QApplication.clipboard().setText(name))
        else:
            if kind=='boards':
                menu.addAction(icon('kicad_import'),'Add KiCad board…',self.load_kicad)
                menu.addAction(icon('reload'),'Reload all boards',self.reload_kicad)
            else:
                self.fill_project_menu(menu)
        if not menu.isEmpty():menu.exec(global_pos)

    def fill_project_menu(self,menu):
        menu.addAction(icon('kicad_import'),'Add KiCad board…',self.load_kicad)
        menu.addAction(icon('new'),'New module file…',self.new_module_file).setEnabled(self.project_root is not None)
        menu.addAction(icon('open'),'Open project folder…',self.open_project)
        menu.addAction(icon('reload'),'Refresh',self.refresh_project)

    def set_project_entry(self,path):
        self.project_entry=Path(path).resolve()
        manifest=self.project_root/'scab-project.json'
        try:manifest.write_text(json.dumps({'entry':self.project_entry.relative_to(self.project_root).as_posix()},indent=2)+'\n',encoding='utf-8')
        except OSError as error:self.error(str(error));return
        self.view_target.setCurrentIndex(1);self.refresh_project();self.compile()

    def tree_goto(self,path,name):
        if path is not None:self.open_document(path)
        self.goto_definition_word(name);self.editor.setFocus()

    def tree_find_usages(self,path,name):
        if path is not None:self.open_document(path)
        bar=self.find_bar
        bar.regex.setChecked(False);bar.word.setChecked(True)
        bar.find.setText(name);bar.replace_row.setVisible(False);bar.show();bar.refresh();bar.next(False)
        self.info(f'{len(bar.matches())} uses of {name} in this file · Enter / F3 for the next one')

    def tree_insert_call(self,name):
        cursor=self.editor.textCursor();cursor.insertText(f'{name}();');self.editor.setFocus()

    def find_next(self,backwards=False):
        if not self.find_bar.find.text():
            self.find_bar.open(False);return
        if not self.find_bar.isVisible():self.find_bar.show();self.find_bar.refresh()
        self.find_bar.next(backwards)

    def goto_line_dialog(self):
        current=self.editor.textCursor().blockNumber()+1
        line,ok=QInputDialog.getInt(self,'Go to line','Line:',current,1,max(1,self.editor.blockCount()))
        if ok:self.editor.goto_line(line-1)

    def goto_definition_word(self,word):
        """Jump to a module, function or variable definition anywhere in the project / includes."""
        pattern=re.compile(r'^[ \t]*(?:module|function)[ \t]+'+re.escape(word)+r'\b|^[ \t]*'+re.escape(word)+r'[ \t]*=(?!=)',re.M)
        seen=set();files=[]
        for path in self.project_files():
            files.append(path)
        def includes(path,text,depth=0):
            if depth>6:return
            for m in re.finditer(r'(?:include|use)\s*<([^>]+)>',text):
                candidate=((path.parent if path else Path.cwd())/m[1]).resolve()
                if candidate.is_file() and candidate not in seen:
                    seen.add(candidate);files.append(candidate)
                    try:includes(candidate,self.source_for(candidate),depth+1)
                    except (OSError,UnicodeError):pass
        try:includes(self.path,self.editor.toPlainText())
        except (OSError,UnicodeError):pass
        current=self.editor.toPlainText()
        m=pattern.search(current)
        if m:
            self.editor.select_source(m.start()+len(m[0])-len(m[0].lstrip()),m.end());return True
        for path in files:
            if path is None or (self.path is not None and Path(path).resolve()==Path(self.path).resolve()):continue
            try:text=self.source_for(path)
            except (OSError,UnicodeError):continue
            m=pattern.search(text)
            if m:
                self.open_document(path)
                self.editor.select_source(m.start()+len(m[0])-len(m[0].lstrip()),m.end());self.editor.setFocus()
                return True
        return False

    def goto_definition(self):
        cursor=self.editor.textCursor();cursor.select(QTextCursor.WordUnderCursor);name=cursor.selectedText()
        matches=[entry for entry in self.definitions() if entry[0]==name]
        if not matches:self.info('No module definition found in the project.');return
        choice=matches[0]
        if len(matches)>1:
            options=[str(m[1]) for m in matches];selected,ok=QInputDialog.getItem(self,'Module definitions','Choose file:',options,0,False)
            if not ok:return
            choice=matches[options.index(selected)]
        _,path,start,end=choice
        if path:self.open_document(path)
        self.editor.select_source(start,start+len('module '+name));self.editor.setFocus()

    def edit_tree_variable(self,name):
        if name=='$t':self.info('Use the animation timeline to preview $t; the compiler sets it per frame.');return
        if not self.parse():return
        variable=next((v for v in self.doc.variables() if v[0]==name),None)
        if not variable:return
        _,a,b=variable;source=self.doc.source
        dialog=QDialog(self);dialog.setWindowTitle('Variable · '+name);layout=QVBoxLayout(dialog)
        hint=QLabel('Enter: apply · Shift+Enter: preview in 3D (source unchanged) · Esc: cancel');hint.setObjectName('secondary')
        layout.addWidget(hint)
        field=QLineEdit(source[a:b].strip());layout.addWidget(field)
        preview=QPushButton('Preview in 3D  (Shift+Enter)');preview.setAutoDefault(False);layout.addWidget(preview)
        run_preview=lambda:self.preview_source(source[:a]+' '+field.text()+source[b:])
        preview.clicked.connect(run_preview)
        controls=QDialogButtonBox(QDialogButtonBox.Apply|QDialogButtonBox.Cancel);layout.addWidget(controls)
        for button in controls.buttons():button.setAutoDefault(False)
        controls.button(QDialogButtonBox.Apply).clicked.connect(dialog.accept);controls.rejected.connect(dialog.reject)
        class Keys(QObject):
            def eventFilter(self,obj,event):
                if event.type()==QEvent.KeyPress and event.key() in (Qt.Key_Return,Qt.Key_Enter):
                    if event.modifiers()&Qt.ShiftModifier:run_preview()
                    else:dialog.accept()
                    return True
                return False
        keys=Keys(dialog);field.installEventFilter(keys)
        field.selectAll();field.setFocus()
        result=dialog.exec()
        if result:self.replace_source(source[:a]+' '+field.text()+source[b:])
        self.compile()

    def maybe_save_all(self):
        if not any(e.document().isModified() for e in self.buffers):return True
        answer=QMessageBox.question(self,'Unsaved project files','Save all changed files before closing?',QMessageBox.Save|QMessageBox.Discard|QMessageBox.Cancel)
        if answer==QMessageBox.Cancel:return False
        return self.save_all() if answer==QMessageBox.Save else True
