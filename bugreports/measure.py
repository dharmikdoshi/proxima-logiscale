import os
import sys
import time
from contextlib import contextmanager

import psutil


def machine() -> dict:
    return {"cpu_threads": os.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
            "python": sys.version.split()[0], "os": sys.platform}


def peak_memory_mb() -> int:
    info = psutil.Process().memory_info()
    if hasattr(info, "peak_wset"):   # windows
        return round(info.peak_wset / 1e6)
    import resource  # linux, reports KB
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3)


def _cpu_seconds() -> float:
    times = psutil.Process().cpu_times()
    return times.user + times.system


class Steps:
    """Wall time and cpu time of each step, in the order they ran."""

    def __init__(self):
        self.rows: list[dict] = []

    @contextmanager
    def step(self, name: str):
        row = {"step": name}
        wall, cpu = time.perf_counter(), _cpu_seconds()
        try:
            yield row   # the step can add its own numbers, like rows or mb
        finally:
            row["seconds"] = round(time.perf_counter() - wall, 2)
            row["cpu_seconds"] = round(_cpu_seconds() - cpu, 2)
            self.rows.append(row)

    def total(self) -> float:
        return round(sum(r["seconds"] for r in self.rows), 2)

    def print(self) -> None:
        total = self.total() or 1
        print(f"{'seconds':>8} {'share':>6} {'cpu s':>7}  step")
        for r in self.rows:
            extra = ", ".join(f"{k} {v:,}" if isinstance(v, int) else f"{k} {v}"
                              for k, v in r.items() if k not in ("step", "seconds", "cpu_seconds"))
            print(f"{r['seconds']:8.2f} {100 * r['seconds'] / total:5.1f}% {r['cpu_seconds']:7.1f}  "
                  f"{r['step']}{'  [' + extra + ']' if extra else ''}")
        print(f"{self.total():8.2f}  total, peak memory {peak_memory_mb():,} MB")
