// Component bodies used by the board. Colours are parameters so they can be
// changed per part (Inspector: "only this call") or for all parts (definition).

module qfp(size = 7, height = 1.4, body = "#1b1b1b") {
    color(body) translate([-size / 2, -size / 2, 0]) cube([size, size, height]);
    color("silver") for (a = [0:90:270]) rotate(a) for (i = [-5:5])
        translate([size / 2, i * 0.5, 0]) cube([1, 0.22, 0.2]);
}

module usb_c(width = 9, depth = 7.5, height = 3.3, shell = "silver") {
    color(shell) translate([-depth / 2, -width / 2, 0])
        hull() for (z = [1, height - 1]) translate([0, 1, z]) rotate([0, 90, 0]) cylinder(r = 1, h = depth, $fn = 24);
}

module header(pins = 6, pitch = 2.54, plastic = "#222") {
    color(plastic) translate([-pitch / 2, -pins * pitch / 2, 0]) cube([pitch, pins * pitch, 2.5]);
    color("gold") for (i = [0:pins - 1]) translate([-0.32, -pins * pitch / 2 + pitch / 2 + i * pitch - 0.32, 0])
        cube([0.64, 0.64, 8.5]);
}
