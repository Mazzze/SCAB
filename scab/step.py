"""STEP 3D models via cascadio (OpenCASCADE): STEP -> GLB -> NumPy meshes.

cascadio is optional (``pip install cascadio``). Without it, KiCad footprints
that reference .step models fall back to a sibling .wrl or a courtyard box.
GLB from OpenCASCADE is in metres; meshes are returned in millimetres.
"""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import numpy as np

COMPONENT = {5126: np.float32, 5125: np.uint32, 5123: np.uint16, 5121: np.uint8, 5122: np.int16, 5120: np.int8}
WIDTH = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}
DEFAULT_COLOR = (.7, .7, .72, 1.)


def available():
    try:
        import cascadio  # noqa: F401
        return True
    except ImportError:
        return False


def read_glb(data):
    """[(vertices (n,3) float32, faces (m,3) uint32, rgba)] grouped by color, in GLB units."""
    if data[:4] != b'glTF':
        raise ValueError('Not a GLB file')
    offset = 12
    document, binary = None, b''
    while offset < len(data):
        length, kind = struct.unpack('<II', data[offset:offset + 8])
        chunk = data[offset + 8:offset + 8 + length]
        if kind == 0x4E4F534A:
            document = json.loads(chunk)
        elif kind == 0x004E4942:
            binary = chunk
        offset += 8 + length
    if document is None:
        raise ValueError('GLB without JSON chunk')
    views = document.get('bufferViews', [])
    accessors = document.get('accessors', [])
    materials = document.get('materials', [])

    def accessor(index):
        a = accessors[index]
        view = views[a['bufferView']]
        dtype = np.dtype(COMPONENT[a['componentType']])
        width = WIDTH[a['type']]
        start = view.get('byteOffset', 0) + a.get('byteOffset', 0)
        stride = view.get('byteStride', 0)
        count = a['count']
        if stride and stride != dtype.itemsize * width:
            raw = np.frombuffer(binary, np.uint8, count=stride * (count - 1) + dtype.itemsize * width, offset=start)
            rows = [np.frombuffer(raw[i * stride:i * stride + dtype.itemsize * width].tobytes(), dtype) for i in range(count)]
            return np.vstack(rows)
        return np.frombuffer(binary, dtype, count=count * width, offset=start).reshape(count, width) if width > 1 else \
            np.frombuffer(binary, dtype, count=count, offset=start)

    def color(primitive):
        index = primitive.get('material')
        if index is None or index >= len(materials):
            return DEFAULT_COLOR
        factor = materials[index].get('pbrMetallicRoughness', {}).get('baseColorFactor', list(DEFAULT_COLOR))
        return tuple(round(float(v), 4) for v in factor[:4])

    def local(node):
        if 'matrix' in node:
            return np.asarray(node['matrix'], dtype=float).reshape(4, 4).T
        m = np.eye(4)
        if 'scale' in node:
            m = np.diag(list(node['scale']) + [1.]) @ m
        if 'rotation' in node:
            x, y, z, w = node['rotation']
            r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                          [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                          [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
            rot = np.eye(4)
            rot[:3, :3] = r
            m = rot @ m
        if 'translation' in node:
            t = np.eye(4)
            t[:3, 3] = node['translation']
            m = t @ m
        return m

    groups = {}
    nodes = document.get('nodes', [])

    def visit(index, parent):
        node = nodes[index]
        matrix = parent @ local(node)
        if 'mesh' in node:
            for primitive in document['meshes'][node['mesh']].get('primitives', []):
                if primitive.get('mode', 4) != 4 or 'POSITION' not in primitive.get('attributes', {}):
                    continue
                positions = accessor(primitive['attributes']['POSITION']).astype(np.float64)
                indices = accessor(primitive['indices']).astype(np.int64).reshape(-1, 3) if 'indices' in primitive else \
                    np.arange(len(positions)).reshape(-1, 3)
                placed = (np.c_[positions, np.ones(len(positions))] @ matrix.T)[:, :3]
                groups.setdefault(color(primitive), []).append((placed, indices))
        for child in node.get('children', []):
            visit(child, matrix)
    scenes = document.get('scenes') or [{'nodes': list(range(len(nodes)))}]
    roots = scenes[document.get('scene', 0)].get('nodes', [])
    for root in roots:
        visit(root, np.eye(4))
    out = []
    for rgba, parts in groups.items():
        vertices, faces, shift = [], [], 0
        for v, f in parts:
            vertices.append(v)
            faces.append(f + shift)
            shift += len(v)
        out.append((np.vstack(vertices).astype(np.float32), np.vstack(faces).astype(np.uint32), rgba))
    return out


def load(path, cache_dir=None, tolerance=0.02):
    """Meshes of a STEP file in millimetres (converted once, cached by content)."""
    import cascadio
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:24]
    cache_dir = Path(cache_dir) if cache_dir else Path(tempfile.gettempdir()) / 'scab-step'
    cache_dir.mkdir(parents=True, exist_ok=True)
    glb = cache_dir / f'{digest}.glb'
    if not glb.exists():
        partial = glb.with_suffix('.partial.glb')
        # tolerance in metres: 0.02 mm chord error is plenty for clearance work
        cascadio.step_to_glb(str(path), str(partial), tolerance / 1000., 0.35, False, True, True, False, set(), True)
        partial.replace(glb)
    meshes = read_glb(glb.read_bytes())
    return [(v * 1000., f, c) for v, f, c in meshes]
