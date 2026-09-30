"""Console + file logging."""
from __future__ import annotations

import logging
import time
from pathlib import Path

NOISY = ("httpx", "httpcore", "urllib3", "selenium", "undetected_chromedriver", "uc", "anthropic", "filelock",
         "tldextract", "hpack")


def setup_logging(name: str, log_dir: Path, verbose: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    file = logging.FileHandler(log_file, encoding="utf-8")
    file.setLevel(logging.DEBUG)
    file.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(file)

    for noisy in NOISY:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file
