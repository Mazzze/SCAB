"""Check the complete native stack before importing Qt or NumPy."""
from importlib import metadata
from pathlib import Path
import platform
import sys


def expected_versions():
    source=Path(__file__).resolve().parent.parent/'requirements.txt'
    out={}
    for line in source.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#') or line.endswith('# optional'):continue
        name,version=line.split('#',1)[0].strip().split('==',1)
        out[name]=version
    return out


def python_issue():
    if platform.python_implementation()!='CPython' or sys.version_info[:2]<(3,10):
        return 'SCAB requires CPython 3.10 or newer.'
    return None


def version_issues():
    issues=[]
    for package,wanted in expected_versions().items():
        try: actual=metadata.version(package)
        except metadata.PackageNotFoundError: actual='missing'
        if actual!=wanted: issues.append(f'{package}: installed {actual}, required {wanted}')
    return issues


def ensure_or_exit():
    """Refuse to start with a missing dependency; warn (only) about other versions.

    SCAB_STRICT=1 restores the strict exact-version check.
    """
    import os
    issue=python_issue()
    issues=version_issues()
    missing=[line for line in issues if 'installed missing' in line]
    strict=os.environ.get('SCAB_STRICT')=='1'
    if issue or missing or (strict and issues):
        print('SCAB cannot start with this environment.',file=sys.stderr)
        if issue: print(issue,file=sys.stderr)
        for line in (issues if strict else missing): print('  '+line,file=sys.stderr)
        print('Run bash run.sh (Linux/macOS) or run.bat (Windows) to create the isolated app environment.',file=sys.stderr)
        raise SystemExit(2)
    if issues:
        print('SCAB: untested package versions (set SCAB_STRICT=1 to refuse):',file=sys.stderr)
        for line in issues: print('  '+line,file=sys.stderr)
