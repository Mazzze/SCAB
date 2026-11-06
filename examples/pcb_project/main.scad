// Multi-file PCB project: shape the board and place parts here, then
// PCB → Export KiCad board… writes main.kicad_pcb + main-placement.csv.
include <config.scad>
include <../../assets/scadforge/pcb.scad>
use <modules/parts.scad>

$fn = 32;

pcb_board(size = board_size, radius = board_corner) {
    for (x = [mount_inset, board_size[0] - mount_inset], y = [mount_inset, board_size[1] - mount_inset])
        pcb_mounting_hole(str("H", round(x), "_", round(y)), [x, y], d = mount_hole);
    pcb_hole([50, 8], d = 1.2);
}

pcb_part(ref = "U1", footprint = "Package_QFP:LQFP-48_7x7mm_P0.5mm", pos = [45, 35], rot = 45, value = "STM32G0") qfp();
pcb_connector(ref = "J1", footprint = "Connector_USB:USB_C_Receptacle_GCT_USB4085", pos = [board_size[0] - 4, 32], rot = 0) usb_c();
pcb_connector(ref = "J2", footprint = "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical", pos = [10, 32]) header(6);
for (i = [0:3])
    pcb_part(ref = str("C", i + 1), footprint = "Capacitor_SMD:C_0603_1608Metric", pos = [35 + i * 6, 18],
             body = [1.6, 0.8, 0.8], color = "#b08850", value = "100n");
pcb_part(ref = "R1", footprint = "Resistor_SMD:R_0603_1608Metric", pos = [70, 50], side = "bottom", body = [1.6, 0.8, 0.5], value = "10k");

// Transparent enclosure lid preview (not exported to KiCad)
%color(lid_color) translate([-enclosure_wall - enclosure_gap, -enclosure_wall - enclosure_gap, -6])
    cube([board_size[0] + 2 * (enclosure_wall + enclosure_gap), board_size[1] + 2 * (enclosure_wall + enclosure_gap), 20]);
