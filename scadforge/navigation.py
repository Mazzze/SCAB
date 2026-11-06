"""Small clickable view cube, axis/corner snapping, drag orbit and camera roll."""
import math
import numpy as np
from PySide6.QtCore import Qt,QPointF,QRectF
from PySide6.QtGui import QPainter,QColor,QPen,QPolygonF,QFont
from PySide6.QtWidgets import QWidget

AXIS_COLORS=('#ff6476','#69e292','#6aafff')

class ViewCube(QWidget):
    def __init__(self,viewer):
        super().__init__(viewer);self.viewer=viewer;self.minimized=False
        self.setFixedSize(154,182);self.polygons=[];self.vertices=[];self.edges=[];self.press=None;self.moved=False
        self.setToolTip('Click a face, edge or corner to snap. Drag to orbit. Bottom arrows roll. − minimizes.')
    def set_minimized(self,value):
        self.minimized=bool(value);self.setFixedSize(56,26) if value else self.setFixedSize(154,182)
        self.reposition();self.update()
    def reposition(self):self.move(max(0,self.parent().width()-self.width()-10),10);self.raise_()
    def paintEvent(self,e):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(17,27,39,232));p.setPen(QPen(QColor('#506076'),1));p.drawRoundedRect(QRectF(self.rect()).adjusted(1,1,-1,-1),7,7)
        p.setFont(QFont('DejaVu Sans',8));p.setPen(QColor('#d8e6ef'))
        if self.minimized:p.drawText(self.rect(),Qt.AlignCenter,'XYZ ▸');return
        p.drawText(9,18,'VIEW CUBE');p.drawText(134,18,'−')
        v=self.viewer;matrix=np.array(v.viewMatrix().data()).reshape(4,4).T[:3,:3]
        corners=np.array([[x,y,z] for x in (-1,1) for y in (-1,1) for z in (-1,1)],float)
        camera=corners@matrix.T
        screen=np.column_stack([77+camera[:,0]*33,86-camera[:,1]*33])
        self.polygons=[];self.vertices=[]
        faces=[]
        for axis in range(3):
            for sign in (-1,1):
                normal=np.eye(3)[axis]*sign;direction=matrix@normal
                if direction[2]<=0:continue
                ids=np.where(corners[:,axis]==sign)[0]
                center=screen[ids].mean(axis=0);angles=np.arctan2(screen[ids,1]-center[1],screen[ids,0]-center[0])
                ids=ids[np.argsort(angles)]
                poly=QPolygonF([QPointF(*point) for point in screen[ids]])
                label=('+' if sign>0 else '−')+'XYZ'[axis]
                faces.append((camera[ids,2].mean(),poly,normal,label,axis))
        for depth,poly,normal,label,axis in sorted(faces,key=lambda f:f[0]):
            base=QColor(AXIS_COLORS[axis]);base.setAlpha(205)
            p.setBrush(base);p.setPen(QPen(QColor('#0c1420'),2));p.drawPolygon(poly)
            center=poly.boundingRect().center();p.setPen(QColor('#07101b'));p.drawText(QRectF(center.x()-25,center.y()-12,50,24),Qt.AlignCenter,label)
            self.polygons.append((poly,normal))
        for i,point in enumerate(screen):self.vertices.append((QPointF(*point),corners[i]))
        self.edges=[]
        for i in range(8):
            for j in range(i+1,8):
                if bin(i^j).count('1')==1:self.edges.append((QPointF(*((screen[i]+screen[j])/2)),(corners[i]+corners[j])/2))
        for axis in range(3):
            p.setPen(QColor(AXIS_COLORS[axis]));p.drawText(15+axis*46,148,'XYZ'[axis])
        p.setPen(QColor('#d8e6ef'));p.drawText(12,172,'↶ −15°');p.drawText(94,172,'+15° ↷')
    def snap(self,normal):
        n=np.asarray(normal,dtype=float);n/=np.linalg.norm(n)
        self.viewer.set_roll(0.)
        self.viewer.setCameraPosition(elevation=math.degrees(math.asin(n[2])),azimuth=math.degrees(math.atan2(n[1],n[0])))
    def mousePressEvent(self,e):self.press=e.position();self.last=e.position();self.moved=False
    def mouseMoveEvent(self,e):
        if self.press is None:return
        diff=e.position()-self.last;self.last=e.position()
        if (e.position()-self.press).manhattanLength()>3:self.moved=True
        if self.moved:self.viewer.orbit(-diff.x()*.5,diff.y()*.5)
    def mouseReleaseEvent(self,e):
        if self.moved:self.press=None;return
        point=e.position()
        if self.minimized:self.set_minimized(False)
        elif point.y()<26 and point.x()>124:self.set_minimized(True)
        elif point.y()>154:self.viewer.set_roll(self.viewer.roll+(-15 if point.x()<77 else 15))
        else:
            # Corner snaps use a screen-space threshold; face interiors snap to axes.
            nearest=min(self.vertices,key=lambda entry:(entry[0]-point).manhattanLength(),default=None)
            if nearest and (nearest[0]-point).manhattanLength()<12:self.snap(nearest[1])
            else:
                edge=min(self.edges,key=lambda entry:(entry[0]-point).manhattanLength(),default=None)
                if edge and (edge[0]-point).manhattanLength()<8:self.snap(edge[1]);self.press=None;return
                for poly,normal in reversed(self.polygons):
                    if poly.containsPoint(point,Qt.OddEvenFill):self.snap(normal);break
        self.press=None
