"""One plane definition shared by GPU clip, contour preview, SCAD and DXF export."""
from dataclasses import dataclass
import numpy as np
from .parameters import rotation

@dataclass(frozen=True)
class Plane:
    axis:str='Z'
    distance:float=0.
    angles:tuple=(0.,0.,0.)
    @property
    def basis(self):
        # Right-handed frames. Export coordinates: Z=(X,Y), X=(Y,Z), Y=(X,-Z).
        base={'Z':np.eye(3), 'X':np.array([[0,0,1],[1,0,0],[0,1,0]]),
              'Y':np.array([[1,0,0],[0,0,1],[0,-1,0]])}[self.axis]
        return rotation(self.angles)@base
    @property
    def normal(self): return self.basis[:,2]
    @property
    def origin(self): return self.normal*self.distance
    @property
    def pose(self):
        m=np.eye(4); m[:3,:3]=self.basis; m[:3,3]=self.origin
        return m
    def scad_matrix(self,inverse=False):
        m=np.linalg.inv(self.pose) if inverse else self.pose
        return '['+', '.join('['+', '.join(f'{x:.12g}' for x in row)+']' for row in m)+']'


def section_source(doc,plane,extent,flip=False):
    cube_origin=[-extent,-extent,0 if flip else -2*extent]
    cutter=(f'multmatrix({plane.scad_matrix()}) translate({cube_origin}) '
            f'cube([{2*extent}, {2*extent}, {2*extent}]);')
    source=doc.source
    for node in reversed(doc.roots):
        source=source[:node.start]+'intersection() {\n'+source[node.start:node.end]+'\n'+cutter+'\n}'+source[node.end:]
    return source


def projection_source(doc,plane):
    # Keep definitions at original scope. Union before projection removes internal
    # overlaps, retaining through holes as inner closed loops in the DXF.
    source=doc.source
    objects='\n'.join(source[n.start:n.end] for n in doc.roots)
    for n in reversed(doc.roots):
        source=source[:n.start]+''.join('\n' if c=='\n' else ' ' for c in source[n.start:n.end])+source[n.end:]
    return source+'\nprojection(cut = true)\nmultmatrix('+plane.scad_matrix(True)+')\nunion() {\n'+objects+'\n}\n'


def contour_segments(scene,plane):
    """Uncapped display contour from cached triangles; exact export uses OpenSCAD."""
    out=[]
    for _,_,vertices,faces in scene:
        tri=vertices[faces]
        d=tri@plane.normal-plane.distance
        # Fully coplanar triangles are omitted: avoid triangulation diagonals.
        mask=(d.min(axis=1)<=0)&(d.max(axis=1)>=0)&(np.max(np.abs(d),axis=1)>1e-7)
        tri,d=tri[mask],d[mask]
        if not len(tri): continue
        points=[]; valid=[]
        for a,b in ((0,1),(1,2),(2,0)):
            denom=d[:,a]-d[:,b]
            crossing=(d[:,a]*d[:,b]<=0)&(np.abs(denom)>1e-12)
            ratio=np.divide(d[:,a],denom,out=np.zeros_like(denom),where=np.abs(denom)>1e-12)
            points.append(tri[:,a]+ratio[:,None]*(tri[:,b]-tri[:,a])); valid.append(crossing)
        points=np.stack(points,axis=1); valid=np.stack(valid,axis=1)
        # Three edge intersections can include the same on-plane vertex twice.
        for p,v in zip(points,valid):
            chosen=p[v]
            if len(chosen)>=2:
                diffs=np.sum((chosen[:,None]-chosen[None,:])**2,axis=2)
                a,b=np.unravel_index(np.argmax(diffs),diffs.shape)
                if diffs[a,b]>1e-14: out.extend([chosen[a],chosen[b]])
    return np.asarray(out,dtype=np.float32).reshape(-1,3)
