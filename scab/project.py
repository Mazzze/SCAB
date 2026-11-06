"""Multi-document project tree with ordinary files, modules, variables and colors."""
import json
import re
from pathlib import Path
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QAction,QTextCursor,QColor,QBrush
from PySide6.QtWidgets import (QMenu,QTreeWidget,QTreeWidgetItem,QTabWidget,QFileDialog,QInputDialog,
    QMessageBox,QComboBox,QDialog,QVBoxLayout,QHBoxLayout,QLineEdit,QLabel,QDialogButtonBox,QPushButton)
from .editor import Editor
from .model import Document,SourceError

class ProjectMixin:
    def setup_documents(self,left_layout,middle_layout):
        self.project_root=None;self.project_entry=None;self.buffers={self.editor:None};self.switching=False
        self.project_tree=QTreeWidget();self.project_tree.setHeaderLabels(['Project','Value'])
        self.project_tree.setColumnWidth(0,145);self.project_tree.setColumnWidth(1,90)
        self.project_tree.setMinimumHeight(130);self.project_tree.setMaximumHeight(320)
        self.project_tree.itemDoubleClicked.connect(self.project_item_open)
        project_title=QLabel('Project');project_title.setObjectName('panelTitle')
        left_layout.addWidget(project_title);left_layout.addWidget(self.project_tree,1)
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
        menu=QMenu('Project',self);self.project_menu_bar.insertMenu(self.help_menu.menuAction(),menu)
        for title,fn,shortcut in [('New project…',self.new_project,''),('Open project…',self.open_project,''),
            ('New module file…',self.new_module_file,''),('Save all',self.save_all,'Ctrl+Alt+S'),
            ('Build entry (saves all)',self.build_project,'Ctrl+B'),('Go to module definition',self.goto_definition,'F12')]:
            action=QAction(title,self);action.triggered.connect(fn)
            if shortcut:action.setShortcut(shortcut)
            menu.addAction(action)
        self.project_timer=QTimer(self);self.project_timer.setSingleShot(True);self.project_timer.timeout.connect(self.refresh_project)
        self.project_timer.start(100)

    def wire_editor(self,editor):
        editor.hintChanged.connect(lambda text:self.hint.setText(text or 'Ctrl+Space: completion · F5: compile · Esc: normal mode · :help'))
        editor.modeChanged.connect(lambda text,e=editor:self.mode_label.setText(text) if e is self.editor else None)
        editor.message.connect(self.info)
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
            self.open_document(entry);self.view_target.setCurrentIndex(1);self.refresh_project()
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
        self.project_tree.expandToDepth(1)

    def project_item_open(self,item,column=0):
        data=item.data(0,Qt.UserRole)
        if not data:return
        kind,path,*args=data
        if path is not None:self.open_document(path)
        if kind=='module':self.editor.select_source(args[0],args[0]+len('module'));self.editor.setFocus()
        elif kind=='variable':self.edit_tree_variable(args[0])

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
        layout.addWidget(QLabel('SCAD expression · Preview does not change the source until Apply'))
        field=QLineEdit(source[a:b].strip());layout.addWidget(field)
        preview=QPushButton('Preview in 3D');layout.addWidget(preview)
        preview.clicked.connect(lambda:self.preview_source(source[:a]+' '+field.text()+source[b:]))
        controls=QDialogButtonBox(QDialogButtonBox.Apply|QDialogButtonBox.Cancel);layout.addWidget(controls)
        controls.button(QDialogButtonBox.Apply).clicked.connect(dialog.accept);controls.rejected.connect(dialog.reject)
        result=dialog.exec()
        if result:self.replace_source(source[:a]+' '+field.text()+source[b:])
        self.compile()

    def maybe_save_all(self):
        if not any(e.document().isModified() for e in self.buffers):return True
        answer=QMessageBox.question(self,'Unsaved project files','Save all changed files before closing?',QMessageBox.Save|QMessageBox.Discard|QMessageBox.Cancel)
        if answer==QMessageBox.Cancel:return False
        return self.save_all() if answer==QMessageBox.Save else True
