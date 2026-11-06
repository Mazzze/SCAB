"""Portable SCAD edits and matching preview transformations."""
import math
import re
import numpy as np
from .model import TOKEN
from .parameters import Scope,evaluate,argument,vector_parts,rotation

PATTERNS=('Linear X','Linear Y','Linear Z','Rectangular XY','Rectangular XZ','Rectangular YZ','Circular XY','Circular XZ','Circular YZ','Radial rings','Helix Z','Mirror X','Mirror Y','Mirror Z')


def pattern_matrices(kind,count=4,spacing=15.,rows=3,row_spacing=15.,angle=360.):
    if kind not in PATTERNS:raise ValueError('Unknown pattern')
    count,rows=int(count),int(rows)
    if not 1<=count<=200 or not 1<=rows<=200:raise ValueError('Count must be 1–200')
    if count*rows>2000 and kind in ('Rectangular XY','Rectangular XZ','Rectangular YZ','Radial rings'):raise ValueError('Pattern exceeds 2000 instances')
    matrices=[]
    def add(translation=None,angles=None,scale=None):
        m=np.eye(4)
        if translation is not None:m[:3,3]=translation
        if angles is not None:m[:3,:3]=rotation(angles)
        if scale is not None:m[:3,:3]=np.diag(scale)
        matrices.append(m)
    if kind.startswith('Linear'):
        axis='XYZ'.index(kind[-1])
        for i in range(count):
            t=np.zeros(3);t[axis]=i*spacing;add(t)
    elif kind.startswith('Rectangular'):
        a,b=map('XYZ'.index,kind[-2:])
        for j in range(rows):
            for i in range(count):
                t=np.zeros(3);t[a]=i*spacing;t[b]=j*row_spacing;add(t)
    elif kind.startswith('Circular'):
        axis={'XY':2,'XZ':1,'YZ':0}[kind[-2:]]
        for i in range(count):
            r=np.zeros(3);r[axis]=i*angle/count;add(angles=r)
    elif kind=='Radial rings':
        for j in range(rows):
            for i in range(count):
                a=i*angle/count;r=spacing+j*row_spacing
                add([r*math.cos(math.radians(a)),r*math.sin(math.radians(a)),0],[0,0,a])
    elif kind=='Helix Z':
        for i in range(count):
            a=i*angle/count
            add([spacing*math.cos(math.radians(a)),spacing*math.sin(math.radians(a)),i*row_spacing],[0,0,a])
    else:
        add();scale=np.ones(3);scale['XYZ'.index(kind[-1])]=-1;add(scale=scale)
    return matrices


def pattern_source(doc,node,kind,count,spacing,rows,row_spacing,angle):
    # Values may be ordinary SCAD expressions; the preview evaluates a safe subset.
    i,j='__scab_i','__scab_j'
    while re.search(r'\b'+i+r'\b',doc.source):i+='_' 
    while re.search(r'\b'+j+r'\b',doc.source):j+='_' 
    loop=f'for ({i}=[0:{int(count)-1}]) '
    if kind.startswith('Linear'):
        v=['0']*3;v['XYZ'.index(kind[-1])]=f'{i}*({spacing})';prefix=loop+'translate(['+', '.join(v)+'])'
    elif kind.startswith('Rectangular'):
        a,b=map('XYZ'.index,kind[-2:]);v=['0']*3;v[a]=f'{i}*({spacing})';v[b]=f'{j}*({row_spacing})'
        prefix=f'for ({j}=[0:{int(rows)-1}]) '+loop+'translate(['+', '.join(v)+'])'
    elif kind.startswith('Circular'):
        v=['0']*3;v[{'XY':2,'XZ':1,'YZ':0}[kind[-2:]]]=f'{i}*({angle})/{int(count)}';prefix=loop+'rotate(['+', '.join(v)+'])'
    elif kind=='Radial rings':
        prefix=f'for ({j}=[0:{int(rows)-1}]) '+loop+f'rotate([0,0,{i}*({angle})/{int(count)}]) translate([({spacing})+{j}*({row_spacing}),0,0])'
    elif kind=='Helix Z':
        prefix=loop+f'translate([0,0,{i}*({row_spacing})]) rotate([0,0,{i}*({angle})/{int(count)}]) translate([{spacing},0,0])'
    elif kind.startswith('Mirror'):
        v=[0,0,0];v['XYZ'.index(kind[-1])]=1
        body=doc.source[node.start:node.end]
        replacement='union() {\n'+body+'\nmirror('+str(v)+') {\n'+body+'\n}\n}'
        return doc.source[:node.start]+replacement+doc.source[node.end:]
    else:raise ValueError('Unknown pattern')
    return doc.wrap(node,prefix)


def _pose_values(matrix):
    """Decompose a rigid transform into OpenSCAD Rz * Ry * Rx Euler angles."""
    r=matrix[:3,:3]; y=math.asin(float(np.clip(-r[2,0],-1,1)))
    if abs(math.cos(y))>1e-8:
        x=math.atan2(r[2,1],r[2,2]);z=math.atan2(r[1,0],r[0,0])
    else:
        x=math.atan2(-r[1,2],r[1,1]);z=0.
    return matrix[:3,3],np.degrees([x,y,z])


def _vector(value):
    return '['+', '.join('0' if abs(float(v))<1e-9 else f'{float(v):.10g}' for v in value)+']'


