from PySide6.QtWidgets import (QDialog,QFormLayout,QComboBox,QSpinBox,QDoubleSpinBox,QLineEdit,
    QLabel,QDialogButtonBox,QPushButton,QHBoxLayout)
from PySide6.QtCore import QTimer
from .operations import PATTERNS,pattern_matrices,pattern_source
from .parameters import Scope,evaluate
from .threads import METRIC,thread_code

class PatternDialog(QDialog):
    def __init__(self,window,doc,node):
        super().__init__(window);self.window,self.doc,self.node=window,doc,node
        self.setWindowTitle('Pattern · live light preview');layout=QFormLayout(self)
        self.kind=QComboBox();self.kind.addItems(PATTERNS);layout.addRow('Pattern',self.kind)
        self.count=QSpinBox();self.count.setRange(1,200);self.count.setValue(4);layout.addRow('Count / per ring',self.count)
        self.rows=QSpinBox();self.rows.setRange(1,200);self.rows.setValue(3);layout.addRow('Rows / rings',self.rows)
        self.spacing=QLineEdit('15');layout.addRow('Spacing / radial distance (mm)',self.spacing)
        self.row_spacing=QLineEdit('15');layout.addRow('Row spacing / helix rise per copy (mm)',self.row_spacing)
        self.angle=QLineEdit('360');layout.addRow('Total rotation (degrees)',self.angle)
        self.status=QLabel('First 64 instances shown as a light preview; Apply writes ordinary for/translate/rotate.');self.status.setWordWrap(True);layout.addRow(self.status)
        controls=QDialogButtonBox(QDialogButtonBox.Apply|QDialogButtonBox.Cancel);layout.addRow(controls)
        controls.button(QDialogButtonBox.Apply).clicked.connect(self.apply);controls.rejected.connect(self.reject)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.timeout.connect(self.preview)
        for widget in (self.kind,self.count,self.rows):
            signal=widget.currentIndexChanged if isinstance(widget,QComboBox) else widget.valueChanged;signal.connect(lambda *_:self.timer.start(70))
        for widget in (self.spacing,self.row_spacing,self.angle):widget.textChanged.connect(lambda *_:self.timer.start(100))
        self.result_source=None;self.preview()
    def values(self):
        scope=Scope(self.doc.source,{name:(a,b) for name,a,b in self.doc.variables()},t=self.window.timeline.value()/1000)
        return (self.kind.currentText(),self.count.value(),float(evaluate(self.spacing.text(),scope)),self.rows.value(),float(evaluate(self.row_spacing.text(),scope)),float(evaluate(self.angle.text(),scope)))
    def preview(self):
        try:
            matrices=pattern_matrices(*self.values());self.window.viewer.preview_pattern(self.node.start,matrices)
            self.status.setText(f'{len(matrices)} instances · first {min(64,len(matrices))} shown · preview leaves source unchanged')
        except (ValueError,TypeError,SyntaxError,ZeroDivisionError) as error:self.status.setText(str(error));self.window.viewer.clear_ghosts()
    def apply(self):
        if self.window.editor.toPlainText()!=self.doc.source:
            self.status.setText('Source changed. Close and reopen the pattern tool before applying.');return
        try:
            pattern_matrices(*self.values())
            self.result_source=pattern_source(self.doc,self.node,self.kind.currentText(),self.count.value(),self.spacing.text(),self.rows.value(),self.row_spacing.text(),self.angle.text())
            self.accept()
        except (ValueError,TypeError,SyntaxError,ZeroDivisionError) as error:self.status.setText(str(error))
    def done(self,result):self.timer.stop();self.window.viewer.clear_ghosts();super().done(result)


class ThreadDialog(QDialog):
    def __init__(self,parent):
        super().__init__(parent);self.setWindowTitle('Modeled ISO metric thread · BOSL2')
        layout=QFormLayout(self)
        self.size=QComboBox();self.size.addItems(METRIC);self.size.setCurrentText('M6');layout.addRow('Metric size',self.size)
        self.pitch=QDoubleSpinBox();self.pitch.setRange(.1,10);self.pitch.setDecimals(3);self.pitch.setValue(1.);layout.addRow('Pitch (mm)',self.pitch)
        self.length=QDoubleSpinBox();self.length.setRange(.1,1000);self.length.setValue(12);layout.addRow('Length (mm)',self.length)
        self.margin=QDoubleSpinBox();self.margin.setRange(-5,5);self.margin.setDecimals(3);layout.addRow('Signed diametral fit adjustment (mm)',self.margin)
        self.mode=QComboBox();self.mode.addItems(['External thread / male','Internal threaded hole mask','Cut internal thread from selected object']);layout.addRow('Operation',self.mode)
        self.size.currentTextChanged.connect(lambda name:self.pitch.setValue(METRIC[name]))
        label=QLabel('Positive adjustment: smaller male / larger hole. Negative: tighter fit. Standard tolerance 6g / 6H. Cut starts at selected object XY center / bottom Z. Uses bundled BOSL2; project lib/ must travel with the .scad files.');label.setWordWrap(True);layout.addRow(label)
        self.status=QLabel('');layout.addRow(self.status)
        controls=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel);layout.addRow(controls)
        controls.accepted.connect(self.validate);controls.rejected.connect(self.reject)
        self.code=None
    def validate(self):
        try:self.code=thread_code(self.size.currentText(),self.pitch.value(),self.length.value(),self.margin.value(),self.mode.currentIndex()!=0);self.accept()
        except ValueError as error:self.status.setText(str(error))
