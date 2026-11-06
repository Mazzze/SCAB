"""Open a board or project in KiCad (PCB editor or project manager)."""
import os
from pathlib import Path
import shutil
import sys

VERSIONS = ('10.0', '9.0', '8.0', '7.0', '6.0')


def candidates(program):
    """Possible executables for 'pcbnew' (PCB editor) or 'kicad' (project manager)."""
    out = []
    configured = os.environ.get('SCAB_KICAD')
    if configured:
        path = Path(configured)
        out += [path] if path.is_file() else [path / program, path / f'{program}.exe', path / 'bin' / f'{program}.exe']
    found = shutil.which(program)
    if found:
        out.append(Path(found))
    if sys.platform.startswith('win'):
        for root in (os.environ.get('ProgramFiles', r'C:\Program Files'), os.environ.get('ProgramFiles(x86)', '')):
            for version in VERSIONS:
                if root:
                    out.append(Path(root) / 'KiCad' / version / 'bin' / f'{program}.exe')
        local = os.environ.get('LOCALAPPDATA')
        if local:
            for version in VERSIONS:
                out.append(Path(local) / 'Programs' / 'KiCad' / version / 'bin' / f'{program}.exe')
    elif sys.platform == 'darwin':
        app = Path('/Applications/KiCad/KiCad.app')
        out.append(app / 'Contents/Applications' / f'{program}.app' / 'Contents/MacOS' / program if program != 'kicad'
                   else app / 'Contents/MacOS/kicad')
    else:
        out += [Path('/usr/bin') / program, Path('/usr/local/bin') / program, Path('/app/bin') / program]
    return out


def find(program):
    return next((str(p) for p in candidates(program) if p.is_file() and os.access(p, os.X_OK)), None)


def command(path, project=False):
    """(executable, arguments) to open a board (PCB editor) or its project (KiCad), or None."""
    path = Path(path)
    if project:
        target = path.with_suffix('.kicad_pro')
        if not target.exists():
            target = path
        exe = find('kicad')
        if exe:
            return exe, [str(target)]
        return None
    exe = find('pcbnew')
    if exe:
        return exe, [str(path)]
    exe = find('kicad')        # the project manager also opens a board file
    if exe:
        return exe, [str(path.with_suffix('.kicad_pro')) if path.with_suffix('.kicad_pro').exists() else str(path)]
    return None


def open_in_kicad(path, project=False):
    """Start KiCad detached; falls back to the desktop's file association. Returns a status message."""
    from PySide6.QtCore import QProcess, QUrl
    from PySide6.QtGui import QDesktopServices
    path = Path(path)
    found = command(path, project)
    if found:
        exe, args = found
        if QProcess.startDetached(exe, args, str(path.parent))[0]:
            return f'Opened {Path(args[0]).name} in KiCad ({Path(exe).name}) · save there (Ctrl+S) and SCAB updates the board'
    target = path.with_suffix('.kicad_pro') if project and path.with_suffix('.kicad_pro').exists() else path
    if QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
        return f'Opened {target.name} with the system default application · save there and SCAB updates the board'
    return 'KiCad was not found. Install KiCad or set SCAB_KICAD to its bin folder.'
