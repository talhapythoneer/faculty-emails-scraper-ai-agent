"""Faculty Emails Extractor - command line.

The simplest way to run it is the scripts in the project folder (run_module1.bat ... run_module3.bat on Windows,
mac_linux/run_module*.sh on macOS / Linux). They call this file; options typed after them are passed on.

    python run.py m1 --state KY --limit 5      # official websites (Google: Serper API or Chrome)
    python run.py m2 --state KY                # undergraduate programs pages (Google first, crawl if unsure)
    python run.py m2 --google-only             # Google only; weak rows are crawled on the next normal run
    python run.py m3 --state KY                # majors + faculty emails -> output/03_final.xlsx
    python run.py m3 --export-only             # rebuild the spreadsheet without processing
    python run.py m3 --retry-blocked           # re-run only institutions whose site showed an unsolved CAPTCHA
    python run.py cost                         # AI tokens + Serper searches, estimated cost so far
"""
from __future__ import annotations

import argparse
import sys


def show_cost(cfg: dict) -> None:
    from src.common.config import resolve_path
    from src.common.db import DB

    db = DB(resolve_path(cfg["paths"]["db_file"]))
    searches = db.count_pages("serper")
    print(f"Serper API searches so far: {searches:,} (2,500 free at signup, then about ${searches / 1000:.2f} "
          f"at $1 per 1,000)\n")
    llm = cfg.get("llm", {})
    p_in, p_out = float(llm.get("price_input_per_mtok", 1.0)), float(llm.get("price_output_per_mtok", 5.0))
    rows = db.llm_usage_summary()
    if not rows:
        print("No AI calls recorded yet.")
        return
    total = 0.0
    print(f"{'model':<20} {'purpose':<20} {'calls':>6} {'input tok':>11} {'output tok':>11} {'cost $':>8}")
    for model, purpose, calls, tin, tout, _ in rows:
        cost = (tin or 0) / 1e6 * p_in + (tout or 0) / 1e6 * p_out
        total += cost
        print(f"{model:<20} {purpose:<20} {calls:>6} {tin or 0:>11,} {tout or 0:>11,} {cost:>8.2f}")
    n = db.llm_institution_count()
    print(f"\nTotal: ${total:.2f} across {n} institutions that used AI"
          + (f" (~${total / n:.3f} each)" if n else ""))
    print(f"Prices used: ${p_in}/M input, ${p_out}/M output tokens (edit llm.price_* in config.yaml).")


def main(argv=None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Faculty Emails Extractor")
    parser.add_argument("module", choices=["m1", "m2", "m3", "cost"],
                        help="m1 = websites, m2 = programs pages, m3 = majors + emails, cost = AI/API usage")
    parser.add_argument("--state", action="append", help="State name or 2-letter code (repeatable)")
    parser.add_argument("--unitid", action="append", help="Only this unitid (repeatable)")
    parser.add_argument("--limit", type=int, help="Process at most N institutions this run")
    parser.add_argument("--force", action="store_true", help="Re-process rows that are already done")
    parser.add_argument("--no-cache", action="store_true", help="Download pages / search Google again")
    parser.add_argument("--no-browser", action="store_true", help="Never open Chrome (m2/m3)")
    parser.add_argument("--workers", type=int, help="Parallel institutions (m2/m3)")
    parser.add_argument("--export-only", action="store_true", help="Only rebuild the output spreadsheet")
    parser.add_argument("--google-only", action="store_true",
                        help="m2: use Google results only, do not crawl the websites")
    parser.add_argument("--retry-blocked", action="store_true",
                        help="m3: re-run only the institutions whose website blocked us with a CAPTCHA last time")
    parser.add_argument("--config", help="Path to an alternative config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug messages in the console")
    args = parser.parse_args(argv)

    from src.common.config import load_config, resolve_path
    from src.common.log import setup_logging

    cfg = load_config(args.config)
    if args.module == "cost":
        show_cost(cfg)
        return
    log_file = setup_logging(args.module, resolve_path(cfg["paths"]["log_dir"]), args.verbose)
    print(f"Log file: {log_file}")

    if args.module == "m1":
        from src import m1_websites as module
    elif args.module == "m2":
        from src import m2_programs as module
    else:
        from src import m3_faculty as module
    module.run(cfg, args)


if __name__ == "__main__":
    main()
