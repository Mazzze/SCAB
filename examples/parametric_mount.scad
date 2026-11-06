// SCAD Forge example — click any part of the model: the Inspector shows the
// module calls, colours and variables behind it and lets you edit them.
// Try: click a standoff and change its height; click the LED and pick a colour.

$fn = 48;

plate_size = [90, 60];
plate_thickness = 3;
corner_radius = 5;
hole_d = 3.4;              // M3 clearance
standoff_height = 8;
standoff_color = "goldenrod";
led_color = "red";

module rounded_plate(size, t, r) {
    linear_extrude(height = t)
        translate([r, r]) offset(r = r) square([size[0] - 2 * r, size[1] - 2 * r]);
}

module standoff(h = standoff_height, d = 6, hole = hole_d, c = standoff_color) {
    color(c) difference() {
        cylinder(d = d, h = h);
        translate([0, 0, -1]) cylinder(d = hole, h = h + 2);
    }
}

module led(c = led_color, d = 5, h = 8) {
    color(c) {
        cylinder(d = d, h = h - d / 2);
        translate([0, 0, h - d / 2]) sphere(d = d);
    }
    color("silver") for (x = [-1.27, 1.27]) translate([x, 0, -3]) cylinder(d = 0.6, h = 3);
}

// Base plate with mounting holes
color("steelblue") difference() {
    rounded_plate(plate_size, plate_thickness, corner_radius);
    for (x = [8, plate_size[0] - 8], y = [8, plate_size[1] - 8])
        translate([x, y, -1]) cylinder(d = hole_d, h = plate_thickness + 2);
}

// Standoffs on the plate
for (x = [8, plate_size[0] - 8], y = [8, plate_size[1] - 8])
    translate([x, y, plate_thickness]) standoff();

// Status LEDs: the first one overrides the colour, the others use the default
translate([45, 30, plate_thickness]) led("lime");
for (i = [1:3]) translate([45 + i * 8, 30, plate_thickness]) led();
