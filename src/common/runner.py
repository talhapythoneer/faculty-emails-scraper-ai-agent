"""Run one function per institution on a thread pool with a progress bar (Ctrl+C safe on Windows)."""
from __future__ import annotations

import logging
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable

from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

log = logging.getLogger(__name__)


def run_parallel(items: list, fn: Callable, workers: int, desc: str, on_result: Callable,
                 label: Callable = str) -> None:
    if not items:
        return
    with logging_redirect_tqdm():
        bar = tqdm(total=len(items), desc=desc, unit="inst", dynamic_ncols=True)
        executor = ThreadPoolExecutor(max_workers=max(1, int(workers)))
        futures = {executor.submit(fn, item): item for item in items}
        pending = set(futures)
        try:
            while pending:
                done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for f in done:
                    item = futures[f]
                    try:
                        result = f.result()
                    except Exception as e:
                        log.exception("Failed: %s", label(item))
                        result = {"status": "error", "notes": f"error: {e}"}
                    try:
                        on_result(item, result)
                    except Exception:
                        log.exception("Could not save the result for %s", label(item))
                    bar.update(1)
        except KeyboardInterrupt:
            log.warning("Interrupted - cancelling queued institutions (the ones in progress finish first)...")
            executor.shutdown(wait=False, cancel_futures=True)
            bar.close()
            raise
        executor.shutdown(wait=True)
        bar.close()
