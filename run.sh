#!/usr/bin/env bash
# SCAB launcher (Linux / macOS): isolated venv, optional Rust engine build, start.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
VENV=".venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating Python environment in $VENV"
    "$PYTHON" -m venv "$VENV"
fi
STAMP="$VENV/.requirements"
if [ ! -f "$STAMP" ] || ! cmp -s requirements.txt "$STAMP"; then
    "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
    grep -v '# optional' requirements.txt > "$VENV/.required.txt"
    "$VENV/bin/python" -m pip install -r "$VENV/.required.txt"
    # optional extras (STEP models); the app works without them
    grep '# optional' requirements.txt | sed 's/#.*//' | xargs -r "$VENV/bin/python" -m pip install || echo "Optional packages skipped"
    cp requirements.txt "$STAMP"
fi
ENGINE="rust-engine/target/release/scab-engine"
if command -v cargo >/dev/null 2>&1; then
    if [ ! -x "$ENGINE" ] || [ -n "$(find rust-engine/src rust-engine/Cargo.toml -newer "$ENGINE" 2>/dev/null)" ]; then
        echo "Building the parallel Rust compile engine"
        (cd rust-engine && cargo build --release) || echo "Rust engine build failed; using the slower Python compiler"
    fi
elif [ ! -x "$ENGINE" ]; then
    echo "Tip: install Rust (https://rustup.rs) for much faster compiles of large models."
fi
if ! command -v openscad >/dev/null 2>&1 && [ -z "${OPENSCAD_BINARY:-}" ]; then
    echo "OpenSCAD was not found on PATH. Install it or set OPENSCAD_BINARY (or Settings → OpenSCAD path)."
fi
exec "$VENV/bin/python" -m scab "$@"
