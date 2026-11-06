# SCAD Forge

A source-first desktop CAD editor for **OpenSCAD**, aimed at designing complex 3D
PCB assemblies (board shape, mounting holes, parts, enclosures) before taking the
board to **KiCad**. Every action edits ordinary `.scad` text — OpenSCAD stays the
geometry authority, your files stay portable.

## Highlights

| | |
|---|---|
| **Click the model → edit what made it** | The Inspector shows the clicked primitive, every module call above it (in any file, any depth), the colors in effect, loop values and the variables it uses. Edit a value and press Enter: the change is written *where the value is defined* (e.g. `config.scad:5`), or as an override on just that call. Module defaults, colors and global variables are all editable. |
| **Complete Vim mode** | Motions, operators, text objects, counts, visual / line / block mode, registers and the system clipboard, `.` repeat, macros, marks, jump list, search with highlighting, and ex commands (`:s`, `:g`, `:normal`, `:sort`, …). |
| **See where the SCAD is wrong** | Syntax errors are flagged while you type. OpenSCAD errors and warnings (including failed `assert`s and the module call chain) are mapped back to the right file and line: gutter dots, wavy underline, hover text with a hint, a clickable **Problems** panel, and `:cn` / `:cp` to jump between them. |
| **Fast compiles of large models** | The Rust engine expands the design once, splits it into pieces, **instances identical geometry** (vias, screws, repeated parts are meshed once), meshes in parallel and caches by content. Editing one part recompiles one part. |
| **KiCad export** | `PCB → Export KiCad board…` writes a `.kicad_pcb` with the exact Edge.Cuts outline (true arcs), holes and footprints placed by library id, plus a placement CSV. |

Measured on a PCB-like test model (board with 200 drilled holes + 600 placed
components, CGAL backend): first compile **242 s → 7.5 s**, recompile after
changing one component **235 s → 0.16 s**.

## Getting started

Requirements: [OpenSCAD](https://openscad.org) (2021.01 or newer; a 2023+ build
with the Manifold backend is much faster), Python 3.10+, and optionally
[Rust](https://rustup.rs) for the fast compile engine.

```bash
bash run.sh                      # Linux / macOS
bash run.sh path/to/model.scad   # open a file
bash run.sh examples/pcb_project # open a project folder
```

On Windows run `run.bat`. The scripts create `.venv`, install
`requirements.txt`, build `rust-engine` when `cargo` is available, then start
the app. If OpenSCAD is not on `PATH`, use *Settings → OpenSCAD path…* or set
`OPENSCAD_BINARY`.

Try `examples/parametric_mount.scad` (opened at start-up) and
`examples/pcb_project/` (multi-file project with the PCB library).

## The Inspector

1. Compile (F5, or automatically while typing).
2. Click any part of the model. A yellow box outlines the primitive; the
   Inspector lists its parameters grouped by where they come from:
   * the primitive call (`cylinder(d = hole_d, …)`),
   * each enclosing module call (`led(c = "lime")`, `standoff()`), with
     all parameters — also defaults that were not passed,
   * `color()`, transforms, `for` loops (the loop value of the clicked instance)
     and `if` conditions,
   * the global variables used by any of these.
3. Edit a value and press **Enter** (mouse wheel or ↑/↓ nudge the number under
   the cursor; Shift ×0.1, Ctrl ×10). Colors open a color picker with live
   preview. The **Apply to** column chooses where the edit goes:
   * `variable x → value · config.scad:3` — change the definition (all uses),
   * `default of led(d) → value · parts.scad:2` — change the module default,
   * `only this call` — replace the argument at this call, or add a missing
     `name = value` argument to this call only.
4. The model recompiles and the Inspector re-picks the same point.
   Every edit is a normal text edit: `u` in that file undoes it.

Library internals (BOSL2, `lib/`, OpenSCAD library paths) are collapsed; tick
*Library frames* to see them. Double-click any row to jump to its source.

Imported meshes (`import()`), and constructs the tracer cannot evaluate, fall
back to selecting the top-level object.

## Vim

Vim mode is on by default (toolbar checkbox; the setting is remembered). The
mode and pending keys are shown in the status bar.

* **Motions:** `h j k l` `w W b B e E ge gE` `0 ^ $ g_ _ |` `+ - Enter` `gg G {n}G`
  `H M L` `{ }` `( )` `%` `[( [{ ]) ]}` `f F t T ; ,` `/ ? n N * # g* g#`
  `` ` ' `` marks, `Ctrl-O` / `Ctrl-I` jumps, `gd` / `K` / `Ctrl-]` (definition, project-wide)
* **Operators:** `d c y > < =` (re-indent) `g~ gu gU` `gc` (toggle `//` comments) — with any
  motion or text object, `dd cc yy >> gcc …`, counts everywhere (`d3w`, `3dd`, `2>>`)
* **Text objects:** `iw aw iW aW is as ip ap i( a( ib ab i{ a{ iB aB i[ a[ i< a< i" a" i' a'`
  (repeat `i(` in visual mode to grow to the outer block)
* **Editing:** `x X s S r R J gJ D C Y p P gp gP ~ u Ctrl-R .`, `Ctrl-A` / `Ctrl-X`
  increment numbers (handy for dimensions), `i a I A o O gi gI`
* **Visual:** `v V Ctrl-V`, `o`, `gv`; block `I` / `A` insert on all lines, `r`, `c`, `p`
* **Registers:** `"a`–`"z`, `"A` (append), `"0`–`"9`, `"-`, `"_`, `"+`/`"*` (system clipboard).
  `clipboard=unnamedplus` is the default, so `y` / `p` work with other applications.
