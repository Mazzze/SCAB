"""Safe measurement handles for a deliberately bounded subset of OpenSCAD.
No eval(), arbitrary Python execution, or inferred bounding-box edits.
"""
import ast
from dataclasses import dataclass
import math
import re
import numpy as np
from .model import TOKEN

NUMBER = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z')
IDENT = re.compile(r'[$A-Za-z_][$\w]*\Z')


def clean(expression):
    return ''.join(m[0] if not m[0].startswith(('//','/*')) else ' ' for m in TOKEN.finditer(expression)).strip()


def evaluate(expression, scope):
    expression = re.sub(r'\$([A-Za-z_]\w*)',r'__scad_\1',clean(expression))
    tree = ast.parse(expression,mode='eval')
    if sum(1 for _ in ast.walk(tree)) > 300: raise ValueError('Expression too complex')
    funcs={'sin':lambda a:math.sin(math.radians(a)), 'cos':lambda a:math.cos(math.radians(a)),
           'tan':lambda a:math.tan(math.radians(a)), 'sqrt':math.sqrt, 'abs':abs,
           'min':min,'max':max,'floor':math.floor,'ceil':math.ceil,'pow':lambda a,b:a**b if abs(b)<12 else float('nan')}
    def visit(n):
        if isinstance(n,ast.Constant) and isinstance(n.value,(int,float,bool)): return n.value
        if isinstance(n,ast.Name):
            if n.id in ('true','false'): return n.id=='true'
            name='$'+n.id[7:] if n.id.startswith('__scad_') else n.id
            return scope.value(name)
        if isinstance(n,(ast.List,ast.Tuple)): return [visit(v) for v in n.elts]
        if isinstance(n,ast.UnaryOp):
            v=visit(n.operand)
            if isinstance(n.op,ast.USub): return -v
            if isinstance(n.op,ast.UAdd): return v
        if isinstance(n,ast.BinOp):
            a,b=visit(n.left),visit(n.right)
            if not isinstance(a,(int,float)) or not isinstance(b,(int,float)): raise ValueError('Non-scalar arithmetic')
            if isinstance(n.op,ast.Add): return a+b
            if isinstance(n.op,ast.Sub): return a-b
            if isinstance(n.op,ast.Mult): return a*b
            if isinstance(n.op,ast.Div): return a/b
            if isinstance(n.op,ast.Mod): return a%b
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in funcs and not n.keywords:
            return funcs[n.func.id](*(visit(v) for v in n.args))
        raise ValueError('Unsupported expression')
    result=visit(tree.body)
    if not np.isfinite(np.asarray(result,dtype=float)).all(): raise ValueError('Non-finite expression')
    if np.max(np.abs(np.asarray(result,dtype=float)),initial=0)>1e9: raise ValueError('Measurement too large')
    return result


@dataclass
class Ref:
    start:int
    end:int
    name:str


