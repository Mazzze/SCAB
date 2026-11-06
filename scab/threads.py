"""Metric ISO presets, modeled by the bundled BSD-licensed BOSL2 library."""
from pathlib import Path
import os,shutil

METRIC={'M2':.4,'M2.5':.45,'M3':.5,'M4':.7,'M5':.8,'M6':1.,'M8':1.25,'M10':1.5,'M12':1.75,'M16':2.,'M20':2.5,'M24':3.}


def install_library(source_file,project_root=None):
    root=Path(project_root) if project_root else Path(source_file).parent
    target=root/'lib'/'BOSL2'
    bundled=Path(__file__).resolve().parent.parent/'assets'/'BOSL2'
    if target.exists():
        stamp=target/'UPSTREAM.txt'
        if not stamp.is_file() or stamp.read_bytes()!=(bundled/'UPSTREAM.txt').read_bytes():
            raise ValueError('lib/BOSL2 already contains another library version. It was left unchanged; use a fresh project for the bundled thread tool.')
    else:
        import tempfile
        target.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.scab-bosl2-',dir=target.parent) as tmp:
            stage=Path(tmp)/'BOSL2';stage.mkdir()
            for file in bundled.iterdir():
                if file.is_file():shutil.copy2(file,stage/file.name)
            os.replace(stage,target)
    relative=os.path.relpath(target,Path(source_file).parent).replace(os.sep,'/')
    return f'include <{relative}/std.scad>\ninclude <{relative}/screws.scad>\n'


def thread_code(size='M6',pitch=1.,length=12.,margin=0.,internal=False):
    if size not in METRIC:raise ValueError('Unknown metric preset')
    pitch,length,margin=float(pitch),float(length),float(margin)
    diameter=float(size[1:])
    if not 0<pitch<diameter or length<=0 or abs(margin)>=diameter/2:raise ValueError('Invalid pitch, length or fit adjustment')
    spec=f'{size}x{pitch:g}'
    if internal:
        return f'screw_hole("{spec}", head="none", thread=true, length={length:g}, tolerance="6H", hole_oversize={margin:g}, anchor=BOTTOM, $slop=0);'
    return f'screw("{spec}", head="none", drive="none", length={length:g}, tolerance="6g", shaft_undersize={margin:g}, anchor=BOTTOM);'
