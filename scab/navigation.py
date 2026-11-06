"""View cube: click a face, edge or corner to snap the camera; drag to orbit."""
import math
import numpy as np
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QPainter, QColor, QPen, QPolygonF, QFont
from PySide6.QtWidgets import QWidget

AXIS_COLORS = ('#e05a5a', '#5cb85c', '#5b8def')
FACE_NAMES = {(0, 1): 'RIGHT', (0, -1): 'LEFT', (1, 1): 'BACK', (1, -1): 'FRONT', (2, 1): 'TOP', (2, -1): 'BOTTOM'}
SIZE = 112
SCALE = 30


class ViewCube(QWidget):
    def __init__(self, viewer):
        super().__init__(viewer)
        self.viewer = viewer
        self.setFixedSize(SIZE, SIZE)
        self.polygons, self.vertices, self.edges = [], [], []
        self.press = None
        self.moved = False
        self.hover = None
        self.setMouseTracking(True)
        self.setToolTip('Click a face, edge or corner to look from there · drag to orbit')

    def reposition(self):
        self.move(max(0, self.parent().width() - self.width() - 8), 8)
        self.raise_()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        font = QFont()
        font.setPointSizeF(7.5)
        font.setBold(True)
        p.setFont(font)
        center = SIZE / 2
        matrix = np.array(self.viewer.viewMatrix().data()).reshape(4, 4).T[:3, :3]
        corners = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], float)
        camera = corners @ matrix.T
        screen = np.column_stack([center + camera[:, 0] * SCALE, center - camera[:, 1] * SCALE])
        self.polygons, self.vertices, faces = [], [], []
        for axis in range(3):
            for sign in (-1, 1):
                normal = np.eye(3)[axis] * sign
                direction = matrix @ normal
                if direction[2] <= 0:
                    continue
                ids = np.where(corners[:, axis] == sign)[0]
                middle = screen[ids].mean(axis=0)
                angles = np.arctan2(screen[ids, 1] - middle[1], screen[ids, 0] - middle[0])
                ids = ids[np.argsort(angles)]
                poly = QPolygonF([QPointF(*point) for point in screen[ids]])
                faces.append((camera[ids, 2].mean(), poly, normal, FACE_NAMES[(axis, sign)], axis, float(direction[2])))
        for depth, poly, normal, label, axis, facing in sorted(faces, key=lambda f: f[0]):
            shade = int(70 + 45 * facing)
            hovered = self.hover is not None and np.allclose(self.hover, normal)
            p.setBrush(QColor(60, 95, 160) if hovered else QColor(shade, shade + 2, shade + 6, 235))
            p.setPen(QPen(QColor('#17181b'), 1))
            p.drawPolygon(poly)
            bounds = poly.boundingRect()
            if facing > .35 and p.fontMetrics().horizontalAdvance(label) + 4 < bounds.width():
                p.setPen(QColor('#e6e7e9'))
                c = bounds.center()
                p.drawText(QRectF(c.x() - 30, c.y() - 8, 60, 16), Qt.AlignCenter, label)
            self.polygons.append((poly, normal))
        # small axis tripod at the back-left-bottom corner
        origin = screen[0]
        for axis in range(3):
            tip = origin + (screen[[4, 2, 1][axis]] - origin) * .45
            p.setPen(QPen(QColor(AXIS_COLORS[axis]), 2))
            p.drawLine(QPointF(*origin), QPointF(*tip))
        for i, point in enumerate(screen):
            self.vertices.append((QPointF(*point), corners[i]))
        self.edges = []
        for i in range(8):
            for j in range(i + 1, 8):
                if bin(i ^ j).count('1') == 1:
                    self.edges.append((QPointF(*((screen[i] + screen[j]) / 2)), (corners[i] + corners[j]) / 2))

    def snap(self, normal):
        n = np.asarray(normal, dtype=float)
        n /= np.linalg.norm(n)
        self.viewer.set_roll(0.)
        self.viewer.setCameraPosition(elevation=math.degrees(math.asin(n[2])), azimuth=math.degrees(math.atan2(n[1], n[0])))

    def target(self, point):
        nearest = min(self.vertices, key=lambda entry: (entry[0] - point).manhattanLength(), default=None)
        if nearest and (nearest[0] - point).manhattanLength() < 10:
            return nearest[1]
        edge = min(self.edges, key=lambda entry: (entry[0] - point).manhattanLength(), default=None)
        if edge and (edge[0] - point).manhattanLength() < 7:
            return edge[1]
        for poly, normal in reversed(self.polygons):
            if poly.containsPoint(point, Qt.OddEvenFill):
                return normal
        return None

    def mousePressEvent(self, e):
        self.press = e.position()
        self.last = e.position()
        self.moved = False

    def mouseMoveEvent(self, e):
        if self.press is None:
            target = self.target(e.position())
            hover = target if target is not None and np.count_nonzero(target) == 1 else None
            if (hover is None) != (self.hover is None) or (hover is not None and not np.allclose(hover, self.hover)):
                self.hover = hover
                self.update()
            return
        diff = e.position() - self.last
        self.last = e.position()
        if (e.position() - self.press).manhattanLength() > 3:
            self.moved = True
        if self.moved:
            self.viewer.orbit(-diff.x() * .5, diff.y() * .5)

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mouseReleaseEvent(self, e):
        if not self.moved:
            target = self.target(e.position())
            if target is not None:
                self.snap(target)
        self.press = None
