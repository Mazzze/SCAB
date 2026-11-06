@echo off
rem SCAB launcher (Windows): isolated venv, optional Rust engine build, start.
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Creating Python environment in .venv
    py -3 -m venv .venv || python -m venv .venv
)
fc /b requirements.txt .venv\requirements.stamp >nul 2>&1
if errorlevel 1 (
    .venv\Scripts\python.exe -m pip install --upgrade pip
    findstr /v /c:"# optional" requirements.txt > .venv\required.txt
    .venv\Scripts\python.exe -m pip install -r .venv\required.txt && copy /y requirements.txt .venv\requirements.stamp >nul
    .venv\Scripts\python.exe -m pip install cascadio==0.1.1
)
where cargo >nul 2>&1
if not errorlevel 1 (
    if not exist rust-engine\target\release\scab-engine.exe (
        echo Building the parallel Rust compile engine
        pushd rust-engine
        cargo build --release
        popd
    )
)
.venv\Scripts\python.exe -m scab %*
