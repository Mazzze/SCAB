// SCAB example: an enclosure designed around a live KiCad board.
//
// The line below links the KiCad board: SCAB shows it in 3D, reloads it every
// time the board is saved in KiCad, rewrites kicad/board.scad (outline, holes,
// part positions and 3D boxes) and checks the clearance of every part.
// scab: kicad = board/board.kicad_pcb
//
// Try it: lower lid_inner to 7 — C1 (the 7.7 mm tall electrolytic capacitor)
// then collides with the lid and turns red in the Clearance tab.
include <kicad/board.scad>

$fn = 48;

wall = 2;            // enclosure wall thickness
gap = 0.8;           // space between board edge and wall
floor_t = 2;         // bottom thickness
standoff_h = 5;      // board bottom above the inside floor
base_above = 3;      // base wall height above the board top
lid_inner = 10;      // free height above the board top under the lid
lid_t = 2;           // lid thickness
screw_d = 2.5;       // pilot hole in the standoffs (M3 thread-forming)
show_lid = true;     // set false to look inside (the clearance check then ignores the lid)

base_color = "#4a4f57";
lid_color = [0.80, 0.83, 0.88, 0.45];

top_z = kicad_board_thickness + lid_inner;          // underside of the lid
split_z = kicad_board_thickness + base_above;        // where base and lid meet

module outline(grow) {
    offset(r = grow) polygon(kicad_board_outline);
}

module shell(z0, z1, inner_z0) {
    difference() {
        translate([0, 0, z0]) linear_extrude(height = z1 - z0) outline(gap + wall);
        translate([0, 0, inner_z0]) linear_extrude(height = z1 - inner_z0 + 1) outline(gap);
    }
}

// Base: floor, walls up to split_z, standoffs under the mounting holes, USB-C cutout.
color(base_color) difference() {
    union() {
        shell(-standoff_h - floor_t, split_z, -standoff_h);
        for (h = kicad_mounting_holes)
            translate([h[0][0], h[0][1], -standoff_h - 0.01]) cylinder(d = h[1] + 3, h = standoff_h);
    }
    for (h = kicad_mounting_holes)
        translate([h[0][0], h[0][1], -standoff_h - floor_t - 1]) cylinder(d = screw_d, h = standoff_h + floor_t + 2);
    // opening for the USB-C plug: the connector's 3D box, extended outwards through the wall
    kicad_part_box("J1", margin = 1, extend = [wall + gap + 2, 0, 0]);
}

// Lid: walls from split_z to the top, holes for the reset button and the LEDs.
if (show_lid) color(lid_color) difference() {
    translate([0, 0, split_z]) linear_extrude(height = top_z + lid_t - split_z) outline(gap + wall);
    translate([0, 0, split_z - 1]) linear_extrude(height = top_z - split_z + 1) outline(gap);
    for (ref = ["SW1"]) translate([kicad_part(ref)[3][0], kicad_part(ref)[3][1], top_z - 1]) cylinder(d = 4, h = lid_t + 2);
    for (ref = ["D1", "D2"]) translate([kicad_part(ref)[3][0], kicad_part(ref)[3][1], top_z - 1]) cylinder(d = 2, h = lid_t + 2);
    kicad_part_box("J1", margin = 1, extend = [wall + gap + 2, 0, 0]);
}
