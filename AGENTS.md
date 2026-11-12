# SCAB for AI agents

You are designing **3D-printable enclosures, brackets and chassis around real
KiCad boards** with SCAB. A design is an ordinary OpenSCAD file (`.scad`). It
links one or more KiCad boards (`.kicad_pcb`). SCAB turns each board into
OpenSCAD data: outline, holes, and every part's position and 3D box. Your
geometry can then follow the board. When the user saves the board in KiCad,
everything updates.

The user may have the design open in the SCAB window while you work. SCAB
reloads a file that changes on disk (when the user has no unsaved edits in
it), so they see your changes right away.

## Tools

The same tools are available as an MCP server and as a command line. Every
command returns JSON.

| MCP tool (`scab` server) | Command line | What it does |
|---|---|---|
| `scab_info` | `scab-cli info design.scad` | Boards (id, pose, size, thickness, mounting holes, parts with 3D boxes, include API), variables, modules, objects, groups. **Start here.** |
| `scab_board` | `scab-cli board x.kicad_pcb` | One KiCad board as data, without a design |
| `scab_link_board` | `scab-cli link design.scad x.kicad_pcb [--id ID] [--pose x,y,z,rx,ry,rz]` | Link a board: writes the link comment, `<id>_pose`, the include line, and `kicad/<id>.scad` |
| `scab_unlink_board` | `scab-cli unlink design.scad ID` | Remove a board link, its pose and its include line |
| `scab_set_pose` | `scab-cli pose design.scad ID x y z rx ry rz` | Place a board |
| `scab_update_includes` | `scab-cli update design.scad` | Re-read the boards and rewrite `kicad/*.scad` |
| `scab_check` | `scab-cli check design.scad [--min 1]` | **Verification**: syntax, OpenSCAD errors and warnings, then clearance of every part. `ok` means it compiles without errors and nothing collides. The command line exits with 1 when the design is not ok. |
| `scab_render` | `scab-cli render design.scad out.png --view iso` | Picture of the design with the boards placed. Views: iso, top, front, right, back, left, bottom |
| `scab_export` | `scab-cli export design.scad out.stl` or `out.step` | STL: the chassis only. STEP: chassis plus every board with its parts |
| `scab_board_api` | `scab-cli api ID` | The OpenSCAD names a board include defines |

### Running the tools

- **Command line:** `tools/scab-cli` (Linux/macOS) or `tools\scab-cli.bat`
  (Windows), in the SCAB folder. You can also run `python -m scab.cli …`
  from the SCAB folder.
- **MCP:** this repository's `.mcp.json` registers the server for Claude
  Code. For another client, use the command
  `python3 <SCAB>/tools/scab_launch.py mcp`.
- **A design in another folder:** run
  `tools/scab-cli init <project folder>` once. It copies this guide and a
  `.mcp.json` that points back to SCAB into that folder.
- **OpenSCAD** must be installed (or set `OPENSCAD_BINARY`). `render` needs
  OpenGL; on a Linux machine without a display it runs itself under
  `xvfb-run`.

## The loop

1. `scab_info design.scad`. Read the boards: `size`, `thickness`,
   `mounting_holes`, and `parts` (`ref`, `side`, `position`, `height`,
   `box_min`, `box_max`). Read the existing variables too.
2. If the board is not linked yet, run `scab_link_board`. If it is a second
   board, give it an id (`--id display`) and a pose.
3. Write or edit the `.scad`. Follow the rules below.
4. Run `scab_check`. Fix every `error`. Fix `collision`s, which mean the
   enclosure cuts into a part or two boards touch. Look at `tight` parts,
   which have less than the minimum gap or headroom. Repeat until `ok` is
   true.
5. Run `scab_render` and look at the picture: openings in the right walls,
   lid on top, nothing floating.
6. Tell the user what you made, the important parameters, and the check
   result.

## Linking and coordinates

A design file starts like this:

```openscad
// scab: kicad = board/board.kicad_pcb                  main board, id "kicad"
// scab: kicad display = ../display/display.kicad_pcb   second board, id "display"
kicad_pose = [[0, 0, 0], [0, 0, 0]];
display_pose = [[8, 14, 14], [0, 0, 0]];   // translate [x, y, z], then rotate [rx, ry, rz] (degrees)
include <kicad/board.scad>
include <kicad/display.scad>
```

