import re
from PySide6.QtCore import Qt, Signal, QStringListModel
from PySide6.QtGui import (QColor, QFont, QSyntaxHighlighter, QTextCharFormat,
                           QTextCursor, QTextFormat, QPainter)
from PySide6.QtWidgets import QPlainTextEdit, QCompleter, QTextEdit, QWidget
from .model import SIGNATURES, TOKEN


class Highlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        def paint(start, length, color, bold=False):
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold: f.setFontWeight(QFont.Weight.Bold)
            self.setFormat(start, length, f)
        state=self.previousBlockState()
        depth=max(0,state)//2
        in_comment=bool(max(0,state)%2)
        palette=('#ffd479','#83d9ff','#d9a0ff','#8fe0b1','#ff9ca8','#c9d8ff')
        for m in TOKEN.finditer(text):
            v=m[0];start=m.start();end=m.end()
            if in_comment:
                close=text.find('*/',start)
                if close<0:
                    paint(start,len(text)-start,'#728296');break
                paint(start,close+2-start,'#728296')
                in_comment=False
                # Token matching resumes below only after this comment's end.
                self._comment_end=close+2
                continue
            if start<getattr(self,'_comment_end',0):continue
            color=None
            if v.startswith('//'):paint(start,len(v),'#728296');break
            if v.startswith('/*'):
                color='#728296'
                if not v.endswith('*/'):in_comment=True
            elif v=='/' and text[start:start+2]=='/*':
                paint(start,len(text)-start,'#728296');in_comment=True;break
            elif v.startswith('"'):color='#badc98'
            elif v in ('(', '[', '{'):
                color=palette[depth%len(palette)];depth+=1
            elif v in (')', ']', '}'):
                depth=max(0,depth-1);color=palette[depth%len(palette)]
            elif re.fullmatch(r'\d+(?:\.\d*)?(?:[eE][+-]?\d+)?',v):color='#e4bb81'
            elif v in SIGNATURES:color='#7cc7f2'
            elif v in ('module','function','if','else','let','each','true','false','undef','include','use'):color='#ca9ee6'
            elif v.startswith('$'):color='#e69cba'
            if color:paint(start,len(v),color)
        self._comment_end=0
        self.setCurrentBlockState(depth*2+int(in_comment))


