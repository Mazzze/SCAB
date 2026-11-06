"""Bridge to the Rust scheduler; the Qt worker owns all UI signals."""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import numpy as np
from .materials import SceneMesh, DEFAULT_COLOR
from .meshio import load_mesh, instance_mesh
from .model import Document, Node

MARK = 9876500


def find_engine():
    if os.environ.get('SCADFORGE_USE_RUST', '1') == '0':
        return None
    root = Path(__file__).resolve().parent.parent
    candidates = [os.environ.get('SCADFORGE_ENGINE'), root / 'bin/scadforge-engine.exe', root / 'bin/scadforge-engine',
                  root / 'rust-engine/target/release/scadforge-engine.exe', root / 'rust-engine/target/release/scadforge-engine']
    return next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), None)


def timed_source(source, t):
    doc = Document(source)
    assignments = [(a, b) for name, a, b in doc.variables() if name == '$t']
    if not assignments:
        return f'$t={t:.12g}; ' + source
    for a, b in reversed(assignments):
        source = source[:a] + f' {t:.12g}' + source[b:]
    return source


def marked_source(source, roots):
    """Wrap each top-level object so its evaluated CSG can be found again.

    No newlines are added: OpenSCAD's error line numbers stay valid.
    """
    for index in range(len(roots) - 1, -1, -1):
        node = roots[index]
        source = (source[:node.start] + f'render(convexity = {MARK + index}) {{ ' + source[node.start:node.end]
                  + ' }' + source[node.end:])
    return source


def stop_tree(proc):
    if proc.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def default_workers():
    configured = os.environ.get('SCADFORGE_WORKERS')
    if configured:
        return max(1, min(32, int(configured)))
    return max(1, min(16, (os.cpu_count() or 2)))


def assemble(parts, meshes):
    """Combine instanced parts into one (vertices, faces, face_colors, palette)."""
    groups = {}
    for color, index, matrix in parts:
        mesh = meshes[index]
        if mesh is None:
            continue
        groups.setdefault(index, []).append((tuple(color), matrix))
    vertices, faces, colors = [], [], []
    offset = 0
    for index, instances in groups.items():
        v, f = meshes[index]
        mats = [np.asarray(m, dtype=np.float64).reshape(4, 4) if m is not None else None for _, m in instances]
        placed_v, placed_f = instance_mesh(v, f, mats, offset)
        vertices.append(placed_v)
        faces.append(placed_f)
        offset += len(placed_v)
        per = np.asarray([c for c, _ in instances], dtype=np.float32)
        colors.append(np.repeat(per, len(f), axis=0))
    if not vertices:
        return None
    face_colors = np.vstack(colors)
    palette = list(dict.fromkeys(tuple(map(float, c)) for c in (color for color, _, _ in parts)))
    return np.vstack(vertices), np.vstack(faces).astype(np.uint32), face_colors, palette


def native_compile(job, doc, cache_dir):
    from .compiler import Cancelled
    engine = find_engine()
    roots = doc.roots or [Node(0, 0, 'empty')]
    source = marked_source(doc.source, doc.roots) if doc.roots else doc.source
    request = {'executable': str(Path(job.executable).resolve()), 'directory': str(Path(job.directory).resolve()),
               'cache': str(Path(cache_dir).resolve()), 'draft': job.draft, 'workers': default_workers(),
               'max_tasks': int(os.environ.get('SCADFORGE_MAX_TASKS', '384')),
               'frames': [{'source': timed_source(source, t), 'roots': [{'start': n.start, 'end': n.end} for n in roots]}
                          for t in job.times]}
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen([engine], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', **options)
    messages = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:
                messages.put(line)
        finally:
            messages.put(None)

    # stdin can exceed pipe capacity for a large project; write while readers run.
    def writer():
        try:
            proc.stdin.write(json.dumps(request))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    errors = []

    def read_errors():
        errors.append(proc.stderr.read())
    threads = [threading.Thread(target=f, daemon=True) for f in (reader, writer, read_errors)]
    for thread in threads:
        thread.start()
    result = None
    error = None
    try:
        while True:
            if job.cancelled.is_set():
                stop_tree(proc)
                raise Cancelled()
            try:
                line = messages.get(timeout=.1)
            except queue.Empty:
                continue
            if line is None:
                break
            message = json.loads(line)
            if message['event'] == 'progress':
                job.progress.emit(message['phase'])
                job.totalProgress.emit(int(message['percent']), message['phase'])
            elif message['event'] == 'done':
                result = message
            elif message['event'] == 'error':
                error = message['message']
        proc.wait()
        for thread in threads:
            thread.join(timeout=1)
        if job.cancelled.is_set():
            raise Cancelled()
        if proc.returncode or error or result is None:
            raise RuntimeError(error or ''.join(errors) or f'Rust engine exited {proc.returncode} without a result')
        job.totalProgress.emit(98, 'Loading meshes')
        meshes = [None] * len(result['meshes'])
        needed = sorted({part['mesh'] for frame in result['frames'] for root in frame for part in root['parts']})
        for index in needed:
            if job.cancelled.is_set():
                raise Cancelled()
            path = result['meshes'][index]['path']
            meshes[index] = load_mesh(Path(path)) if path else None
        limit = int(os.environ.get('SCADFORGE_MAX_MESH_MB', '1536')) * 1024 * 1024
        frames = []
        memory = 0
        for frame in result['frames']:
            scene = []
            for root in frame:
                parts = [(p['color'], p['mesh'], p['matrix']) for p in root['parts']]
                combined = assemble(parts, meshes)
                if combined is None:
                    continue
                vertices, faces, face_colors, palette = combined
                render = (vertices, faces, face_colors) if len(palette) > 1 else None
                chunk = SceneMesh(root['start'], root['end'], vertices, faces,
                                  color=palette[0] if palette else DEFAULT_COLOR, render=render, palette=palette)
                memory += chunk.nbytes
                if memory > limit:
                    raise RuntimeError(f'Meshes exceed {limit // 1048576} MiB. Enable draft mesh, reduce frames or $fn '
                                       '(or raise SCADFORGE_MAX_MESH_MB).')
                scene.append(chunk)
            frames.append(scene)
        cached = sum(m['cached'] for m in result['meshes'])
        logs = (f"Rust engine · {result['backend']} · {result.get('pieces', 0)} pieces · "
                f"{len(result['meshes'])} unique meshes ({result.get('instanced', 0)} instanced) · {cached} cached\n"
                + result.get('expand_logs', '') + result['logs'])
        job.totalProgress.emit(99, 'Uploading GPU meshes')
        return frames, logs
    finally:
        if proc.poll() is None:
            stop_tree(proc)
