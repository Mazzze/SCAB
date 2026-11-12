# SCAB

Two kinds of work happen in this repository:

- **Designing with SCAB** means OpenSCAD enclosures and brackets around KiCad
  boards. Read `AGENTS.md` first. Use the `scab` MCP tools from `.mcp.json`,
  or `tools/scab-cli`, to read boards and to check every change
  (`scab_check` / `scab-cli check`).
- **Developing SCAB itself** means the Python/Qt app in `scab/` and the Rust
  engine in `rust-engine/`. See "Development" in `README.md`. Run the tests
  with `QT_QPA_PLATFORM=offscreen python -m pytest -q`. Regenerate the example
  boards with `python tools/make_example_board.py`.
