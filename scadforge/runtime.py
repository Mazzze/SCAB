"""Check the complete native stack before importing Qt or NumPy."""
from importlib import metadata
from pathlib import Path
import platform
import sys


def expected_versions():
    source=Path(__file__).resolve().parent.parent/'requirements.txt'
    return dict(line.strip().split('==',1) for line in source.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith('#'))


def python_issue():
    if platform.python_implementation()!='CPython' or not ((3,10)<=sys.version_info[:2]<(3,14)):
        return 'SCAD Forge 0.7 requires CPython 3.10–3.13 for its pinned Qt wheels.'
    return None


def version_issues():
    issues=[]
    for package,wanted in expected_versions().items():
        try: actual=metadata.version(package)
        except metadata.PackageNotFoundError: actual='missing'
        if actual!=wanted: issues.append(f'{package}: installed {actual}, required {wanted}')
    return issues


def ensure_or_exit():
    issue=python_issue()
    issues=version_issues()
    if issue or issues:
        print('SCAD Forge cannot start safely with this environment.',file=sys.stderr)
        if issue: print(issue,file=sys.stderr)
        for line in issues: print('  '+line,file=sys.stderr)
        print('Run bash run.sh (Linux) or run.bat (Windows) to create the isolated app environment.',file=sys.stderr)
        raise SystemExit(2)
