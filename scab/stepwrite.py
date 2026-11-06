"""Write meshes to a STEP (ISO 10303-21, AP214) file.

Each body becomes a FACETED_BREP (one planar face per triangle, shared
vertices) with its own name and colour, so CAD tools (FreeCAD, KiCad,
Fusion, SolidWorks — anything built on a STEP reader) see separate coloured
solids. Geometry is faceted: curved surfaces keep the tessellation they had
in OpenSCAD / the 3D models.
"""
from datetime import datetime, timezone
from pathlib import Path
import numpy as np


def _f(v):
    text = f'{float(v):.6f}'.rstrip('0')
    return text + '0' if text.endswith('.') else text


def _s(text):
    return "'" + str(text).replace("'", "''").replace('\\', '\\\\') + "'"


def clean(vertices, faces):
    """Weld duplicate vertices and drop degenerate triangles."""
    v = np.asarray(vertices, dtype=np.float64)
    f = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    rounded = np.round(v, 6)
    unique, inverse = np.unique(rounded, axis=0, return_inverse=True)
    f = inverse.reshape(-1)[f]
    ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 2] != f[:, 0])
    f = f[ok]
    if len(f):
        tri = unique[f]
        area = np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
        f = f[area > 1e-12]
    used = np.unique(f)
    remap = np.full(len(unique), -1)
    remap[used] = np.arange(len(used))
    return unique[used], remap[f]


def write_step(path, bodies, name='SCAB export', author='SCAB'):
    """bodies: [(name, vertices (n,3) mm, faces (m,3), rgba)] -> STEP file. Returns the number of solids."""
    lines = []
    counter = [0]

    def add(text):
        counter[0] += 1
        lines.append(f'#{counter[0]}={text};')
        return counter[0]

    # units and context
    length = add('(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.))')
    angle = add('(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.))')
    solid_angle = add('(NAMED_UNIT(*)SI_UNIT($,.STERADIAN.)SOLID_ANGLE_UNIT())')
    uncertainty = add(f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-06),#{length},'distance_accuracy_value','confusion accuracy')")
    context = add(f"(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{uncertainty}))"
                  f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{length},#{angle},#{solid_angle}))REPRESENTATION_CONTEXT('Context #1','3D Context with UNIT and UNCERTAINTY'))")
    app_context = add("APPLICATION_CONTEXT('automotive_design')")
    add(f"APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#{app_context})")
    product_context = add(f"PRODUCT_CONTEXT('',#{app_context},'mechanical')")
    product = add(f"PRODUCT({_s(name)},{_s(name)},'',(#{product_context}))")
    formation = add(f"PRODUCT_DEFINITION_FORMATION('','',#{product})")
    definition_context = add(f"PRODUCT_DEFINITION_CONTEXT('part definition',#{app_context},'design')")
    definition = add(f"PRODUCT_DEFINITION('design','',#{formation},#{definition_context})")
    shape = add(f"PRODUCT_DEFINITION_SHAPE('','',#{definition})")
    add(f"PRODUCT_RELATED_PRODUCT_CATEGORY('part',$,(#{product}))")

    origin = add("CARTESIAN_POINT('',(0.,0.,0.))")
    z = add("DIRECTION('',(0.,0.,1.))")
    x = add("DIRECTION('',(1.,0.,0.))")
    placement = add(f"AXIS2_PLACEMENT_3D('',#{origin},#{z},#{x})")

    items = []
    styled = []
    color_cache = {}
    for body_name, vertices, faces, rgba in bodies:
        v, f = clean(vertices, faces)
        if len(f) < 4:
            continue
        points = [add(f"CARTESIAN_POINT('',({_f(a)},{_f(b)},{_f(c)}))") for a, b, c in v]
        tri = v[f]
        normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        normals /= np.linalg.norm(normals, axis=1, keepdims=True)
        edges = tri[:, 1] - tri[:, 0]
        edges /= np.linalg.norm(edges, axis=1, keepdims=True)
        face_ids = []
        for (a, b, c), n, e in zip(f, normals, edges):
            loop = add(f"POLY_LOOP('',(#{points[a]},#{points[b]},#{points[c]}))")
            bound = add(f"FACE_OUTER_BOUND('',#{loop},.T.)")
            nd = add(f"DIRECTION('',({_f(n[0])},{_f(n[1])},{_f(n[2])}))")
            ed = add(f"DIRECTION('',({_f(e[0])},{_f(e[1])},{_f(e[2])}))")
            axis = add(f"AXIS2_PLACEMENT_3D('',#{points[a]},#{nd},#{ed})")
            plane = add(f"PLANE('',#{axis})")
            face_ids.append(add(f"FACE_SURFACE('',(#{bound}),#{plane},.T.)"))
        shell = add("CLOSED_SHELL('',(" + ','.join(f'#{i}' for i in face_ids) + '))')
        brep = add(f"FACETED_BREP({_s(body_name)},#{shell})")
        items.append(brep)
        key = tuple(round(float(c), 3) for c in rgba[:3])
        if key not in color_cache:
            colour = add(f"COLOUR_RGB('',{_f(key[0])},{_f(key[1])},{_f(key[2])})")
            fill = add(f"FILL_AREA_STYLE_COLOUR('',#{colour})")
            area = add(f"FILL_AREA_STYLE('',(#{fill}))")
            surface_fill = add(f"SURFACE_STYLE_FILL_AREA(#{area})")
            side = add(f"SURFACE_SIDE_STYLE('',(#{surface_fill}))")
            usage = add(f"SURFACE_STYLE_USAGE(.BOTH.,#{side})")
            color_cache[key] = add(f"PRESENTATION_STYLE_ASSIGNMENT((#{usage}))")
        styled.append(add(f"STYLED_ITEM('color',(#{color_cache[key]}),#{brep})"))
    if not items:
        raise ValueError('Nothing to export')
    representation = add(f"FACETED_BREP_SHAPE_REPRESENTATION({_s(name)},(#{placement}," +
                         ','.join(f'#{i}' for i in items) + f"),#{context})")
    add(f"SHAPE_DEFINITION_REPRESENTATION(#{shape},#{representation})")
    add("MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION('',(" + ','.join(f'#{i}' for i in styled) + f"),#{context})")

    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    header = ['ISO-10303-21;', 'HEADER;',
              "FILE_DESCRIPTION(('SCAB export: OpenSCAD model and KiCad boards'),'2;1');",
              f"FILE_NAME({_s(Path(path).name)},'{stamp}',({_s(author)}),(''),'SCAB','SCAB','');",
              "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));", 'ENDSEC;', 'DATA;']
    with open(path, 'w', encoding='utf-8') as out:
        out.write('\n'.join(header) + '\n')
        out.write('\n'.join(lines))
        out.write('\nENDSEC;\nEND-ISO-10303-21;\n')
    return len(items)
