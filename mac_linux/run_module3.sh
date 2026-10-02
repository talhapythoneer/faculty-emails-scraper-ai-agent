#!/usr/bin/env bash
# Module 3 - qualifying majors (Column E) and faculty emails (Column F)
# Needs output/02_programs.xlsx. Output: output/03_final.xlsx (updated after every institution)
# Run:  bash mac_linux/run_module3.sh            (options, e.g. --state KY --limit 10,
#                                                 --retry-blocked, --export-only)
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "Please run: bash mac_linux/setup.sh"; exit 1; }
echo "=== Module 3: majors and faculty emails -> output/03_final.xlsx ==="
echo "Keep the computer awake: a Chrome window may open while it runs."
.venv/bin/python run.py m3 "$@"
echo
echo "Finished. The result is output/03_final.xlsx (see its Review sheet for rows to check)."