* **Macros & repeat:** `qa … q`, `@a`, `@@`, `3@a`, `.` with counts
* **Insert mode:** auto-indent, `{` Enter `}` block expansion, `Ctrl-W Ctrl-U`,
  `Ctrl-R {reg}`, `Ctrl-O` (one normal command), `Ctrl-T Ctrl-D`, `Ctrl-N` / `Ctrl-Space` completion
* **Ex:** `:w :q :wq :x :q! :wa :e file :{n}` `:s/pat/rep/gi :%s :'<,'>s :& g&`
  `:g/pat/cmd :v/pat/cmd :normal :d :y :m :t :co :j :> :< :sort [n|i|u] :pu`
  `:noh :set (ic scs hls is ws sw ts sts et cb rnu …) :reg :marks :delm :undo :redo`
  `:make` (compile) `:cn :cp :copen` (errors) `:bn :bp :b{n}` (tabs) `:kicad :export :inspect`
* Search uses Vim regex syntax (`\<word\>`, `\(group\)`, `\v` very magic, `\c`), with
  incremental search and match highlighting.

`:set rnu` shows relative line numbers for count-based jumps.

## Errors and warnings

* The live parser marks syntax errors as you type (missing `;`, unclosed
  brackets, …) at the exact line and column.
* After a compile, every `ERROR:` / `WARNING:` from OpenSCAD is shown in the
  file where it occurs — also in included / used files, which open in a tab when
  you click the entry. `TRACE` lines (the chain of module calls) appear under
  the error. A short hint explains the common cases (unknown variable from a
  `use <>` file, misspelled parameter, missing `;` reported on the next line…).
* A failed `assert()` or other fatal error keeps the previous mesh visible.

## PCB → KiCad workflow

1. `PCB → Insert PCB library + example board` copies
   [`assets/scadforge/pcb.scad`](assets/scadforge/pcb.scad) into your project
   (`lib/scadforge/pcb.scad`) and adds an example.
2. Design with ordinary OpenSCAD, using:
   * `pcb_board(size, thickness, radius, outline) { cutters }`
   * `pcb_hole(pos, d)`, `pcb_mounting_hole(ref, pos, d, footprint)`
   * `pcb_part(ref, footprint, pos, rot, side, body, color, value) { 3D body }`
   * `pcb_connector(...)` — same as a part, different default look.

   They can live inside your own modules, loops and transforms; the export uses
   their real world position.
3. `PCB → Export KiCad board…` (`Ctrl+Shift+K`, `:kicad`) writes
   `name.kicad_pcb` and `name-placement.csv`.
4. In KiCad open the board and run **Tools → Update Footprints from Library**
   to load the pads of every placed footprint, then add the schematic netlist
   (*Update PCB from Schematic*, matching by reference) and route.

Without `pcb_board()`, the outline comes from an OpenSCAD cross-section through
the model (the construction plane if shown, otherwise near the bottom of the
model). The construction plane panel can also export any section as a DXF for
KiCad Edge.Cuts.

## How compiling works

* The UI wraps each top-level object in a marker and runs OpenSCAD **once** per
  animation frame to export evaluated CSG.
* `rust-engine` splits each object into pieces (unions, groups, colors and
  transforms are distributed; `difference`, `hull`, extrusions … stay intact,
  so the result is still exact OpenSCAD geometry), keys pieces by their code
  without the placement, and meshes unique pieces in parallel
  (`SCADFORGE_WORKERS`, default: CPU count). Results are cached in
  `.scadforge-cache/` by content (including bytes of imported files).
* Meshes are loaded with NumPy and instanced on the GPU side; nothing heavy runs
  on the UI thread.
* Projects compile in place; when unsaved buffers differ from disk, the tree is
  mirrored with hard links and only edited files are written.
* *Draft mesh* caps `$fn` at 24 for preview only.

Environment knobs: `OPENSCAD_BINARY`, `SCADFORGE_ENGINE`, `SCADFORGE_USE_RUST=0`,
`SCADFORGE_WORKERS`, `SCADFORGE_MAX_TASKS`, `SCADFORGE_TIMEOUT_SECONDS`,
`SCADFORGE_MAX_MESH_MB`, `SCADFORGE_CACHE_DIR`, `SCADFORGE_STRICT=1`.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests            # Vim engine, parser/tracer, inspector, KiCad, Qt editor
(cd rust-engine && cargo test)
```

Layout:

| Path | |
|---|---|
| `scadforge/vim.py` | Qt-independent Vim engine (fully unit tested) |
| `scadforge/editor.py` | Qt editor: Vim adapter, highlighting, completion, diagnostics |
| `scadforge/lang.py` | OpenSCAD parser with exact source spans |
| `scadforge/trace.py` | Evaluator / tracer: primitives, world transforms, call chains |
| `scadforge/inspector.py`, `inspector_panel.py` | Value provenance and the Inspector UI |
| `scadforge/diagnostics.py`, `problems.py` | Error parsing / mapping and the Problems panel |
| `scadforge/kicad.py` | `.kicad_pcb` / CSV export |
| `scadforge/native_engine.py`, `meshio.py` | Rust engine bridge, NumPy STL loading and instancing |
| `rust-engine/` | Parallel, content-addressed OpenSCAD scheduler |
| `assets/scadforge/pcb.scad` | PCB helper library |
| `assets/BOSL2/` | Bundled [BOSL2](https://github.com/BelfrySCAD/BOSL2) (BSD-2) for the thread tool |

Third-party: BOSL2 © Revar Desmera and contributors, BSD 2-Clause (see
`assets/BOSL2/LICENSE`).
