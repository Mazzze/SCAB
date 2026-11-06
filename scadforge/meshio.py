"""Fast STL reading and instancing with NumPy (no per-triangle Python loops)."""
from pathlib import Path
import re
import numpy as np

STL_RECORD = np.dtype([('normal', '<f4', (3,)), ('v', '<f4', (3, 3)), ('attr', '<u2')])


def weld(triangles):
    """(n*3, 3) float32 corner positions -> unique vertices + (n, 3) faces."""
    tri = np.ascontiguousarray(triangles, dtype=np.float32)
    if not len(tri):
        return None
    keys = tri.view(np.dtype((np.void, tri.dtype.itemsize * 3))).ravel()
    _, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    vertices = tri[first]
    faces = inverse.reshape(-1, 3).astype(np.uint32)
    return vertices, faces


def read_stl(path):
    data = Path(path).read_bytes()
    if len(data) >= 84:
        count = int.from_bytes(data[80:84], 'little')
        if 84 + 50 * count == len(data):
            records = np.frombuffer(data, STL_RECORD, count=count, offset=84)
            return weld(records['v'].reshape(-1, 3))
    text = data.decode('utf-8', errors='replace')
    numbers = re.findall(r'vertex\s+(\S+)\s+(\S+)\s+(\S+)', text)
    if not numbers:
        return None
    return weld(np.asarray(numbers, dtype=np.float32))


def load_mesh(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return None
    result = read_stl(path)
    if result is None or not len(result[1]):
        return None
    return result


def instance_mesh(vertices, faces, matrices, offset=0):
    """Place one mesh under K matrices (None = identity) in a single vectorized step."""
    vertices = np.asarray(vertices, dtype=np.float64)
    k = len(matrices)
    stack = np.empty((k, 4, 4))
    for i, m in enumerate(matrices):
        stack[i] = np.eye(4) if m is None else m
    placed = np.einsum('kij,nj->kni', stack[:, :3, :3], vertices) + stack[:, None, :3, 3]
    n = len(vertices)
    shifted = faces[None, :, :].astype(np.int64) + (offset + np.arange(k) * n)[:, None, None]
    return placed.reshape(-1, 3).astype(np.float32), shifted.reshape(-1, 3).astype(np.uint32)
