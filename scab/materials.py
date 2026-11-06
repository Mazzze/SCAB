"""Material partitions of OpenSCAD's evaluated CSG, never of unevaluated modules.

OpenSCAD resolves files, module parameters, loops, $t and color expressions.
Geometry remains the CLI's responsibility. Source files are never rewritten.
"""
from collections import OrderedDict
from dataclasses import dataclass
import numpy as np
import re
from .model import Document
from .parameters import Scope,evaluate

DEFAULT_COLOR=(.72,.74,.78,1.)


@dataclass(frozen=True)
class MaterialPlan:
    parts:tuple
    warnings:tuple=()


class SceneMesh(tuple):
    """Keep the exact solid for picking/sections and batched materials for drawing.

    Four-item tuple compatibility is intentional: all existing solid operations
    continue to use (source_start, source_end, vertices, faces).
    """
    def __new__(cls,start,end,vertices,faces,*,color=DEFAULT_COLOR,render=None,palette=None):
        obj=super().__new__(cls,(start,end,vertices,faces))
        obj.color=tuple(color)
        obj.palette=tuple(palette) if palette is not None else (obj.color,)
        obj.render=render  # (vertices, faces, per-face RGBA), or None for uniform
        return obj

    @property
    def nbytes(self):
        shared=(self[2],self[3])
        extra=sum(a.nbytes for a in self.render if not any(a is b for b in shared)) if self.render is not None else 0
        return self[2].nbytes+self[3].nbytes+extra


def combine_material_meshes(parts):
    vertices=[];faces=[];colors=[];offset=0
    for color,(v,f) in parts:
        vertices.append(v);faces.append(f+offset);offset+=len(v)
        colors.append(np.tile(np.asarray(color,dtype=np.float32),(len(f),1)))
    if not vertices:return None
    return np.vstack(vertices),np.vstack(faces).astype(np.uint32),np.vstack(colors)


def material_plan(source):
    doc=Document(source);scope=Scope(source,{})
    warnings=[]
    def children(node):
        items=node.children
        if len(items)==1 and source[items[0].start:items[0].start+1]=='{':return items[0].children
        return items
    def header(node):
        # CSG consists of evaluated calls; preserve every numeric argument,
        # polyhedron point, imported path and tessellation parameter verbatim.
        return source[node.start:node.children[0].start].rstrip() if node.children else source[node.start:node.end]
    def code(node):return source[node.start:node.end]
    def block(name,body):return name+' {\n'+'\n'.join(body)+'\n}'
    def merge(layers):
        out=OrderedDict()
        for layer in layers:
            for color,body in layer.items():out.setdefault(color,[]).append(body)
        return OrderedDict((c,items[0] if len(items)==1 else block('union()',items)) for c,items in out.items())
    def resolved_color(node,inherited):
        if inherited is not None:return inherited
        match=re.search(r'color\s*\(\s*(\[[^\]]*\])',header(node))
        if match is None:raise ValueError('Unrecognized evaluated CSG color')
        value=evaluate(match[1],scope)
        if len(value) not in (3,4):raise ValueError('Invalid evaluated CSG color')
        if min(value[:3])<0:return None  # OpenSCAD's unset/default color sentinel
        return tuple(float(np.clip(x,0,1)) for x in value)+( (1.,) if len(value)==3 else ())
    def walk(node,inherited=None):
        if source[node.start:node.start+1] in ('%','*'):return OrderedDict()
        items=children(node);name=node.name
        if name=='color':
            return merge(walk(c,resolved_color(node,inherited)) for c in items)
        if name in ('group','union'):
            return merge(walk(c,inherited) for c in items)
        if not items:
            if name in ('difference','intersection','hull','minkowski'):return OrderedDict()
            return OrderedDict([(inherited or DEFAULT_COLOR,code(node))])
        if name in ('difference','intersection'):
            # Cutters constrain every material in the first solid. Their colors
            # do not introduce positive geometry or override the base material.
            base=walk(items[0],inherited)
            return OrderedDict((color,block(name+'()',[body]+[code(c) for c in items[1:]])) for color,body in base.items())
        layers=merge(walk(c,inherited) for c in items)
        if len(layers)<=1:
            return OrderedDict((color,code(node)) for color in layers)
        if name in ('hull','minkowski','offset','fill','resize','roof'):
            # These can create surfaces with no unique source material. Keep the
            # exact solid and make the first-operand convention visible in logs.
            warnings.append(f'{name} combines materials; its generated solid uses the first material. Put color() outside to choose another.')
            return OrderedDict([(next(iter(layers)),code(node))])
        # Affine transforms and extrusions distribute over the material pieces.
        # A color wrapper was already removed above, so ancestors keep precedence.
        return OrderedDict((color,block(header(node),[body])) for color,body in layers.items())
    parts=merge(walk(node) for node in doc.roots)
    return MaterialPlan(tuple(parts.items()),tuple(dict.fromkeys(warnings)))
