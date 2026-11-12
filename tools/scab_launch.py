"""Run a SCAB entry point with SCAB's own Python environment, from any working directory.

    python tools/scab_launch.py cli check design.scad
    python tools/scab_launch.py mcp                       (the MCP server on stdio)

Uses .venv (created by run.sh / run.bat) when it exists, else the Python running this script.
"""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
MODULES = {'cli': 'scab.cli', 'mcp': 'scab.mcp_server'}


def venv_python():
    for candidate in (ROOT / '.venv' / 'bin' / 'python', ROOT / '.venv' / 'Scripts' / 'python.exe'):
        if candidate.is_file():
            return str(candidate)
    return None


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in MODULES:
        sys.stderr.write('usage: scab_launch.py cli|mcp [args...]\n')
        return 2
    module = MODULES[sys.argv[1]]
    python = venv_python()
    env = dict(os.environ, PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get('PYTHONPATH', ''))
    if python and Path(python).resolve() != Path(sys.executable).resolve():
        return subprocess.call([python, '-m', module] + sys.argv[2:], env=env)
    sys.path.insert(0, str(ROOT))
    sys.argv = [module] + sys.argv[2:]
    if module == 'scab.cli':
        from scab.cli import main as run
        return run()
    from scab.mcp_server import serve
    serve()
    return 0


if __name__ == '__main__':
    sys.exit(main())