class Gutter(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(event.rect(), QColor('#151b24'))
        block = self.editor.firstVisibleBlock()
        top = self.editor.blockBoundingGeometry(block).translated(self.editor.contentOffset()).top()
        while block.isValid() and top <= event.rect().bottom():
            h = self.editor.blockBoundingRect(block).height()
            if block.isVisible() and top+h >= event.rect().top():
                p.setPen(QColor('#69788c'))
                p.drawText(0, int(top), self.width()-10, int(h), Qt.AlignRight, str(block.blockNumber()+1))
            block = block.next()
            top += h


class Editor(QPlainTextEdit):
    modeChanged = Signal(str)
    hintChanged = Signal(str)
    command = Signal(str)

    def __init__(self):
        super().__init__()
        self.setFont(QFont('DejaVu Sans Mono', 11))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * 4)
        self.highlighter = Highlighter(self.document())
        self.vim = True
        self.mode = 'NORMAL'
        self.pending, self.count, self.command_buffer, self.register = '', '', '', ''
        self.find_wait=None; self.last_find=None
        self.completer = QCompleter(self)
        self.word_model = QStringListModel(self.completer)
        self.completer.setModel(self.word_model)
        self.completer.setWidget(self)
        self.completer.setCaseSensitivity(Qt.CaseSensitive)
        self.completer.setCompletionMode(QCompleter.PopupCompletion)
        self.completer.activated.connect(self.complete)
        self.gutter = Gutter(self)
        self.blockCountChanged.connect(self.update_gutter)
        self.updateRequest.connect(lambda *_: self.gutter.update())
        self.cursorPositionChanged.connect(self.cursor_changed)
        self.textChanged.connect(self.update_words)
        self.update_gutter()

    def update_gutter(self, *_):
        self.setViewportMargins(48, 0, 0, 0)
        self.gutter.setGeometry(0, 0, 46, self.height())
        self.gutter.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.update_gutter()

    def update_words(self):
        words = set(re.findall(r'[$A-Za-z_][$\w]*', self.toPlainText())) | set(SIGNATURES)
        self.all_words = words
        self.word_model.setStringList(sorted(words))

    def set_mode(self, mode):
        self.mode = mode
        self.pending = self.count = ''
        self.modeChanged.emit(mode if self.vim else 'EDIT')

    def toggle_vim(self, checked):
        self.vim = checked
        self.set_mode('NORMAL' if checked else 'INSERT')

    def cursor_changed(self):
        c = self.textCursor()
        before = self.toPlainText()[:c.position()]
        # Locate the innermost still-open call, including nested vectors/functions.
        stack = []
        for m in TOKEN.finditer(before):
            if m[0].startswith(('"','//','/*')): continue
            if m[0] in ('(', '['): stack.append((m[0], m.start()))
            elif m[0] in (')', ']') and stack: stack.pop()
        call = next((pos for kind, pos in reversed(stack) if kind == '('), None)
        if call is not None:
            m = re.search(r'([$\w]+)\s*$', before[:call])
        else:
            m = re.search(r'([$\w]+)\s*$', before)
        self.hintChanged.emit(SIGNATURES.get(m[1], '') if m else '')
        line = QTextEdit.ExtraSelection()
        line.format.setBackground(QColor('#1c2633'))
        line.format.setProperty(QTextFormat.FullWidthSelection, True)
        line.cursor = c
        line.cursor.clearSelection()
        self.setExtraSelections([line])
        self.gutter.update()

    def complete(self, word):
        c = self.textCursor()
        c.movePosition(QTextCursor.StartOfWord, QTextCursor.KeepAnchor)
        c.insertText(word)
        self.setTextCursor(c)

    def popup(self, force=False):
        c = self.textCursor()
        c.select(QTextCursor.WordUnderCursor)
        word = c.selectedText()
        if not force and len(word) < 2:
            self.completer.popup().hide()
            return
        # The current partial token is present in the document, but accepting it
        # would do nothing. Exclude exact matches, even if used elsewhere.
        candidates = sorted(w for w in getattr(self, 'all_words', set())
                            if w.startswith(word) and w != word)
        self.word_model.setStringList(candidates)
        if not candidates:
            self.completer.popup().hide()
            return
        self.completer.setCompletionPrefix(word)
        self.completer.setCurrentRow(0)
        rect = self.cursorRect()
        rect.setWidth(310)
        self.completer.complete(rect)

    def select_source(self, start, end):
        c = self.textCursor()
        source = self.toPlainText()
        c.setPosition(len(source[:start].encode('utf-16-le')) // 2)
        c.setPosition(len(source[:end].encode('utf-16-le')) // 2, QTextCursor.KeepAnchor)
        self.setTextCursor(c)
        self.centerCursor()

    def source_position(self):
        units = self.textCursor().position()
        return len(self.toPlainText().encode('utf-16-le')[:units*2].decode('utf-16-le'))

    def find_character(self,char,direction,till,count,repeat=False):
        c=self.textCursor();line=c.block().text();position=c.positionInBlock()
        found=position+(direction if till and repeat else 0)
        for _ in range(count):
            found=line.find(char,found+1) if direction>0 else line.rfind(char,0,found)
            if found<0:self.pending=self.count='';return
        target=found-direction if till else found
        base=c.block().position()
        operator=self.pending if self.pending in ('d','c','y') else ''
        keep=self.mode=='VISUAL' or bool(operator)
        if operator and direction>0:target+=1
        if operator and direction<0:c.setPosition(min(base+len(line),c.position()+1))
        c.setPosition(base+target,QTextCursor.KeepAnchor if keep else QTextCursor.MoveAnchor)
        if operator:
            self.register=c.selectedText().replace('\u2029','\n')
            if operator!='y':c.removeSelectedText()
            self.pending=''
            if operator=='c':self.set_mode('INSERT')
        self.setTextCursor(c)
        self.count=''

    def indent_lines(self,outdent=False):
        cursor=self.textCursor();start,end=cursor.selectionStart(),cursor.selectionEnd()
        first=self.document().findBlock(start);last=self.document().findBlock(end)
        if end>start and end==last.position():last=last.previous()
        first_number,last_number=first.blockNumber(),last.blockNumber()
        edit=QTextCursor(self.document());edit.beginEditBlock()
        block=last
        while block.isValid() and block.blockNumber()>=first_number:
            edit.setPosition(block.position())
            if outdent:
                text=block.text();count=1 if text.startswith('\t') else min(4,len(text)-len(text.lstrip(' ')))
                edit.setPosition(block.position()+count,QTextCursor.KeepAnchor);edit.removeSelectedText()
            else:edit.insertText('    ')
            block=block.previous()
        edit.endEditBlock()
        cursor.setPosition(self.document().findBlockByNumber(first_number).position())
        block=self.document().findBlockByNumber(last_number)
        cursor.setPosition(block.position()+len(block.text()),QTextCursor.KeepAnchor)
        self.setTextCursor(cursor);self.completer.popup().hide()

    def keyPressEvent(self, e):
        key, text, mods = e.key(), e.text(), e.modifiers()
        ctrl = bool(mods & Qt.ControlModifier)
        if key in (Qt.Key_Tab,Qt.Key_Backtab):
            cursor=self.textCursor()
            if cursor.hasSelection() or key==Qt.Key_Backtab or (self.vim and self.mode!='INSERT'):
                self.indent_lines(key==Qt.Key_Backtab or bool(mods & Qt.ShiftModifier));return
        if key == Qt.Key_Escape:
            self.find_wait=None
            self.completer.popup().hide()
            c = self.textCursor(); c.clearSelection(); self.setTextCursor(c)
            self.command_buffer = ''
            self.set_mode('NORMAL' if self.vim else 'INSERT')
            return
        if self.completer.popup().isVisible():
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                index = self.completer.popup().currentIndex()
                word = index.data() if index.isValid() else self.completer.currentCompletion()
                if word: self.complete(word)
                self.completer.popup().hide()
                return
            if key in (Qt.Key_Up, Qt.Key_Down):
                index = self.completer.popup().currentIndex()
                row = max(0, index.row()) + (1 if key == Qt.Key_Down else -1)
                model = self.completer.completionModel()
                row = max(0, min(row, model.rowCount()-1))
                self.completer.popup().setCurrentIndex(model.index(row,0))
                return
        if ctrl and key == Qt.Key_Space:
            self.popup(True); return
        if not self.vim or self.mode == 'INSERT':
            if key == Qt.Key_Tab:
                self.insertPlainText('    '); return
            if key in (Qt.Key_Return, Qt.Key_Enter):
                line = self.textCursor().block().text()
                indent = re.match(r'\s*', line)[0]
                before = line[:self.textCursor().positionInBlock()].rstrip()
                self.insertPlainText('\n' + indent + ('    ' if before.endswith('{') else ''))
                return
            super().keyPressEvent(e)
            if text and (text.isalnum() or text in '_$'): self.popup()
            else: self.completer.popup().hide()
            return
        if self.mode == 'COMMAND':
            if key in (Qt.Key_Return, Qt.Key_Enter):
                command = self.command_buffer
                self.command_buffer = ''
                self.set_mode('NORMAL')
                self.command.emit(command)
            elif key == Qt.Key_Backspace: self.command_buffer = self.command_buffer[:-1]
            else: self.command_buffer += text
            self.modeChanged.emit(':' + self.command_buffer if self.mode == 'COMMAND' else self.mode)
            return
        if self.find_wait and text:
            direction,till,count=self.find_wait
            self.find_wait=None
            self.last_find=(text,direction,till)
            self.find_character(text,direction,till,count)
            return
        if text in ('f','F','t','T'):
            self.find_wait=(1 if text.islower() else -1,text in ('t','T'),min(int(self.count or '1'),10000))
            self.count=''
            return
        if text in (';',',') and self.last_find:
            char,direction,till=self.last_find
            self.find_character(char,direction if text==';' else -direction,till,min(int(self.count or '1'),10000),repeat=True)
            self.count='';return
        if ctrl:
            if key == Qt.Key_R: self.redo(); return
            if key in (Qt.Key_D, Qt.Key_U):
                c = self.textCursor()
                c.movePosition(QTextCursor.Down if key == Qt.Key_D else QTextCursor.Up,
                               QTextCursor.MoveAnchor, max(1,self.viewport().height()//self.fontMetrics().height()//2))
                self.setTextCursor(c); return
            super().keyPressEvent(e); return
        if text == ':': self.set_mode('COMMAND'); return
        if text.isdigit() and (text != '0' or self.count): self.count += text; return
        n = min(int(self.count or '1'), 10000)
        c = self.textCursor()
        anchor = QTextCursor.KeepAnchor if self.mode == 'VISUAL' or self.pending in ('d','y','c') else QTextCursor.MoveAnchor
        motions = {'h':QTextCursor.Left,'j':QTextCursor.Down,'k':QTextCursor.Up,'l':QTextCursor.Right,
                   'w':QTextCursor.NextWord,'b':QTextCursor.PreviousWord,'e':QTextCursor.EndOfWord,
                   '0':QTextCursor.StartOfLine,'$':QTextCursor.EndOfLine,'G':QTextCursor.End}
        if text in motions:
            c.movePosition(motions[text], anchor, n)
            if self.pending:
                self.register = c.selectedText().replace('\u2029','\n')
                if self.pending in ('d','c'): c.removeSelectedText()
                change = self.pending == 'c'
                self.pending = ''
                if change: self.set_mode('INSERT')
            self.setTextCursor(c)
        elif text == 'g':
            if self.pending == 'g': c.movePosition(QTextCursor.Start, anchor); self.setTextCursor(c); self.pending=''
            else: self.pending='g'; return
        elif text in ('d','y','c'):
            if self.mode == 'VISUAL':
                self.register=c.selectedText().replace('\u2029','\n')
                if text != 'y': c.removeSelectedText()
                self.setTextCursor(c); self.set_mode('INSERT' if text=='c' else 'NORMAL')
            elif self.pending == text:
                c.movePosition(QTextCursor.StartOfLine)
                c.movePosition(QTextCursor.Down,QTextCursor.KeepAnchor,n)
                if not c.hasSelection(): c.movePosition(QTextCursor.EndOfLine,QTextCursor.KeepAnchor)
                self.register = c.selectedText().replace('\u2029','\n')
                if text != 'y': c.removeSelectedText()
                self.setTextCursor(c); self.set_mode('INSERT' if text=='c' else 'NORMAL')
            else: self.pending=text; return
        elif text in ('i','a','I','A','o','O'):
            if text == 'a': c.movePosition(QTextCursor.Right)
            if text in ('I','O'): c.movePosition(QTextCursor.StartOfLine)
            if text in ('A','o'): c.movePosition(QTextCursor.EndOfLine)
            if text == 'o': c.insertText('\n')
            if text == 'O': c.insertText('\n'); c.movePosition(QTextCursor.Up)
            self.setTextCursor(c); self.set_mode('INSERT')
        elif text == 'v': self.set_mode('VISUAL' if self.mode != 'VISUAL' else 'NORMAL')
        elif text == 'x':
            for _ in range(n): c.deleteChar()
        elif text == 'p': c.insertText(self.register)
        elif text == 'u': self.undo()
        elif text == '/':
            self.command_buffer='/'; self.set_mode('COMMAND')
        self.count = ''
