"""Background OpenSCAD compiler. No geometry work runs on the GUI thread."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import numpy as np
import trimesh
from PySide6.QtCore import QThread, Signal
from .model import Document,Node
from .materials import SceneMesh,material_plan,combine_material_meshes,DEFAULT_COLOR


def find_openscad():
    configured = os.environ.get('OPENSCAD_BINARY')
    candidates = [configured, shutil.which('openscad.com'), shutil.which('openscad'),
                  '/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD',
                  r'C:\Program Files\OpenSCAD\openscad.com', r'C:\Program Files\OpenSCAD\openscad.exe']
    return next((p for p in candidates if p and Path(p).is_file()), None)


class Cancelled(Exception): pass


def run_openscad(source, directory, executable, output, t, cancel, timeout=180):
    path = None
    try:
        # Same directory as original: relative include/use/import paths remain valid.
        with tempfile.NamedTemporaryFile(mode='w', suffix='.scad', prefix='.scadforge-',
                                         dir=directory, encoding='utf-8', delete=False) as f:
            # OpenSCAD 2021.01 -D '$t=...' is applied too late for ordinary
            # assignments such as angle=180*$t. Put time in the source scope
            # before variable initialization; keep source line numbers intact.
            doc = Document(source)
            timed_source = source
            assignments = [(a,b) for name,a,b in doc.variables() if name=='$t']
            if assignments:
                for a,b in reversed(assignments):
                    timed_source = timed_source[:a]+f' {t:.12g}'+timed_source[b:]
            else:
                timed_source = f'$t={t:.12g}; '+timed_source
            f.write(timed_source)
            path = f.name
        env = os.environ.copy()
        env['QT_QPA_PLATFORM'] = 'offscreen'
        proc = subprocess.Popen([executable, '-o', str(output), path],
                                cwd=directory, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        start = time.monotonic()
        while True:
            try:
                log, _ = proc.communicate(timeout=0.15)
                break
            except subprocess.TimeoutExpired:
                if cancel.is_set() or time.monotonic()-start > timeout:
                    proc.kill()
                    proc.communicate()
                    if cancel.is_set(): raise Cancelled()
                    raise RuntimeError(f'OpenSCAD exceeded {timeout} seconds. Simplify the model.')
        if cancel.is_set(): raise Cancelled()
        if proc.returncode:
            # An empty intersection is a valid scene, not necessarily an error.
            if 'Current top level object is empty' in log and not any(x in log for x in ('ERROR:', 'Parser error')):
                return log
            raise RuntimeError(log[-16000:] or f'OpenSCAD exit {proc.returncode}')
        return log
    finally:
        if path: Path(path).unlink(missing_ok=True)


def load_mesh(path):
    if not path.exists() or path.stat().st_size == 0: return None
    mesh = trimesh.load(path, force='mesh', process=False)
    if len(mesh.faces) == 0: return None
    # STL has per-face vertices. Merge once, outside interaction/rendering.
    mesh.merge_vertices()
    return np.asarray(mesh.vertices, dtype=np.float32), np.asarray(mesh.faces, dtype=np.uint32)


class CompileJob(QThread):
    ready = Signal(int, object, float, str)
    failed = Signal(int, str)
    progress = Signal(str)
    totalProgress = Signal(int,str)

    def __init__(self, generation, source, directory, executable, times, export_path=None, project_snapshot=None,draft=False):
        super().__init__()
        self.generation, self.source = generation, source
        self.directory, self.executable = directory, executable
        self.times, self.export_path = times, export_path
        self.cancelled = threading.Event()
        self.project_snapshot = project_snapshot
        self.draft=bool(draft)

    def cancel(self): self.cancelled.set()

    def run(self):
        start = time.monotonic()
        try:
            doc = Document(self.source)
            logs, frames = [], []
            memory_bytes = 0
            from .native_engine import find_engine,native_compile
            cache_dir=Path(os.environ.get('SCADFORGE_CACHE_DIR') or (Path(self.project_snapshot['root'])/'.scadforge-cache' if self.project_snapshot else Path(self.directory)/'.scadforge-cache'))
            self.totalProgress.emit(0,'Preparing project')
            with tempfile.TemporaryDirectory(prefix='scadforge-mesh-') as tmp:
                if self.project_snapshot:
                    snapshot=self.project_snapshot
                    stage=Path(tmp)/'project'
                    def copy_file(source,destination):
                        if self.cancelled.is_set():raise Cancelled()
                        return shutil.copy2(source,destination)
                    shutil.copytree(snapshot['root'],stage,ignore=shutil.ignore_patterns('.*','__pycache__'),copy_function=copy_file)
                    for relative,text in snapshot['overrides'].items():
                        output=stage/relative;output.parent.mkdir(parents=True,exist_ok=True);output.write_text(text,encoding='utf-8')
                    self.directory=str((stage/snapshot['entry']).parent)
                if not self.export_path and find_engine():
                    frames,logs=native_compile(self,doc,cache_dir)
                    self.ready.emit(self.generation,frames,time.monotonic()-start,logs[-24000:])
                    return
                preview_roots=doc.roots or [Node(0,0,'empty')]
                total=max(1,len(self.times)*len(preview_roots))
                for frame, t in enumerate(self.times):
                    scene = []
                    if self.export_path:
                        self.totalProgress.emit(5,'Exporting exact full-resolution geometry')
                        destination = Path(self.export_path)
                        fd, name = tempfile.mkstemp(dir=destination.parent, prefix='.scadforge-export-', suffix=destination.suffix or '.stl')
                        os.close(fd)
                        temporary_export = Path(name)
                        temporary_export.unlink()
                        try:
                            logs.append(run_openscad(self.source, self.directory, self.executable,
                                                     temporary_export, t, self.cancelled))
                            if not temporary_export.exists():
                                raise RuntimeError('The section/model is empty; the existing export was left unchanged. Move the plane through a solid.')
                            os.replace(temporary_export, destination)
                        finally:
                            temporary_export.unlink(missing_ok=True)
                    else:
                        for i, node in enumerate(preview_roots):
                            if self.cancelled.is_set(): raise Cancelled()
                            self.progress.emit(f'Compiling frame {frame+1}/{len(self.times)} · object {i+1}/{len(doc.roots)}')
                            base=(frame*len(preview_roots)+i)/total
                            self.totalProgress.emit(int(98*base),'Python fallback · expanding object')
                            isolated=doc.isolated(node)
                            # CSG is evaluated by OpenSCAD, including modules in
                            # other files, children(), expressions, loops and $t.
                            expanded=Path(tmp)/f'{i}.csg'
                            expanded.unlink(missing_ok=True)
                            logs.append(run_openscad(isolated,self.directory,self.executable,expanded,t,self.cancelled))
                            if self.cancelled.is_set():raise Cancelled()
                            evaluated=expanded.read_text(encoding='utf-8')
                            if self.draft:
                                import re
                                evaluated=re.sub(r'\$fn\s*=\s*([0-9]+(?:\.[0-9]+)?)',lambda m:'$fn='+str(min(24,float(m[1]))),evaluated)
                            plan=material_plan(evaluated)
                            for warning in plan.warnings:logs.append('WARNING: Material preview: '+warning)
                            output=Path(tmp)/f'{i}.stl';output.unlink(missing_ok=True)
                            logs.append(run_openscad(evaluated,self.directory,self.executable,output,t,self.cancelled))
                            mesh=load_mesh(output)
                            if mesh is not None:
                                render=None;palette=None
                                if len(plan.parts)>1:
                                    material_meshes=[]
                                    for index,(color,geometry) in enumerate(plan.parts):
                                        self.totalProgress.emit(int(98*(base+(.4+.55*index/max(1,len(plan.parts)))/total)),f'Python fallback · material {index+1}/{len(plan.parts)}')
                                        if self.cancelled.is_set():raise Cancelled()
                                        self.progress.emit(f'Frame {frame+1}/{len(self.times)} · object {i+1}/{len(doc.roots)} · material {index+1}/{len(plan.parts)}')
                                        part=Path(tmp)/f'{i}-material.stl';part.unlink(missing_ok=True)
                                        logs.append(run_openscad(geometry,self.directory,self.executable,part,t,self.cancelled))
                                        result=load_mesh(part)
                                        if result is not None:material_meshes.append((color,result))
                                    render=combine_material_meshes(material_meshes)
                                    palette=tuple(color for color,_ in material_meshes)
                                chunk=SceneMesh(node.start,node.end,*mesh,color=plan.parts[0][0] if len(plan.parts)==1 else DEFAULT_COLOR,render=render,palette=palette)
                                memory_bytes+=chunk.nbytes
                                if memory_bytes>256*1024*1024:
                                    raise RuntimeError('Mesh cache exceeded 256 MiB. Reduce frame count or $fn and bake again.')
                                scene.append(chunk)
                            self.totalProgress.emit(int(98*(base+1/total)),'Object ready')
                    frames.append(scene)
            self.totalProgress.emit(99,'Uploading GPU meshes')
            self.ready.emit(self.generation, frames, time.monotonic()-start, '\n'.join(logs)[-24000:])
        except Cancelled:
            pass
        except Exception as e:
            self.failed.emit(self.generation, str(e))
