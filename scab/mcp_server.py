"""SCAB as an MCP server (Model Context Protocol, stdio): tools an AI agent can call while it designs.

Start:  python -m scab.mcp_server        (from the SCAB folder, with SCAB's Python environment)
Claude Code picks it up from .mcp.json in this repository; other MCP clients take the same command.

No extra packages: newline-delimited JSON-RPC 2.0 on stdin / stdout. Anything a library prints is
sent to stderr so it cannot corrupt the protocol stream.
"""
import base64
import json
import os
from pathlib import Path
import sys
import traceback

PROTOCOL = '2025-06-18'

STRING = {'type': 'string'}
NUMBER = {'type': 'number'}
SCAD = {'type': 'string', 'description': 'Path to the .scad design file'}

TOOLS = [
    {'name': 'scab_info',
     'description': 'Summary of a SCAD design: linked KiCad boards (id, pose, size, thickness, mounting holes, parts with '
                    'positions and 3D boxes, the include API names), top-level variables, modules, objects and SCAB groups. '
                    'Call this first.',
     'inputSchema': {'type': 'object', 'properties': {'scad': SCAD}, 'required': ['scad']}},
    {'name': 'scab_board',
     'description': 'A KiCad .kicad_pcb as data: outline box and size, thickness, every drilled hole, copper layers, '
                    'stackup colors and every part (ref, value, side, position, rotation, 3D box, height above the board).',
     'inputSchema': {'type': 'object', 'properties': {'pcb': {'type': 'string', 'description': 'Path to the .kicad_pcb'},
                                                      'parts': {'type': 'boolean', 'default': True}}, 'required': ['pcb']}},
    {'name': 'scab_link_board',
     'description': 'Link a KiCad board to a design: adds `// scab: kicad <id> = path`, `<id>_pose = [[x,y,z],[rx,ry,rz]];` '
                    'and `include <kicad/<id>.scad>`, and writes that include (outline, holes, parts, helper modules). '
                    'The first board gets id "kicad" unless an id is given.',
     'inputSchema': {'type': 'object', 'properties': {
         'scad': SCAD, 'pcb': STRING, 'id': {'type': 'string', 'description': 'Board id (identifier)'},
         'pose': {'type': 'array', 'items': NUMBER, 'minItems': 6, 'maxItems': 6,
                  'description': '[x, y, z, rx, ry, rz] in mm / degrees'}}, 'required': ['scad', 'pcb']}},
    {'name': 'scab_unlink_board',
     'description': 'Remove a board link, its pose and its include line from a design.',
     'inputSchema': {'type': 'object', 'properties': {'scad': SCAD, 'id': STRING}, 'required': ['scad', 'id']}},
    {'name': 'scab_set_pose',
     'description': 'Place a linked board: writes <id>_pose = [[x, y, z], [rx, ry, rz]] (translate, then rotate).',
     'inputSchema': {'type': 'object', 'properties': {
         'scad': SCAD, 'id': STRING,
         'position': {'type': 'array', 'items': NUMBER, 'minItems': 3, 'maxItems': 3},
         'rotation': {'type': 'array', 'items': NUMBER, 'minItems': 3, 'maxItems': 3}},
         'required': ['scad', 'id', 'position']}},
    {'name': 'scab_update_includes',
     'description': 'Re-read every linked KiCad board and rewrite kicad/<id>.scad (do this after the board changed in KiCad).',
     'inputSchema': {'type': 'object', 'properties': {'scad': SCAD}, 'required': ['scad']}},
    {'name': 'scab_check',
     'description': 'Verify a design: syntax, OpenSCAD errors/warnings (with line numbers and hints), then the clearance '
                    'of every board part against the chassis and the other boards (collision / tight / ok, headroom, gap). '
                    'ok is true when it compiles without errors and nothing collides.',
     'inputSchema': {'type': 'object', 'properties': {
         'scad': SCAD, 'minimum': {'type': 'number', 'default': 1.0, 'description': 'Wanted clearance in mm'},
         'all_parts': {'type': 'boolean', 'default': False}}, 'required': ['scad']}},
    {'name': 'scab_render',
     'description': 'Render a PNG of the design with its boards placed (returns the image). Views: iso, top, front, right, '
                    'back, left, bottom.',
     'inputSchema': {'type': 'object', 'properties': {
         'scad': SCAD, 'png': {'type': 'string', 'description': 'Output path (default: next to the design)'},
         'view': {'type': 'string', 'default': 'iso'}, 'boards': {'type': 'boolean', 'default': True},
         'width': {'type': 'integer', 'default': 1000}, 'height': {'type': 'integer', 'default': 750}},
         'required': ['scad']}},
    {'name': 'scab_export',
     'description': 'Export: .stl = the chassis only (boards left out), .step = chassis + every board with its 3D parts.',
     'inputSchema': {'type': 'object', 'properties': {'scad': SCAD, 'output': STRING}, 'required': ['scad', 'output']}},
    {'name': 'scab_board_api',
     'description': 'The OpenSCAD names a board include defines for a board id, with their meaning.',
     'inputSchema': {'type': 'object', 'properties': {'id': STRING}, 'required': ['id']}},
]


