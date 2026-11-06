"""GPU mesh inspection. Orbit and section movement never invoke OpenSCAD."""
import re
import numpy as np
import pyqtgraph.opengl as gl
from pyqtgraph.opengl import shaders
from PySide6.QtCore import Qt, Signal, QPointF, QTimer, QEvent, QRunnable, QThreadPool, Slot, QRectF
from PySide6.QtGui import QVector3D, QColor, QPainter, QPen, QFont, QPolygonF, QMatrix4x4
from PySide6.QtWidgets import QWidget
import math
import threading
from .plane import Plane, contour_segments
from .navigation import ViewCube,AXIS_COLORS
from .parameters import rotation

VERTEX = '''
uniform mat4 u_mvp;
uniform mat3 u_normal;
attribute vec4 a_position;
attribute vec3 a_normal;
attribute vec4 a_color;
varying vec3 world;
varying vec3 normal;
varying vec4 color;
void main() {
    world = a_position.xyz;
    normal = normalize(u_normal * a_normal);
    color = a_color;
    gl_Position = u_mvp * a_position;
}
'''
FRAGMENT = '''
#ifdef GL_ES
precision mediump float;
#endif
uniform float sectionOn;
uniform float nx;
uniform float ny;
uniform float nz;
uniform float position;
uniform float flip;
varying vec3 world;
varying vec3 normal;
varying vec4 color;
void main() {
    float d = dot(world, vec3(nx, ny, nz));
    if (sectionOn > 0.5 && (d - position) * flip > 0.0) discard;
    vec3 n = normalize(normal);
    float light = 0.34 + 0.5 * abs(dot(n, normalize(vec3(0.4, 0.6, 1.0))))
                 + 0.16 * abs(dot(n, normalize(vec3(-0.8, 0.2, 0.5))));
    gl_FragColor = vec4(color.rgb * light, color.a);
}
'''


def source_color(source):
    # STL carries no material. Respect simple literal outermost color wrappers.
    m = re.match(r'\s*color\s*\(\s*"([^"]+)"', source)
    if m:
        c = QColor(m[1])
        if c.isValid(): return c.getRgbF()
    m = re.match(r'\s*color\s*\(\s*\[([\d.,\s]+)\]', source)
    if m:
        try:
            vals = [float(x) for x in m[1].split(',')]
            if len(vals) in (3,4): return tuple(vals[:3]) + (1.0,)
        except ValueError: pass
    return (.72, .74, .78, 1.0)


def material_color(source,start,end):
    try:
        from .model import Document
        from .parameters import Scope,evaluate,argument
        import json
        doc=Document(source);node=next((n for n in doc.walk() if n.start==start and n.end==end),None)
        while node is not None and node.name!='color' and len(node.children)==1:node=node.children[0]
        if node is not None and node.name=='color':
            a,b=argument(doc,node,'c',0);expression=source[a:b].strip()
            scope=Scope(source,{name:(a,b) for name,a,b in doc.variables()})
            for _ in range(8):
                if re.fullmatch(r'[$A-Za-z_][$\w]*',expression):
                    (a,b),owner=scope.definition(expression);expression=source[a:b].strip();continue
                break
            if expression.startswith('"'):
                color=QColor(json.loads(expression))
                if color.isValid():return color.getRgbF()
            value=evaluate(expression,scope)
            if isinstance(value,list) and len(value) in (3,4):return tuple(float(np.clip(x,0,1)) for x in value[:3])+(1.,)
    except (ValueError,TypeError,KeyError,SyntaxError,StopIteration):pass
    return source_color(source[start:end])


def ray_hit(vertices, faces, origin, direction, section=None):
    """Vectorized Möller–Trumbore; run only on click, never during orbit."""
    triangles = vertices[faces]
    edge1 = triangles[:,1]-triangles[:,0]
    edge2 = triangles[:,2]-triangles[:,0]
    p = np.cross(direction, edge2)
    det = np.einsum('ij,ij->i', edge1, p)
    valid = np.abs(det) > 1e-9
    inv = np.zeros_like(det)
    inv[valid] = 1/det[valid]
    tv = origin-triangles[:,0]
    u = np.einsum('ij,ij->i', tv, p)*inv
    q = np.cross(tv, edge1)
    v = q @ direction * inv
    t = np.einsum('ij,ij->i', edge2, q)*inv
    valid &= (u>=0)&(v>=0)&(u+v<=1)&(t>0)
    if section:
        normal, position, flip = section
        valid &= (((origin @ normal)+t*(direction @ normal)-position)*flip <= 0)
    return float(np.min(t[valid])) if np.any(valid) else float('inf')


class ContourTask(QRunnable):
    def __init__(self, scene, plane, generation, signal, cancel):
        super().__init__()
        self.scene,self.plane,self.generation,self.signal,self.cancel=scene,plane,generation,signal,cancel
    def run(self):
        if self.cancel.is_set(): return
        try:
            points=contour_segments(self.scene,self.plane)
            if not self.cancel.is_set(): self.signal.emit(self.generation,points)
        except (RuntimeError,ValueError): pass