class Scope:
    def __init__(self,source,definitions,parent=None,t=0,bindings=None,constants=None):
        self.source,self.definitions,self.parent,self.t=source,definitions,parent,t
        self.bindings=bindings or {}
        self.constants=constants or {}
        self.active=set()
    def value(self,name):
        if name=='$t': return self.t
        if name in self.constants: return self.constants[name]
        if name in self.bindings:
            span,owner=self.bindings[name]
            key=(id(owner),name)
            if key in self.active: raise ValueError('Cyclic parameter')
            self.active.add(key)
            try: return evaluate(self.source[span[0]:span[1]],owner)
            finally: self.active.remove(key)
        if name in self.definitions:
            if name in self.active: raise ValueError('Cyclic definition')
            self.active.add(name)
            try:
                a,b=self.definitions[name]
                return evaluate(self.source[a:b],self)
            finally: self.active.remove(name)
        if self.parent: return self.parent.value(name)
        raise ValueError(f'Unknown variable {name}')
    def definition(self,name):
        if name in self.definitions: return self.definitions[name],self
        if self.parent: return self.parent.definition(name)
        raise ValueError('Unknown variable')
    def binding(self,name):
        if name in self.definitions or name in self.constants: return None
        if name in self.bindings: return self.bindings[name]
        return self.parent.binding(name) if self.parent else None
    def ref(self,a,b):
        key=('ref',a,b)
        if key in self.active: return None
        self.active.add(key)
        try: return self._ref(a,b)
        finally: self.active.remove(key)
    def _ref(self,a,b):
        expression=clean(self.source[a:b])
        if IDENT.fullmatch(expression):
            bound=self.binding(expression)
            if bound:
                span,owner=bound
                if owner is self and clean(self.source[span[0]:span[1]])==expression: return None
                return owner.ref(*span)
            (start,end),owner=self.definition(expression)
            # Derived/animated variables remain equations, not overwritten by dragging.
            raw=clean(self.source[start:end])
            if not NUMBER.fullmatch(raw): return None
            ts=[m for m in TOKEN.finditer(self.source[start:end]) if not m[0].isspace() and not m[0].startswith(('//','/*'))]
            return Ref(start+ts[0].start(),start+ts[-1].end(),expression)
        if NUMBER.fullmatch(expression):
            ts=[m for m in TOKEN.finditer(self.source[a:b]) if not m[0].isspace() and not m[0].startswith(('//','/*'))]
            return Ref(a+ts[0].start(),a+ts[-1].end(),'literal')
        return None


def split_spans(source,a,b):
    tokens=[(m[0],a+m.start(),a+m.end()) for m in TOKEN.finditer(source[a:b])
            if not m[0].isspace() and not m[0].startswith(('//','/*'))]
    if not tokens: return []
    parts=[]; depth=0; start=tokens[0][1]
    for v,begin,end in tokens:
        if v in ('(','[','{'): depth+=1
        elif v in (')',']','}'): depth-=1
        elif v==',' and depth==0:
            parts.append((start,begin)); start=end
    parts.append((start,tokens[-1][2]))
    return parts


def call_args(doc,node):
    ts=[t for t in doc.tokens if node.start<=t.start<node.end]
    i=next(i for i,t in enumerate(ts) if t.value=='(')
    depth=0
    for j in range(i,len(ts)):
        if ts[j].value=='(': depth+=1
        elif ts[j].value==')':
            depth-=1
            if depth==0: break
    named={}; positional=[]
    for a,b in split_spans(doc.source,ts[i].end,ts[j].start):
        raw=doc.source[a:b]
        match=re.match(r'\s*([$A-Za-z_][$\w]*)\s*=(?!=)',raw)
        if match: named[match[1]]=(a+match.end(),b)
        else: positional.append((a,b))
    return named,positional


def argument(doc,node,key,index,default=None):
    named,pos=call_args(doc,node)
    return named.get(key,pos[index] if index<len(pos) else default)


def val(doc,span,scope,default=None):
    return evaluate(doc.source[span[0]:span[1]],scope) if span else default


def vector_parts(doc,span,scope):
    a,b=span
    text=clean(doc.source[a:b])
    if IDENT.fullmatch(text):
        binding=scope.binding(text)
        span,scope=binding if binding else scope.definition(text)
        return vector_parts(doc,span,scope)
    tokens=[t for t in doc.tokens if a<=t.start<b]
    if tokens and tokens[0].value=='[' and tokens[-1].value==']':
        return [(part,scope) for part in split_spans(doc.source,tokens[0].end,tokens[-1].start)]
    return [(span,scope)]*3


def rotation(euler):
    x,y,z=np.radians(np.asarray(euler,dtype=float))
    cx,cy,cz=np.cos([x,y,z]); sx,sy,sz=np.sin([x,y,z])
    rx=np.array([[1,0,0],[0,cx,-sx],[0,sx,cx]])
    ry=np.array([[cy,0,sy],[0,1,0],[-sy,0,cy]])
    rz=np.array([[cz,-sz,0],[sz,cz,0],[0,0,1]])
    return rz@ry@rx