- **Board coordinates:** millimetres, origin at the KiCad auxiliary
  (drill/place) origin, otherwise the lower-left of the outline.
  - X to the right, Y up (KiCad's Y is flipped for you).
  - Z = 0 is the **bottom** of the board, and Z = `<id>_board_thickness` is
    its top.
  - Top-side parts sit above the board top; bottom-side parts hang below
    Z = 0.
- **Poses:** `<id>_pose` places a board in the design.
  - Geometry given in a board's coordinates goes inside `<id>_place() { … }`,
    which moves it with the board.
  - The board with id `kicad` normally keeps the pose `[[0,0,0],[0,0,0]]`,
    so its board coordinates are the design's coordinates and you can use
    `kicad_*` names directly.
  - For any other board, wrap what belongs to it in `<id>_place()`.
- **Pose lines are owned by SCAB:** keep them as plain literals. SCAB
  rewrites them when the user drags a board, and `scab_set_pose` writes them
  too.
- **Ids:** the first unnamed link gets id `kicad` (names `kicad_*`, include
  `kicad/<board file name>.scad`). Named links use their name (`display_*`,
  `kicad/display.scad`).
- **Link options** go after the path, e.g. `color=#c03030` (the board's
  color in SCAB).
- **Never edit `kicad/*.scad` or `kicad/*-parts.stl`.** SCAB regenerates
  them.

### What a board include gives you (shown for id `kicad`)

| Name | Meaning |
|---|---|
| `kicad_board_size`, `kicad_board_min`, `kicad_board_max` | outline size and bounding box `[x, y]` |
| `kicad_board_thickness` | board thickness |
| `kicad_board_outline` | outline polygon, so `offset(r = …) polygon(kicad_board_outline)` gives walls that follow the board, rounded corners included |
| `kicad_board_cutouts` | inner cutout polygons |
| `kicad_board_holes` | `[[[x, y], diameter, plated, ref], …]`: every drilled hole |
| `kicad_mounting_holes` | `[[[x, y], diameter], …]`: holes of `H*` footprints or holes ≥ 2.5 mm |
| `kicad_parts` | one record per part: `[ref, value, footprint, [x, y], rotation, side, [min x, y, z], [max x, y, z], has_3d_model]` |
| `kicad_parts_top` | Z of the highest part top |
| `kicad_part("J1")` | one record: `[3]` is the position, `[5]` the side, `[6]` / `[7]` the 3D box corners |
| `kicad_part_box("J1", margin = 0.5, extend = [x, y, z])` | a box around a part, grown by `margin`. `extend` stretches it outwards (+) or inwards (−) per axis, which makes connector openings |
| `kicad_place() { … }` | children in board coordinates, placed at the board's pose |
| `kicad_pcb(parts = true)` | the board itself (and its parts), for looking only |

## Design rules

1. **Plain OpenSCAD only.** The file must also open in OpenSCAD. Use no
   libraries unless the user already uses them.
2. **Parameters first.** Put every dimension in a top-level variable with a
   comment: `wall = 2; // wall thickness`. SCAB shows these in the project
   panel and the inspector, where the user edits them. Avoid magic numbers in
   the geometry.
3. **Derive from the board,** never copy its numbers. Use
   `kicad_board_outline`, `kicad_mounting_holes`, `kicad_part("…")`,
   `kicad_part_box("…")` and `kicad_parts_top`, so the design follows when
   the board changes in KiCad.
4. **One top-level statement per printed part** (base, lid, bracket). SCAB
   exports one STL per top-level object, and `color()` on a top-level object
   is shown in SCAB. Do not merge base and lid into one `union()`.
5. **Do not put `<id>_pcb()` in your geometry.** It is only for looking. The
   check and the STL export leave out top-level statements that only draw
   boards.
6. **Openings come from parts.** `kicad_part_box(ref, margin, extend)`
   inside the wall's `difference()`. Point `extend` at the wall the part
   faces (see the recipe below).
7. **Clearance:** 1 mm is a good default (`--min`). Leave headroom above the
   tallest part. Bottom-side parts and through-hole pins need space under the
   board, so use standoffs at least 3–4 mm high.
8. **Printability:** walls of 1.6 mm or more, no unsupported overhangs wider
   than about 5 mm (put lids on top), and screw pilots: M2.5 ≈ 2.2 mm,
   M3 ≈ 2.5 mm for thread-forming screws. Use `$fn = 48` or more for round
   parts that are printed.
9. **Groups:** variables like `post_and_lid_pos = [0, 0, 0];   // SCAB group: …`
   are made by the user in SCAB to move parts together. Keep them, and keep
   using `translate(<name>)` on their members.
10. **Change only what was asked** in an existing design. Keep the user's
    variable names, comments and structure.

## Recipes

**Walls that follow the board outline**

```openscad
module outline(grow) offset(r = grow) polygon(kicad_board_outline);
difference() {
    translate([0, 0, z0]) linear_extrude(height = z1 - z0) outline(gap + wall);
    translate([0, 0, z0 + floor_t]) linear_extrude(height = z1 - z0) outline(gap);
}
```

**Standoffs under the mounting holes** (the board bottom is at Z = 0):

```openscad
for (h = kicad_mounting_holes) translate([h[0][0], h[0][1], -standoff_h]) difference() {
    cylinder(d = h[1] + 3, h = standoff_h);
    translate([0, 0, -1]) cylinder(d = screw_d, h = standoff_h + 2);
}
```

**A connector opening in the wall it faces.** Compare the part's position
with the board's edges and extend the box outwards along that axis:

```openscad
// J1 at the right edge (x near kicad_board_max[0]): extend +X through the wall
kicad_part_box("J1", margin = 1, extend = [wall + gap + 2, 0, 0]);
// left edge: [-(wall + gap + 2), 0, 0] · top edge (max y): [0, wall + gap + 2, 0] · bottom edge: [0, -(wall + gap + 2), 0]
```

**A hole above a part** (button, LED, light pipe):

```openscad
translate([kicad_part("SW1")[3][0], kicad_part("SW1")[3][1], top_z - 1]) cylinder(d = 4, h = lid_t + 2);
```

**A window for a display, inside its part outline:**

```openscad
display_place() display_part_box("DS1", margin = -1.5, extend = [0, 0, 40]);
```

**A second board stacked on the first.** Give it a pose, then build its
standoffs in its own coordinates and let them reach down to the lower
board's top:

```openscad
display_place() for (h = display_mounting_holes)
    translate([h[0][0], h[0][1], -(display_pose[0][2] - kicad_board_thickness)])
        cylinder(d = 5, h = display_pose[0][2] - kicad_board_thickness);
```

**Lid height from the parts:** `top_z = kicad_parts_top + lid_clear;`

**A board mounted upside down:** pose `[[x, y, z], [180, 0, 0]]`. Its
bottom-side parts then face up, and the check measures headroom away from the
board.

## Reading `scab_check`

```json
{"ok": false, "stage": "clearance",
 "problems": [{"severity": "warning", "message": "…", "line": 12, "hint": "…"}],
 "clearance": {"minimum": 1, "counts": {"collision": 1, "tight": 0, "ok": 8},
   "problems": [{"board": "kicad", "ref": "C1", "status": "collision", "height": 7.7, "headroom": 1.3, "gap": 0}]}}
```

- **`problems`:** OpenSCAD errors and warnings with line numbers in the
  design. `stage: "syntax"` means the file does not parse; the line is in the
  problem.
- **`collision`:** the chassis passes through the part's 3D box, the part
  sits inside material, or it touches another board. Fix it by moving
  material away, raising the lid, adding an opening, or moving the board
  (`scab_set_pose`).
- **`tight`:** `gap` (the nearest distance to the model) or `headroom` (the
  free space straight above a top-side part, below a bottom-side part) is
  less than `minimum`.
- **`height`** is the part's height above the board surface. It is useful
  for choosing `lid_clear` and `standoff_h`.

## Worked example

`examples/agent_enclosure/enclosure.scad` is a base with standoffs, a lid,
a USB-C opening and holes for the button and LEDs, made only from the board
data. `scab-cli check` reports `ok` with 9 of 9 parts clear. Read it before
you write a new enclosure. `examples/kicad_chassis/chassis.scad` shows two
stacked boards.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `OpenSCAD not found` | Install OpenSCAD, or set `OPENSCAD_BINARY` to its executable |
| `Can't open include file 'kicad/x.scad'` | Run `scab_update_includes`, or link the board with `scab_link_board` |
| `render` fails on Linux | It needs OpenGL; install `xvfb` (it is used automatically) |
| Everything collides | Check the board pose and `standoff_h`; a wall's `difference()` may be missing its inner cut |
| Slow check | Lower `$fn` while iterating; the check compiles the whole design once |