class Overlay(QWidget):
    def __init__(self,view):
        super().__init__(view)
        self.view=view
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
    def paintEvent(self,event):
        v=self.view
        matrix=v.screen_matrix()
        project=lambda point:v.project(point,matrix)
        p=QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        p.setFont(QFont('DejaVu Sans',9))
        placed=[]
        def label_box(text,anchor,color):
            # Light text on a dark, bordered box; nudge down until it does not overlap another label.
            width=p.fontMetrics().horizontalAdvance(text)+12
            x=min(max(2,anchor.x()+10),max(2,self.width()-width-2))
            y=min(max(2,anchor.y()-22),self.height()-22)
            rect=QRectF(x,y,width,20)
            while any(rect.intersects(r) for r in placed) and rect.bottom()<self.height()-2:
                rect.translate(0,22)
            placed.append(rect)
            p.setPen(QPen(color,1));p.setBrush(QColor(30,31,34,235));p.drawRect(rect)
            p.setPen(QColor('#e6e7e9'));p.drawText(rect.adjusted(6,0,-4,0),Qt.AlignVCenter|Qt.AlignLeft,text)
        for i,h in enumerate(v.handles):
            value=v.drag_value if v.drag_kind=='dimension' and v.drag_index==i else h.value
            origin=project(h.origin); point=project(h.point(value))
            if point is None or origin is None: continue
            key=h.label.rsplit(' · ',1)[-1]
            color=QColor(AXIS_COLORS['XYZ'.index(key)] if key in ('X','Y','Z') else {'r':'#d9a343','d':'#d9a343','r1':'#d9a343','d1':'#d9a343','r2':'#b37fd9','d2':'#b37fd9','h':'#4fb3a9'}.get(key,'#c9ccd1'))
            p.setPen(QPen(QColor('#111214'),4));p.drawLine(origin,point)
            p.setPen(QPen(color,2)); p.drawLine(origin,point)
            if h.ring is not None:
                angles=np.linspace(0,2*np.pi,49)
                world=h.origin+value*(np.cos(angles)[:,None]*h.ring[0]+np.sin(angles)[:,None]*h.ring[1])
                coords,valid=v.project_many(world,matrix)
                if valid.all(): p.drawPolyline(QPolygonF([QPointF(float(x),float(y)) for x,y in coords]))
                else:
                    for index in range(len(coords)-1):
                        if valid[index] and valid[index+1]: p.drawLine(QPointF(*coords[index]),QPointF(*coords[index+1]))
            p.setPen(QPen(QColor('#111214'),2));p.setBrush(color); p.drawRect(QRectF(point.x()-5,point.y()-5,10,10))
            label_box(f'{h.label} = {value:.4g} mm',point,color)
        if v.gizmo_center is not None and v.transform_mode in ('Move','Rotate'):
            for axis in range(3):
                world=v.gizmo_points(axis);coords,valid=v.project_many(world,matrix)
                if not valid.all():continue
                poly=QPolygonF([QPointF(float(x),float(y)) for x,y in coords])
                p.setPen(QPen(QColor('#111214'),5));p.drawPolyline(poly)
                color=QColor(AXIS_COLORS[axis]);p.setPen(QPen(color,2));p.drawPolyline(poly)
                endpoint=poly[-1] if v.transform_mode=='Move' else poly[8+axis*8]
                p.setPen(QPen(QColor('#111214'),2));p.setBrush(color);p.drawRect(QRectF(endpoint.x()-5,endpoint.y()-5,10,10))
                label=f'{v.transform_mode} {"XYZ"[axis]}'
                if v.drag_kind in ('translate','rotate') and v.gizmo_axis==axis:label+=f' {v.drag_value:.3g}'+('°' if v.drag_kind=='rotate' else ' mm')
                label_box(label,endpoint,color)
        if v.plane_visible:
            origin,end=v.plane_arrow()
            a,b=project(origin),project(end)
            if a is not None and b is not None:
                p.setPen(QPen(QColor('#5c8ff2'),2)); p.drawLine(a,b)
                p.setBrush(QColor('#5c8ff2')); p.drawRect(int(b.x()-6),int(b.y()-6),12,12)
                label_box(f'Plane {v.section_pos:.3f} mm',b,QColor('#5c8ff2'))
        if v.measure_points:
            pts=[project(q) for q in v.measure_points]
            p.setPen(QPen(QColor('#f2c14e'),2))
            if len(pts)==2 and pts[0] is not None and pts[1] is not None:p.drawLine(pts[0],pts[1])
            for q in pts:
                if q is not None:p.setBrush(QColor('#f2c14e'));p.setPen(QPen(QColor('#111214'),1));p.drawRect(QRectF(q.x()-4,q.y()-4,8,8))
            if len(pts)==2 and pts[1] is not None:
                d=v.measure_points[1]-v.measure_points[0]
                label_box(f'{np.linalg.norm(d):.3f} mm  ·  dx {d[0]:.3f}  dy {d[1]:.3f}  dz {d[2]:.3f}',pts[1],QColor('#f2c14e'))
        p.end()


