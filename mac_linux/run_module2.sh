#!/usr/bin/env bash
# Module 2 - undergraduate programs page of every institution (Column D)
# Needs output/01_websites.xlsx. Output: output/02_programs.xlsx
# Run:  bash mac_linux/run_module2.sh            (options, e.g. --state KY)
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "Please run: bash mac_linux/setup.sh"; exit 1; }
echo "=== Module 2: undergraduate programs pages -> output/02_programs.xlsx ==="
.venv/bin/python run.py m2 "$@"
echo
echo "Finished. Check the Review sheet of output/02_programs.xlsx (programs_url_override / client_note),"
echo "then run:  bash mac_linux/run_module3.sh"
