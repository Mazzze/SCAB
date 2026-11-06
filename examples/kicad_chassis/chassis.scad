// SCAB example: an enclosure designed around two live KiCad boards from two
// different KiCad projects — a main board and a display board stacked on it.
//
// The `scab: kicad` lines link the boards: SCAB shows them in 3D, reloads each
// one when it is saved in KiCad, rewrites kicad/<name>.scad (outline, holes,
// part positions and 3D boxes) and checks every part against this model and
// against the other board (Clearance tab).
// scab: kicad = board/board.kicad_pcb
// scab: kicad display = display/display.kicad_pcb
kicad_pose = [[0, 0, 0], [0, 0, 0]];       // main board (SCAB updates these when you move a board)
display_pose = [[8, 14, 14], [0, 0, 0]];   // display board: 14 mm above the main board
include <kicad/board.scad>
include <kicad/display.scad>
//
// Try it: drag the display board with Move (G) or edit display_pose / the
// Boards tab — its window and standoffs follow. Lower lid_inner to 15 and the
// display hits the lid; lower the display to z = 9 and it hits C1.

$fn = 48;

wall = 2;            // enclosure wall thickness
gap = 0.8;           // space between board edge and wall
floor_t = 2;         // bottom thickness
standoff_h = 5;      // board bottom above the inside floor
base_above = 3;      // base wall height above the board top
lid_inner = 18;      // free height above the main board top under the lid
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

// Standoffs carrying the display board, standing on the main board.
color(base_color) display_place()
    for (h = display_mounting_holes) translate([h[0][0], h[0][1], -(display_pose[0][2] - kicad_board_thickness)])
        difference() {
            cylinder(d = 5, h = display_pose[0][2] - kicad_board_thickness);
            translate([0, 0, -1]) cylinder(d = 2.2, h = display_pose[0][2]);
        }

// Lid: walls from split_z to the top, holes for the reset button and the LEDs, a window for the display.
if (show_lid) color(lid_color) difference() {
    translate([0, 0, split_z]) linear_extrude(height = top_z + lid_t - split_z) outline(gap + wall);
    translate([0, 0, split_z - 1]) linear_extrude(height = top_z - split_z + 1) outline(gap);
    for (ref = ["SW1"]) translate([kicad_part(ref)[3][0], kicad_part(ref)[3][1], top_z - 1]) cylinder(d = 4, h = lid_t + 2);
    for (ref = ["D1", "D2"]) translate([kicad_part(ref)[3][0], kicad_part(ref)[3][1], top_z - 1]) cylinder(d = 2, h = lid_t + 2);
    display_place() display_part_box("DS1", margin = -1.5, extend = [0, 0, 40]);
    kicad_part_box("J1", margin = 1, extend = [wall + gap + 2, 0, 0]);
}