class Viewer(gl.GLViewWidget):
    picked=Signal(int)
    pickedPoint=Signal(int,object)
    dimensionEdited=Signal(object,float)
    dimensionPreview=Signal(str)
    planeMoved=Signal(float)
    contourReady=Signal(int,object)
    transformEdited=Signal(str,int,float,object)
    rollChanged=Signal(float)
    contextRequested=Signal(object,object)
    boardPicked=Signal(str,int,object)
    boardTransformed=Signal(str,object)
    measured=Signal(str)
    def __init__(self):
        super().__init__()
        self.setBackgroundColor('#2a2c30')
        self.setCameraPosition(distance=125,elevation=28,azimuth=-48)
        self.grid=gl.GLGridItem(); self.grid.setSize(200,200); self.grid.setSpacing(10,10)
        self.grid.setColor((110,113,120,90)); self.addItem(self.grid)
        self.axis_item=gl.GLAxisItem(); self.axis_item.setSize(25,25,25); self.addItem(self.axis_item)
        self.scene=[]; self.meshes=[]; self.cache={}; self.selected=-1; self.source=''
        self.section_on=False; self.plane_visible=False
        self.section_axis=2; self.section_pos=0.; self.section_flip=1.; self.section_angles=(0.,0.,0.)
        self.press_pos=None; self.moved=False; self.extent=100.; self.plane_items=[]
        self.contour_item=None; self.contour_generation=0; self.contour_cancel=threading.Event()
        self.contour_timer=QTimer(self); self.contour_timer.setSingleShot(True); self.contour_timer.timeout.connect(self.queue_contour)
        self.contourReady.connect(self.show_contour, Qt.QueuedConnection)
        self.handles=[]; self.drag_kind=None; self.drag_index=-1; self.drag_value=0.
        self.roll=0.;self.transform_mode='Dimensions';self.gizmo_center=None;self.gizmo_radius=10.
        self.gizmo_axis=0;self.gizmo_selected=-1;self.ghosts=[]
        self.pick_items=[]
        self.measure_mode=False;self.measure_points=[]
        self.reference=[];self.reference_items=[];self.reference_data=[];self.reference_owner=[];self.board_items={};self.board_local={};self.board_matrix={};self.gizmo_board=None
        self.overlay=Overlay(self); self.overlay.show()
        self.view_cube=ViewCube(self);self.view_cube.show();self.view_cube.reposition()

    @property
    def section_plane(self): return Plane('XYZ'[self.section_axis],self.section_pos,self.section_angles)

    def set_handles(self,handles):
        # A background compile/source edit may invalidate the active dimension.
        # Cancel that drag rather than dereferencing its old index/ranges.
        if self.drag_kind=='dimension':
            self.drag_kind=None; self.drag_index=-1; self.press_pos=None
        self.handles=list(handles); self.overlay.update()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'overlay'): self.overlay.setGeometry(self.rect())
        if hasattr(self,'view_cube'): self.view_cube.reposition()

    def update(self,*args):
        # Invalidate annotations when the view changes, never from paintGL:
        # repainting a child from its parent's paint can form a repaint loop.
        super().update(*args)
        if hasattr(self,'overlay'): self.overlay.update()
        if hasattr(self,'view_cube'): self.view_cube.update()

    def screen_matrix(self):
        rect=(0,0,self.width(),self.height())
        matrix=self.projectionMatrix(rect,rect)*self.viewMatrix()
        return np.array(matrix.data()).reshape(4,4).T

    def project_many(self,points,matrix):
        result=np.column_stack([points,np.ones(len(points))])@matrix.T
        valid=result[:,3]>1e-8
        w=np.where(valid,result[:,3],1.)
        xy=result[:,:2]/w[:,None]
        xy[:,0]=(xy[:,0]+1)*self.width()/2
        xy[:,1]=(1-xy[:,1])*self.height()/2
        return xy,valid

    def project(self,point,matrix=None):
        if not self.width() or not self.height(): return None
        matrix=self.screen_matrix() if matrix is None else matrix
        result=matrix@np.r_[point,1]
        if result[3]<=1e-8: return None
        x,y=result[:2]/result[3]
        return QPointF((x+1)*self.width()/2,(1-y)*self.height()/2)

    def clear_cache(self):
        for entry in self.cache.values():
            for item in entry[1]:
                self.removeItem(item); shaders.ShaderProgram.names.pop(item.shader().name,None)
        self.cache.clear(); self.meshes=[]

    def set_scene(self,scene,source,fit=False):
        for item in self.meshes: item.setVisible(False)
        self.meshes=[]; self.scene,self.source=scene,source; key=id(scene)
        if key in self.cache:
            self.meshes,self.bounds,self.extent=self.cache[key][1:]
            for item in self.meshes: item.setVisible(True)
            self.update_section(); self.highlight(self.selected); return
        for i,(start,end,vertices,faces) in enumerate(scene):
            shader=shaders.ShaderProgram(f'scab-{id(self)}-{key}-{i}',
                [shaders.VertexShader(VERTEX),shaders.FragmentShader(FRAGMENT)],
                uniforms={'sectionOn':[0.],'nx':[0.],'ny':[0.],'nz':[1.],'position':[0.],'flip':[1.]})
            data=scene[i];color=getattr(data,'color',material_color(source,start,end))
            render=getattr(data,'render',None)
            options={}
            if render is not None:
                vertices,faces,face_colors=render;options['faceColors']=face_colors
            item=gl.GLMeshItem(vertexes=vertices,faces=faces,smooth=False,color=color,shader=shader,drawEdges=False,**options)
            item.base_color=color;item.base_face_colors=render[2] if render is not None else None
            if (render is not None and np.any(render[2][:,3]<1)) or color[3]<1:
                item.setGLOptions('translucent');item.setDepthValue(10)   # after opaque geometry
            self.addItem(item);self.meshes.append(item)
        if scene:
            bounds=np.array([[s[2].min(axis=0),s[2].max(axis=0)] for s in scene])
            self.bounds=(bounds[:,0].min(axis=0),bounds[:,1].max(axis=0))
            self.extent=max(100.,float(np.max(np.abs(bounds)))*4)
            if fit: self.fit()
        self.cache[key]=(scene,self.meshes,getattr(self,'bounds',None),self.extent)
        self.update_section(); self.highlight(self.selected)

    def fit(self):
        boxes=[]
        if self.scene:boxes.append(self.bounds)
        for vertices,_,_ in self.reference_data:boxes.append((vertices.min(axis=0),vertices.max(axis=0)))
        if not boxes: return
        low=np.min([b[0] for b in boxes],axis=0);high=np.max([b[1] for b in boxes],axis=0)
        self.opts['center']=QVector3D(*map(float,(low+high)/2))
        self.setCameraPosition(distance=max(10.,float(np.linalg.norm(high-low))*1.8)); self.update()

    def highlight(self,start):
        """Select a SCAD object (by root start) for the selection tint and outline."""
        self.selected=start
        if start is not None and start>=0:self.selected_board=None
        self.apply_selection()

    def select_board(self,ident):
        self.selected_board=ident
        if ident:self.selected=-1
        self.apply_selection()

    SELECT=np.array([0.36,0.56,0.95,1.],dtype=np.float32)

    def tint(self,colors):
        out=np.array(colors,dtype=np.float32).copy()
        out[...,:3]=out[...,:3]*.5+self.SELECT[:3]*.5
        return out

    def apply_selection(self):
        """Blue tint and outline on the selected SCAD object or board."""
        selected=getattr(self,'selected',-1)
        for item,data in zip(self.meshes,self.scene):
            on=selected is not None and selected>=0 and data[0]==selected
            if getattr(item,'tinted',False)==on:continue
            item.tinted=on
            if item.base_face_colors is not None:
                item.opts['meshdata'].setFaceColors(self.tint(item.base_face_colors) if on else item.base_face_colors);item.meshDataChanged()
            item.setColor(tuple(map(float,self.tint(item.base_color))) if on else item.base_color)
        self.recolor_reference()
        self.update_selection_outline()
        self.update()

    def selection_bounds(self):
        selected=getattr(self,'selected',-1)
        if getattr(self,'selected_board',None):
            return self.board_bounds(self.selected_board)
        data=next((s for s in self.scene if selected is not None and selected>=0 and s[0]==selected),None)
        if data is None:return None
        return data[2].min(axis=0),data[2].max(axis=0)

    def update_selection_outline(self,matrix=None):
        for item in getattr(self,'selection_items',[]):self.removeItem(item)
        self.selection_items=[]
        bounds=self.selection_bounds()
        if bounds is None:return
        low,high=np.asarray(bounds[0],dtype=float),np.asarray(bounds[1],dtype=float)
        pad=max(.2,float(np.max(high-low))*.015);low,high=low-pad,high+pad
        corners=np.array([[x,y,z] for x in (low[0],high[0]) for y in (low[1],high[1]) for z in (low[2],high[2])])
        edges=[(i,j) for i in range(8) for j in range(i+1,8) if bin(i^j).count('1')==1]
        item=gl.GLLinePlotItem(pos=np.array([corners[k] for pair in edges for k in pair],dtype=np.float32),mode='lines',
                               color=tuple(map(float,self.SELECT)),width=2,antialias=True)
        item.setGLOptions('additive')
        if matrix is not None:item.setTransform(QMatrix4x4(*map(float,np.asarray(matrix).flatten())))
        self.addItem(item);self.selection_items=[item]

    def recolor_reference(self):
        groups=getattr(self,'reference_groups',{})
        board=getattr(self,'selected_board',None)
        for item in self.reference_items:
            colors=item.base_face_colors.copy()
            for mask,rgba in groups.get(item.owner,[]):colors[mask]=rgba
            if board and item.owner[0]==board:colors=self.tint(colors)
            item.opts['meshdata'].setFaceColors(colors);item.meshDataChanged()

    def camera_rotation(self):
        cached=getattr(self,'_camera_rotation',None)
        if cached is not None:return cached
        return rotation([0,0,getattr(self,'roll',0.)])@rotation([self.opts['elevation']-90,0,0])@rotation([0,0,-self.opts['azimuth']-90])

    def viewMatrix(self):
        r=self.camera_rotation();data=np.eye(4);data[:3,:3]=r
        matrix=QMatrix4x4();matrix.translate(0,0,-self.opts['distance'])
        matrix*=QMatrix4x4(*map(float,data.ravel()))
        c=self.opts['center'];matrix.translate(-c.x(),-c.y(),-c.z())
        return matrix

    def setCameraPosition(self,pos=None,distance=None,elevation=None,azimuth=None,rotation=None):
        if elevation is not None or azimuth is not None or rotation is not None:self._camera_rotation=None
        return super().setCameraPosition(pos,distance,elevation,azimuth,rotation)

    def cameraPosition(self):
        offset=self.camera_rotation().T@np.array([0.,0.,self.opts['distance']])
        return self.opts['center']+QVector3D(*map(float,offset))

    def orbit(self,azim,elev):
        # Rotate about screen axes, including after roll and across the poles.
        r=rotation([elev,0,0])@rotation([0,-azim,0])@self.camera_rotation()
        self._camera_rotation=r
        e=-math.acos(float(np.clip(r[2,2],-1,1)))
        self.opts['elevation']=math.degrees(e)+90
        if abs(math.sin(e))>1e-8:
            self.opts['azimuth']=(-math.degrees(math.atan2(-r[2,0],-r[2,1]))-90+180)%360-180
            self.roll=math.degrees(math.atan2(-r[0,2],r[1,2]))
            self.rollChanged.emit(self.roll)
        self.update()

    def set_roll(self,degrees):
        value=(float(degrees)+180)%360-180
        if getattr(self,'_camera_rotation',None) is not None:
            self._camera_rotation=rotation([0,0,value-self.roll])@self._camera_rotation
        self.roll=value
        self.rollChanged.emit(self.roll);self.update()

    def pan(self,dx,dy,dz,relative='global'):
        if relative!='view':return super().pan(dx,dy,dz,relative)
        inverse,ok=self.viewMatrix().inverted()
        if ok:
            scale=self.opts['distance']*2*math.tan(math.radians(self.opts['fov']/2))/max(1,self.width())
            self.opts['center']+=inverse.mapVector(QVector3D(-dx*scale,dy*scale,dz*scale))
            self.update()

    def set_gizmo(self,mode,start=None):
        if self.drag_kind in ('translate','rotate'):
            self.drag_kind=None;self.press_pos=None
        self.gizmo_board=None
        self.transform_mode=mode;self.gizmo_selected=start if start is not None else -1
        data=next((s for s in self.scene if s[0]==start),None)
        if data is not None:
            low,high=data[2].min(axis=0),data[2].max(axis=0)
            self.gizmo_center=(low+high)/2;self.gizmo_radius=max(5.,float(np.linalg.norm(high-low))*.42)
        else:self.gizmo_center=None
        self.update()

    def gizmo_points(self,axis):
        if self.gizmo_center is None:return np.empty((0,3))
        center=self.gizmo_center;r=self.gizmo_radius
        if self.transform_mode=='Move':return np.array([center,center+np.eye(3)[axis]*r])
        other=[i for i in range(3) if i!=axis];angles=np.linspace(0,2*np.pi,65)
        return center+r*(np.cos(angles)[:,None]*np.eye(3)[other[0]]+np.sin(angles)[:,None]*np.eye(3)[other[1]])

    def preview_transform(self,kind,axis,amount):
        matrix=np.eye(4)
        if kind=='translate':matrix[:3,3]=np.eye(3)[axis]*amount
        else:
            vector=np.zeros(3);vector[axis]=amount;matrix[:3,:3]=rotation(vector)
            matrix[:3,3]=self.gizmo_center-matrix[:3,:3]@self.gizmo_center
        for item in getattr(self,'selection_items',[]):item.setTransform(QMatrix4x4(*map(float,matrix.flatten())))
        if getattr(self,'gizmo_board',None):
            base=getattr(self,'board_matrix',{}).get(self.gizmo_board,np.eye(4))
            self.set_board_pose(self.gizmo_board,matrix@base,preview=True);return
        qt=QMatrix4x4(*map(float,matrix.flatten()))
        for item,data in zip(self.meshes,self.scene):
            if data[0]==self.gizmo_selected:item.setTransform(qt)
        self.update()

    def clear_ghosts(self):
        for item in self.ghosts:self.removeItem(item)
        self.ghosts=[]

    def preview_pattern(self,start,matrices):
        self.clear_ghosts()
        data=next((s for s in self.scene if s[0]==start),None)
        if data is None:return
        vertices,faces=data[2],data[3]
        for m in matrices[:64]:
            if len(faces)>15000:
                low,high=vertices.min(axis=0),vertices.max(axis=0)
                points=np.array([[x,y,z,1] for x in (low[0],high[0]) for y in (low[1],high[1]) for z in (low[2],high[2])])
                world=(points@np.asarray(m).T)[:,:3]
                edges=[(i,j) for i in range(8) for j in range(i+1,8) if bin(i^j).count('1')==1]
                item=gl.GLLinePlotItem(pos=np.array([world[k] for pair in edges for k in pair]),mode='lines',color=(.65,.85,1.,.8),width=2)
                self.addItem(item);self.ghosts.append(item);continue
            item=gl.GLMeshItem(vertexes=vertices,faces=faces,smooth=False,color=(.65,.85,1.,.16),
                drawEdges=len(faces)<15000,edgeColor=(.7,.9,1.,.45),glOptions='translucent')
            item.setTransform(QMatrix4x4(*map(float,np.asarray(m).flatten())))
            self.addItem(item);self.ghosts.append(item)
        self.update()

    def preview_color(self,start,color):
        for item,data in zip(self.meshes,self.scene):
            if data[0]==start:
                if item.base_face_colors is not None:
                    colors=item.base_face_colors if color is None else np.tile(np.asarray(color,dtype=np.float32),(len(item.base_face_colors),1))
                    item.opts['meshdata'].setFaceColors(colors);item.meshDataChanged()
                item.setColor(item.base_color if color is None else color)
        self.update()

    def plane_center(self):
        plane=self.section_plane
        center=np.mean(self.bounds,axis=0) if self.scene else np.zeros(3)
        return center+plane.normal*(plane.distance-center@plane.normal)

    def plane_arrow(self):
        center=self.plane_center()
        length=max(5.,min(self.extent*.12,100.))
        return center,center+self.section_plane.normal*length

    def update_section(self):
        normal=self.section_plane.normal
        for item in self.meshes+getattr(self,'reference_items',[]):
            shader=item.shader()
            for k,v in [('sectionOn',float(self.section_on)),('nx',normal[0]),('ny',normal[1]),('nz',normal[2]),('position',self.section_pos),('flip',self.section_flip)]: shader[k]=[float(v)]
        for item in self.plane_items: self.removeItem(item)
        self.plane_items=[]
        if self.plane_visible:
            size=max(10.,float(np.linalg.norm(self.bounds[1]-self.bounds[0]))*.7) if self.scene else 50.
            center=self.plane_center(); basis=self.section_plane.basis
            verts=np.array([center+basis[:,0]*u*size+basis[:,1]*v*size for u,v in [(-1,-1),(1,-1),(1,1),(-1,1)]],dtype=np.float32)
            plane=gl.GLMeshItem(vertexes=verts,faces=np.array([[0,1,2],[0,2,3]]),
                color=(.12,.65,.95,.22),smooth=False,computeNormals=False,glOptions='translucent')
            border=gl.GLLinePlotItem(pos=np.vstack([verts,verts[0]]),color=(.25,.85,1.,1.),width=2,antialias=True)
            self.plane_items=[plane,border]
            for item in self.plane_items: self.addItem(item)
        self.contour_generation+=1
        self.contour_cancel.set()
        if self.contour_item: self.removeItem(self.contour_item); self.contour_item=None
        if self.plane_visible: self.contour_timer.start(90)
        else: self.contour_timer.stop()
        self.overlay.update(); self.update()

    def queue_contour(self):
        self.contour_cancel=threading.Event()
        QThreadPool.globalInstance().start(ContourTask(self.scene,self.section_plane,self.contour_generation,self.contourReady,self.contour_cancel))

    @Slot(int,object)
    def show_contour(self,generation,points):
        if generation!=self.contour_generation or not self.plane_visible or not len(points): return
        if self.contour_item: self.removeItem(self.contour_item)
        self.contour_item=gl.GLLinePlotItem(pos=points,mode='lines',color=(1.,.77,.2,1.),width=3,antialias=True)
        self.addItem(self.contour_item)

    def drag_projection(self,origin,direction):
        a,b=self.project(origin),self.project(origin+direction)
        if a is None or b is None: return np.array([0.,-1.])
        delta=np.array([b.x()-a.x(),b.y()-a.y()])
        # If viewed straight down the measurement axis, vertical drag remains usable.
        if np.linalg.norm(delta)<.15: return np.array([0.,-self.height()/max(1.,self.opts['distance'])])
        return delta

    def mousePressEvent(self,e):
        self.press_pos=e.position(); self.mousePos=e.position(); self.moved=False; self.drag_kind=None
        if e.button()==Qt.LeftButton and self.gizmo_center is not None and self.transform_mode in ('Move','Rotate') and not e.modifiers():
            candidates=[]
            for axis in range(3):
                points=self.gizmo_points(axis)
                if self.transform_mode=='Move':points=points[-1:]
                for j,world in enumerate(points):
                    p=self.project(world)
                    if p is not None:candidates.append(((p-e.position()).manhattanLength(),axis,j,world))
            if candidates:
                distance,axis,j,point=min(candidates,key=lambda c:c[0])
                if distance<16:
                    self.gizmo_axis=axis;self.drag_kind='translate' if self.transform_mode=='Move' else 'rotate'
                    direction=np.eye(3)[axis] if self.drag_kind=='translate' else np.cross(np.eye(3)[axis],point-self.gizmo_center)*math.pi/180
                    self.drag_vector=self.drag_projection(point,direction);self.drag_value=self.drag_start_value=0.
                    self.overlay.update();return
        if e.button()==Qt.LeftButton and not e.modifiers() & (Qt.ShiftModifier|Qt.AltModifier|Qt.ControlModifier):
            if self.plane_visible:
                a,b=self.plane_arrow(); p=self.project(b)
                if p is not None and (p-e.position()).manhattanLength()<16:
                    self.drag_kind='plane'; self.drag_value=self.section_pos
                    self.drag_start_value=self.section_pos; self.drag_vector=self.drag_projection(b,self.section_plane.normal)
            if self.drag_kind is None:
                candidates=[]
                for i,h in enumerate(self.handles):
                    p=self.project(h.point())
                    if p is not None: candidates.append(((p-e.position()).manhattanLength(),i))
                if candidates and min(candidates)[0]<16:
                    _,self.drag_index=min(candidates); h=self.handles[self.drag_index]
                    self.drag_kind='dimension'; self.drag_value=self.drag_start_value=h.value
                    self.drag_vector=self.drag_projection(h.point(),h.direction)
        self.overlay.update()

    def mouseMoveEvent(self,e):
        if self.press_pos is None: return
        diff=e.position()-self.mousePos; self.mousePos=e.position()
        total=e.position()-self.press_pos
        if total.manhattanLength()>3: self.moved=True
        if self.drag_kind:
            delta=np.array([total.x(),total.y()])
            precision=.1 if e.modifiers() & Qt.ShiftModifier else 1.
            amount=float(delta@self.drag_vector/max(1e-8,self.drag_vector@self.drag_vector))*precision
            self.drag_value=self.drag_start_value+amount
            if self.drag_kind in ('translate','rotate'):
                self.preview_transform(self.drag_kind,self.gizmo_axis,self.drag_value)
            elif self.drag_kind=='dimension':
                self.drag_value=max(.01,self.drag_value)
                self.dimensionPreview.emit(f'{self.handles[self.drag_index].label} = {self.drag_value:.4g} mm · release to update code')
                self.overlay.update()
            else: self.planeMoved.emit(self.drag_value)
            return
        if e.buttons() & Qt.LeftButton:
            if e.modifiers() & Qt.ControlModifier:self.set_roll(self.roll+diff.x()*.5)
            elif e.modifiers() & (Qt.ShiftModifier|Qt.AltModifier|Qt.ControlModifier): self.pan(diff.x(),diff.y(),0,relative='view')
            else: self.orbit(-diff.x()*.5,diff.y()*.5)
        elif e.buttons() & Qt.MiddleButton:
            if e.modifiers() & Qt.ShiftModifier: self.orbit(-diff.x()*.5,diff.y()*.5)
            else: self.pan(diff.x(),diff.y(),0,relative='view')
        elif e.buttons() & Qt.RightButton: self.orbit(-diff.x()*.5,diff.y()*.5)

    def mouseReleaseEvent(self,e):
        if self.drag_kind in ('translate','rotate') and self.moved:
            if getattr(self,'gizmo_board',None):
                matrix=np.eye(4)
                if self.drag_kind=='translate':matrix[:3,3]=np.eye(3)[self.gizmo_axis]*self.drag_value
                else:
                    vector=np.zeros(3);vector[self.gizmo_axis]=self.drag_value;matrix[:3,:3]=rotation(vector)
                    matrix[:3,3]=self.gizmo_center-matrix[:3,:3]@self.gizmo_center
                base=getattr(self,'board_matrix',{}).get(self.gizmo_board,np.eye(4))
                self.boardTransformed.emit(self.gizmo_board,matrix@base)
            else:self.transformEdited.emit(self.drag_kind,self.gizmo_axis,self.drag_value,self.gizmo_center.copy())
        elif self.drag_kind=='dimension' and self.moved:
            self.dimensionEdited.emit(self.handles[self.drag_index],self.drag_value)
        elif not self.drag_kind and e.button()==Qt.LeftButton and not self.moved:
            if self.measure_mode:self.measure_click(e.position())
            else:self.pick(e.position())
        elif e.button()==Qt.RightButton and not self.moved and not self.drag_kind:
            self.contextRequested.emit(e.globalPosition().toPoint(),e.position())
        self.press_pos=None; self.drag_kind=None; self.drag_index=-1; self.overlay.update()

    def wheelEvent(self,e):
        pixels=e.pixelDelta(); delta=pixels.y() if not pixels.isNull() else e.angleDelta().y()
        if e.modifiers() & Qt.ShiftModifier: self.pan(0,delta*.2,0,relative='view')
        else:
            self.opts['distance']=float(np.clip(self.opts['distance']*math.exp(-delta*(.008 if not pixels.isNull() else .0015)),.05,1e8))
            self.update()
        e.accept()

    def event(self,e):
        if e.type()==QEvent.NativeGesture:
            if e.gestureType()==Qt.NativeGestureType.ZoomNativeGesture:
                self.opts['distance']=float(np.clip(self.opts['distance']*math.exp(-e.value()*3),.05,1e8)); self.update(); return True
            if e.gestureType()==Qt.NativeGestureType.RotateNativeGesture:
                self.set_roll(self.roll+e.value()); return True
        return super().event(e)

    def mouseDoubleClickEvent(self,e):
        if not self.drag_kind: self.fit()

    def pick(self,pos):
        rect=(0,0,self.width(),self.height())
        inverse,ok=(self.projectionMatrix(rect,rect)*self.viewMatrix()).inverted()
        if not ok: return
        x,y=2*pos.x()/self.width()-1,1-2*pos.y()/self.height(); points=[]
        for z in (-1,1):
            v=inverse.map(QVector3D(x,y,z)); points.append(np.array([v.x(),v.y(),v.z()]))
        origin=points[0]; direction=points[1]-origin; direction/=np.linalg.norm(direction)
        best,selected=float('inf'),-1
        section=(self.section_plane.normal,self.section_pos,self.section_flip) if self.section_on else None
        for start,end,vertices,faces in self.scene:
            dist=ray_hit(vertices,faces,origin,direction,section)
            if dist<best: best,selected=dist,start
        board=(float('inf'),None)
        for (vertices,faces,_),owner in zip(self.reference_data,getattr(self,'reference_owner',[])):
            dist=ray_hit(vertices,faces,origin,direction,section)
            if dist<board[0]:board=(dist,owner)
        if board[1] is not None and board[0]<best:
            self.boardPicked.emit(board[1][0],board[1][1],origin+direction*board[0]);return
        self.picked.emit(selected)
        self.pickedPoint.emit(selected,(origin+direction*best) if np.isfinite(best) else None)

    def ray(self,pos):
        rect=(0,0,self.width(),self.height())
        inverse,ok=(self.projectionMatrix(rect,rect)*self.viewMatrix()).inverted()
        if not ok:return None,None
        x,y=2*pos.x()/self.width()-1,1-2*pos.y()/self.height();points=[]
        for z in (-1,1):
            v=inverse.map(QVector3D(x,y,z));points.append(np.array([v.x(),v.y(),v.z()]))
        direction=points[1]-points[0];direction/=np.linalg.norm(direction)
        return points[0],direction

    def hit(self,pos,include_reference=True):
        """(kind, index/start, world point) of the surface under a screen position."""
        origin,direction=self.ray(pos)
        if origin is None:return None
        section=(self.section_plane.normal,self.section_pos,self.section_flip) if self.section_on else None
        best=(float('inf'),None,None)
        for start,end,vertices,faces in self.scene:
            d=ray_hit(vertices,faces,origin,direction,section)
            if d<best[0]:best=(d,'model',start)
        if include_reference:
            for index,(vertices,faces,_) in enumerate(self.reference_data):
                d=ray_hit(vertices,faces,origin,direction,section)
                if d<best[0]:best=(d,'reference',self.reference_owner[index] if index<len(getattr(self,'reference_owner',[])) else index)
        if not np.isfinite(best[0]):return None
        return best[1],best[2],origin+direction*best[0]

    def measure_click(self,pos):
        found=self.hit(pos)
        if found is None:return
        if len(self.measure_points)>=2:self.measure_points=[]
        self.measure_points.append(found[2])
        if len(self.measure_points)==2:
            d=self.measure_points[1]-self.measure_points[0]
            self.measured.emit(f'Distance {np.linalg.norm(d):.3f} mm  (dx {d[0]:.3f}, dy {d[1]:.3f}, dz {d[2]:.3f})')
        else:self.measured.emit('Measure: click the second point')
        self.overlay.update()

    def set_measure(self,on):
        self.measure_mode=bool(on);self.measure_points=[];self.overlay.update()

    def set_boards(self,boards):
        """Reference geometry for KiCad boards.

        boards: [(ident, parts [(vertices, faces, face_colors)] in board coordinates, 4x4 pose, visible, show_parts, part_offset)].
        Items keep board-local vertices and a GL transform, so moving a board does not re-upload meshes.
        """
        for item in self.reference_items:self.removeItem(item)
        self.reference_items=[];self.reference_data=[];self.reference_owner=[];self.board_items={};self.board_local={}
        for ident,parts,matrix,visible,show_parts,offset in boards:
            self.board_items[ident]=[];self.board_local[ident]=(parts,offset)
            if not visible:continue
            for index,(vertices,faces,colors) in enumerate(parts):
                if not len(faces) or (index>=offset and not show_parts) or not np.any(colors[:,3]>0):continue
                shader=shaders.ShaderProgram(f'scab-ref-{id(self)}-{ident}-{index}-{id(vertices)}',
                    [shaders.VertexShader(VERTEX),shaders.FragmentShader(FRAGMENT)],
                    uniforms={'sectionOn':[0.],'nx':[0.],'ny':[0.],'nz':[1.],'position':[0.],'flip':[1.]})
                item=gl.GLMeshItem(vertexes=vertices,faces=faces,faceColors=colors,smooth=False,drawEdges=False,shader=shader)
                item.base_face_colors=colors;item.owner=(ident,index)
                if np.any(colors[:,3]<1):item.setGLOptions('translucent')
                item.setDepthValue(-5)   # opaque boards first, so translucent SCAD parts show them through
                self.addItem(item);self.reference_items.append(item);self.board_items[ident].append(item)
            self.set_board_pose(ident,matrix,update=False)
        self.recolor_reference();self.update_selection_outline()
        self.update_section()

    def set_board_pose(self,ident,matrix,update=True,preview=False):
        """Move a board's items (and the world copies used for picking and measuring)."""
        qt=QMatrix4x4(*map(float,np.asarray(matrix,dtype=float).flatten()))
        for item in self.board_items.get(ident,[]):item.setTransform(qt)
        if not preview:
            parts,_=self.board_local.get(ident,([],0))
            keep=[(d,o) for d,o in zip(self.reference_data,self.reference_owner) if o[0]!=ident]
            self.reference_data=[d for d,_ in keep];self.reference_owner=[o for _,o in keep]
            for item in self.board_items.get(ident,[]):
                v,f,c=parts[item.owner[1]]
                world=(np.c_[v,np.ones(len(v))]@np.asarray(matrix,dtype=float).T)[:,:3].astype(np.float32)
                self.reference_data.append((world,f,c));self.reference_owner.append(item.owner)
            self.board_matrix=getattr(self,'board_matrix',{});self.board_matrix[ident]=np.asarray(matrix,dtype=float)
            if getattr(self,'selected_board',None)==ident:self.update_selection_outline()
        if update:self.update()

    def set_reference(self,parts):
        """Single board, identity pose (kept for simple callers)."""
        self.set_boards([('kicad',list(parts),np.eye(4),True,True,len(parts))])

    def board_bounds(self,ident):
        pts=[d[0] for d,o in zip(self.reference_data,self.reference_owner) if o[0]==ident and len(d[0])]
        if not pts:return None
        allp=np.vstack(pts);return allp.min(axis=0),allp.max(axis=0)

    def highlight_reference(self,groups):
        """Recolor reference faces: {(board id, part index): [(face_mask, rgba), ...]}."""
        self.reference_groups=groups
        self.recolor_reference()
        self.update()

    def set_board_gizmo(self,mode,ident):
        """Move / rotate gizmo on a board instead of a SCAD object."""
        bounds=self.board_bounds(ident) if ident else None
        self.transform_mode=mode;self.gizmo_selected=-1;self.gizmo_board=ident if bounds is not None else None
        if bounds is not None and mode in ('Move','Rotate'):
            low,high=bounds;self.gizmo_center=(low+high)/2;self.gizmo_radius=max(5.,float(np.linalg.norm(high-low))*.42)
        else:self.gizmo_center=None
        self.update()

    def show_pick_box(self,low,high,cutter=False):
        """Outline the inspected primitive (world-space box)."""
        self.clear_pick_box()
        low,high=np.asarray(low,dtype=float),np.asarray(high,dtype=float)
        pad=max(.05,float(np.max(high-low))*.01)
        low,high=low-pad,high+pad
        corners=np.array([[x,y,z] for x in (low[0],high[0]) for y in (low[1],high[1]) for z in (low[2],high[2])])
        edges=[(i,j) for i in range(8) for j in range(i+1,8) if bin(i^j).count('1')==1]
        color=(1.,.45,.35,1.) if cutter else (1.,.85,.25,1.)
        item=gl.GLLinePlotItem(pos=np.array([corners[k] for pair in edges for k in pair],dtype=np.float32),mode='lines',color=color,width=2,antialias=True)
        item.setGLOptions('additive')
        self.addItem(item);self.pick_items=[item];self.update()

    def clear_pick_box(self):
        for item in self.pick_items:self.removeItem(item)
        self.pick_items=[];self.update()
