// A minimal enclosure for the example board, written the way AGENTS.md describes.
// Verify:  python -m scab.cli check examples/agent_enclosure/enclosure.scad
// scab: kicad = ../kicad_chassis/board/board.kicad_pcb
kicad_pose = [[0, 0, 0], [0, 0, 0]];   // where the board sits: [x, y, z], [rx, ry, rz]
include <kicad/board.scad>

$fn = 48;

wall = 2;          // wall thickness
gap = 0.8;         // board edge to inner wall
floor_t = 2;       // bottom thickness
standoff_h = 4;    // free space under the board (bottom-side parts and pins)
lid_clear = 3;     // free space above the tallest part
lid_t = 2;         // lid thickness
screw_d = 2.5;     // pilot hole for an M3 thread-forming screw

split_z = kicad_board_thickness + 2;     // where base and lid meet
top_z = kicad_parts_top + lid_clear;     // underside of the lid

module outline(grow) offset(r = grow) polygon(kicad_board_outline);

// opening for the USB-C plug: the connector's 3D box, stretched out through the wall
module usb_opening() kicad_part_box("J1", margin = 1, extend = [wall + gap + 2, 0, 0]);

// Base: floor and walls up to split_z
difference() {
    translate([0, 0, -standoff_h - floor_t]) linear_extrude(height = split_z + standoff_h + floor_t) outline(gap + wall);
    translate([0, 0, -standoff_h]) linear_extrude(height = split_z + standoff_h + 1) outline(gap);
    for (h = kicad_mounting_holes) translate([h[0][0], h[0][1], -standoff_h - floor_t - 1]) cylinder(d = screw_d, h = floor_t + 2);
    usb_opening();
}

// Standoffs under the mounting holes
for (h = kicad_mounting_holes) translate([h[0][0], h[0][1], -standoff_h]) difference() {
    cylinder(d = h[1] + 3, h = standoff_h);
    translate([0, 0, -1]) cylinder(d = screw_d, h = standoff_h + 2);
}

// Lid: walls from split_z up, a hole above the reset button and the LEDs
difference() {
    translate([0, 0, split_z]) linear_extrude(height = top_z + lid_t - split_z) outline(gap + wall);
    translate([0, 0, split_z - 1]) linear_extrude(height = top_z - split_z + 1) outline(gap);
    translate([kicad_part("SW1")[3][0], kicad_part("SW1")[3][1], top_z - 1]) cylinder(d = 4, h = lid_t + 2);
    for (ref = ["D1", "D2"]) translate([kicad_part(ref)[3][0], kicad_part(ref)[3][1], top_z - 1]) cylinder(d = 2, h = lid_t + 2);
    usb_opening();
}
