#!/usr/bin/env bash
# Module 1 - official website of every institution (Google search)
# Output: output/01_websites.xlsx
# Run:  bash mac_linux/run_module1.sh            (options, e.g. --state KY --limit 5)
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "Please run: bash mac_linux/setup.sh"; exit 1; }
echo "=== Module 1: official websites -> output/01_websites.xlsx ==="
.venv/bin/python run.py m1 "$@"
echo
echo "Finished. Check the Review sheet of output/01_websites.xlsx (fix wrong sites in url_override),"
echo "then run:  bash mac_linux/run_module2.sh"