@dataclass
class Handle:
    label:str
    value:float
    ref:Ref
    origin:np.ndarray
    direction:np.ndarray
    ring:object=None
    node_start:int=0
    def point(self,value=None): return self.origin+self.direction*(self.value if value is None else value)
    def replace(self,source,value):
        value=max(.01,float(value))
        return source[:self.ref.start]+f'{value:.6g}'+source[self.ref.end:]


def handles_for(doc,selected,t=0):
    if selected is None: return []
    scope=Scope(doc.source,{name:(a,b) for name,a,b in doc.variables()},t=t)
    modules={}
    for n in doc.nodes:
        if n.name=='module':
            ts=[tok for tok in doc.tokens if n.start<=tok.start<n.end]
            depth=0; ending=None
            for token in ts[2:]:
                if token.value=='(': depth+=1
                elif token.value==')':
                    depth-=1
                    if depth==0: ending=token.start; break
            params=[]
            if ending is not None:
                for a,b in split_spans(doc.source,ts[2].end,ending):
                    match=re.match(r'\s*([$A-Za-z_][$\w]*)\s*(?:=\s*)?',doc.source[a:b])
                    if not match: break
                    default=(a+match.end(),b) if '=' in match[0] else None
                    params.append((match[1],default))
                modules[ts[1].value]=(n.children[0],params)
    handles=[]
    def descend(node,matrix,current_scope,depth=0,enabled=False):
        if depth>20 or len(handles)>=36: return
        enabled=enabled or node.start==selected.start
        try:
            if node.kind=='definition': return
            if node.name=='group':
                defs={}
                for child in node.children:
                    toks=[q for q in doc.tokens if child.start<=q.start<child.end]
                    if len(toks)>2 and toks[1].value=='=': defs[toks[0].value]=(toks[1].end,toks[-1].start)
                if defs: current_scope=Scope(doc.source,defs,current_scope,t=t)
            local=np.eye(4)
            if node.name in ('translate','rotate','scale','mirror','multmatrix'):
                span=argument(doc,node,'a' if node.name=='rotate' else 'm' if node.name=='multmatrix' else 'v',0)
                vector=val(doc,span,current_scope)
                if node.name=='translate': local[:3,3]=np.asarray(vector,dtype=float)
                elif node.name=='rotate':
                    if np.isscalar(vector):
                        named,_=call_args(doc,node)
                        axis=np.asarray(val(doc,named.get('v'),current_scope,[0,0,1]),dtype=float)
                        axis/=np.linalg.norm(axis); x,y,z=axis
                        skew=np.array([[0,-z,y],[z,0,-x],[-y,x,0]])
                        rad=math.radians(vector)
                        local[:3,:3]=np.eye(3)*math.cos(rad)+(1-math.cos(rad))*np.outer(axis,axis)+math.sin(rad)*skew
                    else: local[:3,:3]=rotation(vector)
                elif node.name=='scale': local[:3,:3]=np.diag([vector]*3 if np.isscalar(vector) else vector)
                elif node.name=='mirror':
                    axis=np.asarray(vector,dtype=float); axis/=np.linalg.norm(axis)
                    local[:3,:3]=np.eye(3)-2*np.outer(axis,axis)
                else: local=np.asarray(vector,dtype=float)
                matrix=matrix@local
            def add(label,span,origin,direction,ring=None,owner=None):
                owner=owner or current_scope
                if not span: return
                ref=owner.ref(*span)
                if not ref: return
                value=float(val(doc,span,owner))
                if value<=0: return
                start=(matrix@np.r_[origin,1])[:3]
                delta=matrix[:3,:3]@np.asarray(direction)
                world_ring=None
                if ring is not None: world_ring=tuple(matrix[:3,:3]@np.asarray(b) for b in ring)
                name=ref.name if ref.name!='literal' else label
                handles.append(Handle(f'{name} · {label}',value,ref,start,delta,world_ring,node.start))
            if enabled and node.name in ('cylinder','sphere'):
                named,pos=call_args(doc,node)
                centered=bool(val(doc,named.get('center'),current_scope,False))
                hspan=named.get('h',pos[0] if pos and node.name=='cylinder' else None)
                height=float(val(doc,hspan,current_scope,1)) if node.name=='cylinder' else 0
                z=0 if centered else height/2
                if node.name=='sphere':
                    key='d' if 'd' in named else 'r'
                    span=named.get(key,pos[0] if pos else None)
                    factor=.5 if key=='d' else 1.
                    add(key,span,[0,0,0],[factor,0,0],([factor,0,0],[0,factor,0]))
                else:
                    if 'd' in named or 'r' in named:
                        key='d' if 'd' in named else 'r'; factor=.5 if key=='d' else 1.
                        add(key,named[key],[0,0,z],[factor,0,0],([factor,0,0],[0,factor,0]))
                    else:
                        for radius_key,diam_key,index,level in [('r1','d1',1,-height/2 if centered else 0),('r2','d2',2,height/2 if centered else height)]:
                            key=diam_key if diam_key in named else radius_key
                            span=named.get(key,pos[index] if index<len(pos) else None)
                            factor=.5 if key.startswith('d') else 1.
                            add(key,span,[0,0,level],[factor,0,0],([factor,0,0],[0,factor,0]))
                    add('h',hspan,[0,0,0],[0,0,.5 if centered else 1.])
            elif enabled and node.name=='cube':
                named,pos=call_args(doc,node)
                span=named.get('size',pos[0] if pos else None)
                centered=bool(val(doc,named.get('center',pos[1] if len(pos)>1 else None),current_scope,False))
                if span:
                    for i,(part,owner) in enumerate(vector_parts(doc,span,current_scope)):
                        if i>2: break
                        direction=np.zeros(3); direction[i]=.5 if centered else 1.
                        add('XYZ'[i],part,[0,0,0],direction,owner=owner)
            safe=('group','translate','rotate','scale','mirror','multmatrix','color','difference','union','intersection','hull','render')
            if node.name in safe:
                for child in node.children: descend(child,matrix,current_scope,depth+1,enabled)
            elif node.name=='for':
                loops,_=call_args(doc,node)
                def values(span,owner):
                    raw=clean(doc.source[span[0]:span[1]])
                    if raw.startswith('[') and raw.endswith(']') and ':' in raw:
                        pieces=raw[1:-1].split(':')
                        if len(pieces) not in (2,3): raise ValueError('Unsupported range')
                        parts=[float(evaluate(x,owner)) for x in pieces]
                        start,end=parts[0],parts[-1]; step=parts[1] if len(parts)==3 else 1
                        if step==0: raise ValueError('Zero step')
                        count=max(0,int(math.floor((end-start)/step+1e-8))+1)
                        if count>128: raise ValueError('Too many handle instances')
                        return [start+i*step for i in range(count)]
                    result=val(doc,span,owner)
                    if not isinstance(result,list) or len(result)>128: raise ValueError('Unsupported loop')
                    return result
                def iterate(items,owner):
                    if not items:
                        for child in node.children: descend(child,matrix,owner,depth+1,enabled)
                        return
                    name,span=items[0]
                    for value in values(span,owner):
                        if len(handles)>=36: break
                        iterate(items[1:],Scope(doc.source,{},owner,t=t,constants={name:value}))
                iterate(list(loops.items()),current_scope)
            elif node.name in modules:
                body,parameters=modules[node.name]
                named,positional=call_args(doc,node)
                module_scope=Scope(doc.source,{},scope,t=t)
                for index,(name,default) in enumerate(parameters):
                    actual=named.get(name,positional[index] if index<len(positional) else None)
                    if actual: module_scope.bindings[name]=(actual,current_scope)
                    elif default: module_scope.bindings[name]=(default,module_scope)
                    else: raise ValueError('Missing module argument')
                descend(body,matrix,module_scope,depth+1,enabled)

        except (ValueError,TypeError,IndexError,KeyError,ZeroDivisionError,SyntaxError,StopIteration,OverflowError):
            return
    for root in doc.roots: descend(root,np.eye(4),scope)
    return handles