def call(name, args):
    """Run one tool; returns MCP content items."""
    from . import headless as h
    exe = os.environ.get('OPENSCAD_BINARY') or None
    if name == 'scab_info':
        result = h.info(args['scad'])
    elif name == 'scab_board':
        result = h.board_summary(args['pcb'], parts=args.get('parts', True))
    elif name == 'scab_link_board':
        pose = args.get('pose')
        result = h.link(args['scad'], args['pcb'], args.get('id'), (pose[:3], pose[3:]) if pose else None, exe)
    elif name == 'scab_unlink_board':
        result = h.unlink(args['scad'], args['id'])
    elif name == 'scab_set_pose':
        result = h.set_pose(args['scad'], args['id'], args['position'], args.get('rotation') or [0, 0, 0])
    elif name == 'scab_update_includes':
        result = {'written': h.update_includes(args['scad'], exe)}
    elif name == 'scab_check':
        result = h.check_design(args['scad'], args.get('minimum', 1.0), exe)
        if not args.get('all_parts') and 'clearance' in result:
            result['clearance'].pop('parts', None)
    elif name == 'scab_render':
        scad = Path(args['scad'])
        png = Path(args.get('png') or scad.with_name(scad.stem + f'-{args.get("view", "iso")}.png'))
        result = h.render_png(scad, png, args.get('view', 'iso'), (args.get('width', 1000), args.get('height', 750)),
                              args.get('boards', True), exe)
        data = base64.b64encode(png.read_bytes()).decode('ascii')
        return [{'type': 'text', 'text': json.dumps(result)}, {'type': 'image', 'data': data, 'mimeType': 'image/png'}]
    elif name == 'scab_export':
        result = h.export(args['scad'], args['output'], exe)
    elif name == 'scab_board_api':
        result = h.board_api(args['id'])
    else:
        raise ValueError(f'unknown tool {name}')
    return [{'type': 'text', 'text': json.dumps(result, indent=1, default=str)}]


def handle(message):
    method = message.get('method')
    params = message.get('params') or {}
    if method == 'initialize':
        return {'protocolVersion': params.get('protocolVersion', PROTOCOL),
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': 'scab', 'version': _version()},
                'instructions': 'SCAB designs OpenSCAD enclosures around live KiCad boards. Read AGENTS.md in the SCAB '
                                'folder. Typical loop: scab_info -> edit the .scad -> scab_check -> scab_render.'}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method == 'tools/call':
        try:
            return {'content': call(params.get('name'), params.get('arguments') or {}), 'isError': False}
        except Exception as error:                      # report tool failures to the agent, keep serving
            traceback.print_exc(file=sys.stderr)
            return {'content': [{'type': 'text', 'text': f'{type(error).__name__}: {error}'}], 'isError': True}
    raise LookupError(method)


def _version():
    try:
        from . import __version__
        return __version__
    except ImportError:
        return '0'


def serve(stdin=None, stdout=None):
    if stdout is None:
        # keep the real stdout for the protocol; everything else printed goes to stderr
        stdout = os.fdopen(os.dup(1), 'w', buffering=1, encoding='utf-8')
        os.dup2(2, 1)
        sys.stdout = sys.stderr
    stdin = stdin or sys.stdin
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}) + '\n')
            stdout.flush()
            continue
        if 'id' not in message:
            continue                                      # notification (e.g. notifications/initialized)
        try:
            reply = {'jsonrpc': '2.0', 'id': message['id'], 'result': handle(message)}
        except LookupError:
            reply = {'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32601, 'message': f'method not found: {message.get("method")}'}}
        except Exception as error:
            reply = {'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32603, 'message': str(error)}}
        stdout.write(json.dumps(reply) + '\n')
        stdout.flush()


if __name__ == '__main__':
    serve()
