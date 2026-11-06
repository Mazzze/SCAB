// SCAD Forge PCB helpers — design the board shape and part placement in OpenSCAD,
// then File → Export KiCad board… writes a .kicad_pcb (Edge.Cuts outline, holes and
// footprints placed by library id) plus a placement CSV.
//
// Positions are in millimetres in the board's XY plane, rotations in degrees
// counter-clockwise seen from the top. Everything is ordinary OpenSCAD: the
// modules can be wrapped in your own modules, loops and transforms.

pcb_thickness = 1.6;           // board thickness used by parts placed on the top side
pcb_color = "#1d6b3a";         // solder mask green

// The board: outline extruded to `thickness`; children are cutters (holes, slots).
//   size     [x, y] for a rectangular board with corner `radius`
//   outline  list of [x, y] points for a custom polygon (overrides size)
module pcb_board(size = [100, 80], thickness = pcb_thickness, radius = 3, outline = undef, color = pcb_color) {
    color(color) difference() {
        linear_extrude(height = thickness) pcb_outline(size, radius, outline);
        if ($children > 0) children();
    }
}

module pcb_outline(size = [100, 80], radius = 3, outline = undef) {
    if (!is_undef(outline)) polygon(outline);
    else if (radius > 0) translate([radius, radius]) offset(r = radius, $fn = 64) square([size[0] - 2 * radius, size[1] - 2 * radius]);
    else square(size);
}

// A plain (non-plated) drilled hole through the board. Use inside pcb_board().
module pcb_hole(pos = [0, 0], d = 3.2, thickness = pcb_thickness) {
    translate([pos[0], pos[1], -1]) cylinder(d = d, h = thickness + 2, $fn = 48);
}

// Mounting hole: a hole in the board and a KiCad footprint in the export.
module pcb_mounting_hole(ref = "H1", pos = [0, 0], d = 3.2, footprint = "MountingHole:MountingHole_3.2mm_M3",
                         thickness = pcb_thickness) {
    pcb_hole(pos, d, thickness);
}

// A component. Its children are its 3D body; without children a box `body` is drawn.
//   ref        reference designator, e.g. "U1", "J3"
//   footprint  KiCad library id "Library:Footprint"
//   side       "top" or "bottom"
//   body       [x, y, z] placeholder size, centred on pos
module pcb_part(ref = "U1", footprint = "", pos = [0, 0], rot = 0, side = "top", body = [5, 5, 1.5],
                color = "#222222", value = "", z = pcb_thickness) {
    top = side != "bottom";
    translate([pos[0], pos[1], top ? z : 0])
        rotate([top ? 0 : 180, 0, top ? rot : -rot]) {
            if ($children > 0) children();
            else color(color) translate([-body[0] / 2, -body[1] / 2, 0]) cube(body);
        }
}

// Connectors are parts too; a distinct default colour makes them easy to spot.
module pcb_connector(ref = "J1", footprint = "", pos = [0, 0], rot = 0, side = "top", body = [8, 6, 7],
                     color = "#d0d0d0", value = "", z = pcb_thickness) {
    pcb_part(ref, footprint, pos, rot, side, body, color, value, z) children();
}
