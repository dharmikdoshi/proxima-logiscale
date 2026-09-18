import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime

from .config import Config


@contextmanager
def track(cfg: Config, step: str):
    """One line in data/runs.jsonl per step: what ran, when, how long, did it pass.
    In a real system this would be a table in postgres, a file is enough here."""
    entry = {"step": step, "scale": cfg.label,
             "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
             "status": "done", "seconds": None, "error": None}
    begin = time.perf_counter()
    try:
        yield entry
    except BaseException as err:   # ctrl+c also lands here, we still want that noted
        entry["status"] = "failed"
        entry["error"] = repr(err)
        raise
    finally:
        entry["seconds"] = round(time.perf_counter() - begin, 2)
        cfg.data_root.mkdir(parents=True, exist_ok=True)
        with (cfg.data_root / "runs.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
