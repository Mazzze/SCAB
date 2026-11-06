"""Line icons drawn as SVG (24×24, 1.6 px stroke), rendered for normal/active/disabled states."""
from PySide6.QtCore import QByteArray, Qt, QRectF
from PySide6.QtGui import QIcon, QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer

NORMAL = '#c9ccd1'
ACTIVE = '#ffffff'
DISABLED = '#5d6066'
ACCENT = '#5c8ff2'

PATHS = {
    'new': '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><path d="M12 11v6M9 14h6"/>',
    'open': '<path d="M3 6h6l2 2h10v11H3z"/><path d="M3 10h18"/>',
    'save': '<path d="M4 4h13l3 3v13H4z"/><path d="M8 4v5h8V4"/><path d="M7 20v-6h10v6"/>',
    'undo': '<path d="M9 7 4 12l5 5"/><path d="M4 12h11a5 5 0 0 1 0 10h-3"/>',
    'redo': '<path d="m15 7 5 5-5 5"/><path d="M20 12H9a5 5 0 0 0 0 10h3"/>',
    'build': '<path d="M7 4v16l13-8z"/>',
    'stop': '<rect x="6" y="6" width="12" height="12"/>',
    'export': '<path d="M12 3v11"/><path d="m8 7 4-4 4 4"/><path d="M4 14v6h16v-6"/>',
    'export_parts': '<rect x="3" y="12" width="8" height="8"/><rect x="13" y="12" width="8" height="8"/><path d="M12 2v7"/><path d="m9 6 3 3 3-3"/>',
    'draft': '<path d="M12 3 20 8v8l-8 5-8-5V8z"/><path d="M12 3v18M4 8l16 8M20 8 4 16"/>',
    'auto': '<path d="M20 11a8 8 0 0 0-14.5-4.5"/><path d="M5 3v4h4"/><path d="M4 13a8 8 0 0 0 14.5 4.5"/><path d="M19 21v-4h-4"/>',
    'vim': '<rect x="3" y="6" width="18" height="12"/><path d="M7 10l2.5 5L12 10"/><path d="M15 10v5M15 10h2"/>',
    'inspect': '<path d="M4 4l7 17 2.5-7 7-2.5z"/>',
    'dimensions': '<path d="M3 17h18"/><path d="M3 14v6M21 14v6"/><path d="m6 17 2-2m-2 2 2 2M18 17l-2-2m2 2-2 2"/><path d="M6 4h12v6H6z"/>',
    'move': '<path d="M12 3v18M3 12h18"/><path d="m9 6 3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"/>',
    'rotate': '<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v5h-5"/><circle cx="12" cy="12" r="1.5"/>',
    'color': '<path d="M12 3c4 5 6 8 6 11a6 6 0 0 1-12 0c0-3 2-6 6-11z"/>',
    'duplicate': '<rect x="8" y="8" width="12" height="12"/><path d="M16 8V4H4v12h4"/>',
    'pattern': '<rect x="4" y="4" width="4" height="4"/><rect x="10" y="4" width="4" height="4"/><rect x="16" y="4" width="4" height="4"/><rect x="4" y="10" width="4" height="4"/><rect x="10" y="10" width="4" height="4"/><rect x="16" y="10" width="4" height="4"/><rect x="4" y="16" width="4" height="4"/><rect x="10" y="16" width="4" height="4"/><rect x="16" y="16" width="4" height="4"/>',
    'boolean': '<circle cx="9" cy="12" r="6"/><circle cx="15" cy="12" r="6"/>',
    'animate': '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l3 2M9 2h6"/>',
    'thread': '<path d="M9 3h6v4H9z"/><path d="M8 9l8 2M8 12l8 2M8 15l8 2M8 18l8 2"/><path d="M9 7v14M15 7v14"/>',
    'measure': '<path d="M3 15 15 3l6 6L9 21z"/><path d="m7 11 2 2M10 8l2 2M13 5l2 2"/>',
    'section': '<path d="M3 15 9 9h12l-6 6z"/><path d="M12 3v18" stroke-dasharray="2 2"/>',
    'kicad': '<rect x="6" y="6" width="12" height="12"/><path d="M9 3v3M12 3v3M15 3v3M9 18v3M12 18v3M15 18v3M3 9h3M3 12h3M3 15h3M18 9h3M18 12h3M18 15h3"/>',
    'kicad_import': '<rect x="7" y="9" width="10" height="10"/><path d="M10 19v2M14 19v2M10 7v2M14 7v2"/><path d="M12 2v8"/><path d="m9 7 3 3 3-3"/>',
    'kicad_export': '<rect x="7" y="9" width="10" height="10"/><path d="M10 19v2M14 19v2M5 12h2M5 16h2M17 12h2M17 16h2"/><path d="M12 13V3"/><path d="m9 6 3-3 3 3"/>',
    'clearance': '<path d="M3 20h18"/><rect x="6" y="11" width="5" height="9"/><path d="M15 20v-6h4v6"/><path d="M3 7h18" stroke-dasharray="2 2"/><path d="M8.5 8v2M17 8v4"/>',
    'fit': '<path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5"/><rect x="9" y="9" width="6" height="6"/>',
    'view_iso': '<path d="M12 3 20 7.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5 12 12l8-4.5M12 12v9"/>',
    'view_top': '<path d="M12 3 20 7.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5 12 12l8-4.5L12 3z" fill="{color}" fill-opacity="0.45"/>',
    'view_front': '<path d="M12 3 20 7.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5 12 12v9l-8-4.5z" fill="{color}" fill-opacity="0.45"/>',
    'view_right': '<path d="M12 3 20 7.5v9L12 21l-8-4.5v-9z"/><path d="M20 7.5 12 12v9l8-4.5z" fill="{color}" fill-opacity="0.45"/>',
    'camera': '<path d="M3 8h4l2-3h6l2 3h4v11H3z"/><circle cx="12" cy="13" r="3.5"/>',
    'help': '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .8-1 1.5v.7"/><path d="M12 17h.01"/>',
    'settings': '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    'error': '<circle cx="12" cy="12" r="9" stroke="#e5606d"/><path d="m9 9 6 6M15 9l-6 6" stroke="#e5606d"/>',
    'warning': '<path d="M12 3 22 20H2z" stroke="#d9a343"/><path d="M12 10v4M12 17h.01" stroke="#d9a343"/>',
    'info': '<circle cx="12" cy="12" r="9" stroke="#6c9ce0"/><path d="M12 11v6M12 7h.01" stroke="#6c9ce0"/>',
    'go_source': '<path d="m8 8-5 4 5 4M16 8l5 4-5 4"/><path d="m14 4-4 16"/>',
    'reload': '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
}

_cache = {}


def svg(name, color=NORMAL, size=24):
    body = PATHS[name].replace('{color}', color)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" '
            f'stroke="{color}" stroke-width="1.6" stroke-linecap="square" stroke-linejoin="miter">{body}</svg>')


def pixmap(name, color=NORMAL, size=18):
    from PySide6.QtGui import QGuiApplication
    ratio = QGuiApplication.instance().devicePixelRatio() if QGuiApplication.instance() else 1.
    renderer = QSvgRenderer(QByteArray(svg(name, color).encode()))
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    renderer.render(painter, QRectF(0, 0, size * ratio, size * ratio))
    painter.end()
    pm.setDevicePixelRatio(ratio)
    return pm


def icon(name, size=18):
    key = (name, size)
    if key not in _cache:
        result = QIcon()
        result.addPixmap(pixmap(name, NORMAL, size), QIcon.Normal, QIcon.Off)
        result.addPixmap(pixmap(name, ACTIVE, size), QIcon.Active, QIcon.Off)
        result.addPixmap(pixmap(name, ACTIVE, size), QIcon.Normal, QIcon.On)
        result.addPixmap(pixmap(name, DISABLED, size), QIcon.Disabled, QIcon.Off)
        _cache[key] = result
    return _cache[key]