def transform_source(doc,node,kind,axis,delta,pivot):
    if kind not in ('translate','rotate'):raise ValueError('Unknown transform')
    scope=Scope(doc.source,{name:(a,b) for name,a,b in doc.variables()})
    current=node
    while current.name=='color' and len(current.children)==1:current=current.children[0]
    # Preserve a user's existing direct translation-variable binding.
    if kind=='translate' and current.name=='translate':
        try:
            parts=vector_parts(doc,argument(doc,current,'v',0),scope)
            part,owner=parts[axis];expression=doc.source[part[0]:part[1]].strip()
            ref=owner.ref(*part)
            if ref and re.fullmatch(r'[$A-Za-z_][$\w]*',expression):
                value=float(evaluate(expression,owner))+delta
                return doc.source[:ref.start]+f'{value:.10g}'+doc.source[ref.end:]
        except (ValueError,TypeError,SyntaxError,KeyError,IndexError):pass
    # Recognize our existing pose by its two variable arguments. Update their
    # initializers instead of adding another wrapper. All output is standard SCAD.
    managed=None;matrix=np.eye(4)
    if node.name=='translate' and len(node.children)==1 and node.children[0].name=='rotate':
        turn=node.children[0]
        a,b=argument(doc,node,'v',0);c,d=argument(doc,turn,'a',0)
        pos,rot=doc.source[a:b].strip(),doc.source[c:d].strip()
        if re.fullmatch(r'[A-Za-z_]\w*_position',pos) and rot==pos[:-9]+'_rotation':
            try:
                for variable in (pos,rot):
                    a,b=scope.definition(variable)[0]
                    if any(re.fullmatch(r'[$A-Za-z_][$\w]*',t.value) for t in doc.tokens if a<=t.start<b):
                        raise ValueError('Preserve the existing pose equation')
                matrix[:3,3]=evaluate(pos,scope);matrix[:3,:3]=rotation(evaluate(rot,scope));managed=(pos,rot)
            except (ValueError,KeyError,TypeError):pass
    colors=[]
    if managed is None:
        matrix=np.eye(4)
        current=node
        while True:
            if current.name=='group' and len(current.children)==1 and current.children[0].kind=='geometry':
                current=current.children[0];continue
            if current.name=='color' and len(current.children)==1:
                child=current.children[0];colors.append(doc.source[current.start:child.start].rstrip());current=child;continue
            if current.name not in ('translate','rotate') or len(current.children)!=1:break
            try:
                a,b=argument(doc,current,'v' if current.name=='translate' else 'a',0)
                expression=doc.source[a:b]
                # Only collapse literal transforms. Keep variables and equations
                # in the original subtree so later edits and animations still work.
                if any(re.fullmatch(r'[$A-Za-z_][$\w]*',t.value) for t in doc.tokens if a<=t.start<b):break
                value=np.asarray(evaluate(expression,scope),dtype=float)
                if value.shape!=(3,):break
                local=np.eye(4)
                if current.name=='translate':local[:3,3]=value
                else:local[:3,:3]=rotation(value)
                matrix=matrix@local;current=current.children[0]
            except (ValueError,TypeError,SyntaxError,KeyError):break
    delta_matrix=np.eye(4)
    if kind=='translate':delta_matrix[axis,3]=float(delta)
    else:
        angles=np.zeros(3);angles[axis]=float(delta);delta_matrix[:3,:3]=rotation(angles)
        p=np.asarray(pivot,dtype=float);delta_matrix[:3,3]=p-delta_matrix[:3,:3]@p
    position,angles=_pose_values(delta_matrix@matrix)
    if managed:
        replacements=[]
        for name,value in zip(managed,(position,angles)):
            _,a,b=next(v for v in doc.variables() if v[0]==name)
            replacements.append((a,b,' '+_vector(value)))
        result=doc.source
        for a,b,text in sorted(replacements,reverse=True):result=result[:a]+text+result[b:]
        return result
    # Prefer the module call's name, with collision-free identifiers per object.
    probe=current
    while len(probe.children)==1:probe=probe.children[0]
    base=probe.name if re.fullmatch(r'[A-Za-z_]\w*',probe.name) else 'object'
    if base=='group':base='object'
    stem=base;number=2
    while re.search(r'\b'+re.escape(stem)+r'_(?:position|rotation)\b',doc.source):stem=f'{base}_{number}';number+=1
    pos,rot=stem+'_position',stem+'_rotation'
    removed=doc.source[node.start:current.start]+doc.source[current.end:node.end]
    comments=[m[0] for m in TOKEN.finditer(removed) if m[0].startswith(('//','/*')) and m[0] not in '\n'.join(colors)]
    body='\n'.join(comments+colors+[doc.source[current.start:current.end]])
    text=f'{pos} = {_vector(position)};\n{rot} = {_vector(angles)};\ntranslate({pos}) rotate({rot}) {{\n{body}\n}}'
    return doc.source[:node.start]+text+doc.source[node.end:]


def color_source(doc,node,color):
    # The outermost applicable color wins in OpenSCAD, even when the selected
    # tree item is a descendant. Edit that call instead of adding an inner color.
    applicable=[n for n in doc.walk() if n.name=='color' and n.start<=node.start and node.end<=n.end]
    target=min(applicable,key=lambda n:n.start) if applicable else None
    if target is None:
        current=node
        while len(current.children)==1:
            current=current.children[0]
            if current.name=='color':target=current;break
    if target is not None:
        span=argument(doc,target,'c',0)
        if span:
            expression=doc.source[span[0]:span[1]].strip()
            variable=next((v for v in doc.variables() if v[0]==expression),None)
            if variable:_,a,b=variable;span=(a,b)
            return doc.source[:span[0]]+'"'+color+'"'+doc.source[span[1]:]
    return doc.wrap(node,'color("'+color+'")')
