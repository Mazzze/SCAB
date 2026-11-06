from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QToolButton,QSizePolicy

class Collapsible(QWidget):
    def __init__(self,title,content,collapsed=False,parent=None):
        super().__init__(parent)
        self.content=content
        self.button=QToolButton();self.button.setText(title);self.button.setCheckable(True)
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.button.setStyleSheet('QToolButton{background:#2b2d30;border:0;border-top:1px solid #393b40;padding:4px 6px;text-align:left;color:#9a9da3;} QToolButton:hover{color:#e6e7e9;}')
        self.button.setChecked(not collapsed)
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.setSpacing(1)
        layout.addWidget(self.button);layout.addWidget(content)
        self.button.toggled.connect(self.set_expanded);self.set_expanded(not collapsed)
    def set_expanded(self,expanded):
        self.content.setVisible(expanded)
        self.button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        if self.button.isChecked()!=expanded:self.button.setChecked(expanded)
    @property
    def collapsed(self):return not self.button.isChecked()
