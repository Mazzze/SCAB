"""SCAB command line for scripts and AI agents. Every command prints JSON.

    python -m scab.cli info design.scad              boards (pose, size, holes, parts), variables, objects
    python -m scab.cli board path/to/board.kicad_pcb  one KiCad board as data
    python -m scab.cli link design.scad board.kicad_pcb [--id display] [--pose 0,0,10,0,0,0]
    python -m scab.cli unlink design.scad display
    python -m scab.cli pose design.scad display [x y z rx ry rz]
    python -m scab.cli update design.scad            rewrite kicad/<id>.scad for every linked board
    python -m scab.cli check design.scad [--min 1]   syntax + OpenSCAD errors + clearance (exit 1 when not ok)
    python -m scab.cli clearance design.scad [--min 1]
    python -m scab.cli render design.scad out.png [--view iso|top|front|right|back|left|bottom] [--no-boards]
    python -m scab.cli export design.scad out.stl|out.step
    python -m scab.cli api display                   the names a board include defines
    python -m scab.cli init path/to/project          AGENTS.md + .mcp.json there, so an agent can use SCAB

See AGENTS.md for the design rules and a worked example.
"""
import argparse
import json
import sys


def _floats(text, count):
    values = [float(v) for v in str(text).replace(' ', '').split(',') if v != '']
    if len(values) != count:
        raise argparse.ArgumentTypeError(f'expected {count} comma separated numbers')
    return values


def init_project(folder, force=False):
    """Copy the agent guide into a project and register SCAB's MCP server there."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    written, kept = [], []
    launcher = root / 'tools' / 'scab_launch.py'
    files = {
        'AGENTS.md': (root / 'AGENTS.md').read_text(encoding='utf-8').replace(
            '`tools/scab-cli`', f'`{root / "tools" / "scab-cli"}`').replace(
            'examples/agent_enclosure/enclosure.scad', str(root / 'examples' / 'agent_enclosure' / 'enclosure.scad')),
        '.mcp.json': json.dumps({'mcpServers': {'scab': {'command': 'python3' if sys.platform != 'win32' else 'python',
                                                         'args': [str(launcher), 'mcp']}}}, indent=2) + '\n',
        'CLAUDE.md': 'This folder holds SCAB designs (OpenSCAD around KiCad boards). Read AGENTS.md before designing; '
                     'use the `scab` MCP tools (or the scab-cli command) to inspect boards and to check every change.\n',
    }
    for name, text in files.items():
        target = folder / name
        if target.exists() and not force:
            kept.append(str(target))
            continue
        target.write_text(text, encoding='utf-8')
        written.append(str(target))
    return {'written': written, 'kept (use --force)': kept}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='scab-cli', description='SCAB tools for scripts and AI agents (JSON output)')
    parser.add_argument('--openscad', help='OpenSCAD executable (default: OPENSCAD_BINARY or PATH)')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('info', help='design summary: boards, poses, include API, variables, objects')
    p.add_argument('scad')
    p = sub.add_parser('board', help='one KiCad board as data')
    p.add_argument('pcb')
    p.add_argument('--no-parts', action='store_true')
    p = sub.add_parser('link', help='link a KiCad board to a design')
    p.add_argument('scad'); p.add_argument('pcb')
    p.add_argument('--id'); p.add_argument('--pose', help='x,y,z,rx,ry,rz')
    p = sub.add_parser('unlink', help='remove a board link (and its pose / include lines)')
    p.add_argument('scad'); p.add_argument('id')
    p = sub.add_parser('pose', help='read or set a board pose')
    p.add_argument('scad'); p.add_argument('id'); p.add_argument('values', nargs='*', type=float)
    p = sub.add_parser('update', help='rewrite the kicad/<id>.scad includes')
    p.add_argument('scad')
    for name in ('check', 'clearance'):
        p = sub.add_parser(name)
        p.add_argument('scad'); p.add_argument('--min', type=float, default=1.0, help='minimum clearance in mm')
        p.add_argument('--all', action='store_true', help='list every part, not only problems')
    p = sub.add_parser('render', help='PNG of the design with its boards')
    p.add_argument('scad'); p.add_argument('png')
    p.add_argument('--view', default='iso'); p.add_argument('--size', default='1200x900')
    p.add_argument('--no-boards', action='store_true')
    p = sub.add_parser('export', help='.stl (chassis only) or .step (chassis + boards)')
    p.add_argument('scad'); p.add_argument('output')
    p = sub.add_parser('api', help='names defined by a board include')
    p.add_argument('id')
    p = sub.add_parser('init', help='set up a project folder for AI agents (AGENTS.md, .mcp.json, CLAUDE.md)')
    p.add_argument('folder')
    p.add_argument('--force', action='store_true', help='overwrite existing files')
    args = parser.parse_args(argv)

    from . import headless as h
    exe = args.openscad
    code = 0
    try:
        if args.command == 'info':
            out = h.info(args.scad)
        elif args.command == 'board':
            out = h.board_summary(args.pcb, parts=not args.no_parts)
        elif args.command == 'link':
            pose = None
            if args.pose:
                v = _floats(args.pose, 6)
                pose = (v[:3], v[3:])
            out = h.link(args.scad, args.pcb, args.id, pose, exe)
        elif args.command == 'unlink':
            out = h.unlink(args.scad, args.id)
        elif args.command == 'pose':
            if args.values:
                if len(args.values) != 6:
                    parser.error('pose needs x y z rx ry rz')
                out = h.set_pose(args.scad, args.id, args.values[:3], args.values[3:])
            else:
                board = next((b for b in h.info(args.scad)['boards'] if b['id'] == args.id), None)
                if board is None:
                    raise ValueError(f'no board {args.id!r}')
                out = {'id': args.id, 'pose': board['pose']}
        elif args.command == 'update':
            out = {'written': h.update_includes(args.scad, exe)}
        elif args.command == 'check':
            out = h.check_design(args.scad, args.min, exe)
            if not args.all and 'clearance' in out:
                out['clearance'].pop('parts', None)
            code = 0 if out.get('ok') else 1
        elif args.command == 'clearance':
            out = h.clearance(args.scad, args.min, exe)
            if not args.all:
                out['parts'] = [r for r in out['parts'] if r['status'] != 'ok']
            code = 1 if out['counts']['collision'] else 0
        elif args.command == 'render':
            w, _, ht = args.size.partition('x')
            out = h.render_png(args.scad, args.png, args.view, (int(w), int(ht or w)), not args.no_boards, exe)
        elif args.command == 'export':
            out = h.export(args.scad, args.output, exe)
            code = 0 if out.get('ok') else 1
        elif args.command == 'api':
            out = h.board_api(args.id)
        elif args.command == 'init':
            out = init_project(args.folder, args.force)
    except (OSError, ValueError, RuntimeError) as error:
        out = {'error': str(error)}
        code = 2
    json.dump(out, sys.stdout, indent=1, default=str)
    sys.stdout.write('\n')
    return code


if __name__ == '__main__':
    sys.exit(main())
