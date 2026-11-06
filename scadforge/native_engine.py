"""Bridge to the optional Rust scheduler; the Qt worker owns all UI signals."""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
import numpy as np
from .materials import SceneMesh,combine_material_meshes,DEFAULT_COLOR
from .model import Document,Node


def find_engine():
    if os.environ.get('SCADFORGE_USE_RUST','1')=='0':return None
    root=Path(__file__).resolve().parent.parent
    candidates=[os.environ.get('SCADFORGE_ENGINE'),root/'bin/scadforge-engine.exe',root/'bin/scadforge-engine',
                root/'rust-engine/target/release/scadforge-engine.exe',root/'rust-engine/target/release/scadforge-engine']
    return next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()),None)


def timed_source(source,t):
    doc=Document(source);assignments=[(a,b) for name,a,b in doc.variables() if name=='$t']
    if not assignments:return f'$t={t:.12g}; '+source
    for a,b in reversed(assignments):source=source[:a]+f' {t:.12g}'+source[b:]
    return source


def stop_tree(proc):
    if proc.poll() is not None:return
    if os.name=='nt':
        subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:os.killpg(proc.pid,signal.SIGTERM)
        except ProcessLookupError:pass
    try:proc.wait(timeout=3)
    except subprocess.TimeoutExpired:proc.kill();proc.wait()


def native_compile(job,doc,cache_dir):
    from .compiler import Cancelled,load_mesh
    engine=find_engine()
    request={'executable':str(Path(job.executable).resolve()),'directory':str(Path(job.directory).resolve()),
             'cache':str(Path(cache_dir).resolve()),'draft':job.draft,
             'workers':max(1,min(8,int(os.environ.get('SCADFORGE_WORKERS','2')))),
             'frames':[[{'start':n.start,'end':n.end,'source':timed_source(doc.isolated(n),t)} for n in (doc.roots or [Node(0,0,'empty')])] for t in job.times]}
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
    proc=subprocess.Popen([engine],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',**options)
    messages=queue.Queue()
    def reader():
        try:
            for line in proc.stdout:messages.put(line)
        finally:messages.put(None)
    # stdin can exceed pipe capacity for a large project; write while readers run.
    def writer():
        try:proc.stdin.write(json.dumps(request));proc.stdin.close()
        except (BrokenPipeError,OSError):pass
    errors=[]
    def read_errors():errors.append(proc.stderr.read())
    threads=[threading.Thread(target=f,daemon=True) for f in (reader,writer,read_errors)]
    for thread in threads:thread.start()
    result=None;error=None
    try:
        while True:
            if job.cancelled.is_set():stop_tree(proc);raise Cancelled()
            try:line=messages.get(timeout=.1)
            except queue.Empty:continue
            if line is None:break
            message=json.loads(line)
            if message['event']=='progress':
                job.progress.emit(message['phase']);job.totalProgress.emit(int(message['percent']),message['phase'])
            elif message['event']=='done':result=message
            elif message['event']=='error':error=message['message']
        proc.wait()
        for thread in threads:thread.join(timeout=1)
        if job.cancelled.is_set():raise Cancelled()
        if proc.returncode or error or result is None:raise RuntimeError(error or ''.join(errors) or f'Rust engine exited {proc.returncode} without a result')
        loaded={}
        def mesh(index):
            if index not in loaded:
                path=result['meshes'][index]['path'];loaded[index]=load_mesh(Path(path)) if path else None
            return loaded[index]
        frames=[];memory=0
        for frame in result['frames']:
            scene=[]
            for root in frame:
                solid=mesh(root['solid'])
                if solid is None:continue
                parts=[(tuple(color),mesh(index)) for color,index in root['parts']]
                parts=[(c,m) for c,m in parts if m is not None]
                render=combine_material_meshes(parts) if len(parts)>1 else None
                chunk=SceneMesh(root['start'],root['end'],*solid,color=parts[0][0] if parts else DEFAULT_COLOR,
                                render=render,palette=[c for c,_ in parts])
                memory+=chunk.nbytes
                if memory>256*1024*1024:raise RuntimeError('Mesh cache exceeded 256 MiB. Reduce frames or detail.')
                scene.append(chunk)
            frames.append(scene)
        cached=sum(m['cached'] for m in result['meshes'])
        logs=f"Rust engine · {result['backend']} · {cached}/{len(result['meshes'])} meshes cached\n"+result['logs']
        job.totalProgress.emit(99,'Uploading GPU meshes')
        return frames,logs
    finally:
        if proc.poll() is None:stop_tree(proc)
