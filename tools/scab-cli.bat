@echo off
rem SCAB command line (JSON output) - see AGENTS.md.  Example: tools\scab-cli check design.scad
python "%~dp0scab_launch.py" cli %*
