import json
import logging
import os
import platform
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

import duckdb

from .config import Config
from .db import connect
from .layouts import ERROR_CODES_FILE, LAYOUTS, ROLLUPS, rollup_file, sizes
from .queries import Query, queries
from .report import print_table, results_dir

log = logging.getLogger(__name__)

RUNS = 7
CSV_RUNS = 3   # csv is slow and steady, 3 runs is enough and saves many minutes
RESULTS_DIR = Path("results")


def bench(cfg: Config, layouts: list[str] | None = None, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or results_dir(cfg)
    results = []
    for layout in layouts or LAYOUTS:
        files = cfg.root / LAYOUTS[layout]
        if not list(cfg.root.glob(LAYOUTS[layout])):
            log.info("%s is not built for %s, skipping", layout, cfg.label)
            continue
        # new connection per layout, so one layout gets no head start from what the last one cached
        con = connect(cfg.root / "tmp")
        add_views(con, cfg, ["events", "error_codes"], layout)
        row_groups = None if layout == "csv" else read_row_groups(con, files)
        for query in queries(cfg):
            if query.only_layouts and layout not in query.only_layouts:
                continue
            times = _time(con, query.sql, CSV_RUNS if layout == "csv" else RUNS)
            # csv has no columns to skip and no min/max, so every question reads the whole file
            scanned, touched = ((files.stat().st_size, 1) if layout == "csv"
                                else estimate_scan(row_groups, query))
            results.append(_result(layout, query, times, scanned, touched))
        con.close()

    if all(rollup_file(cfg, name).exists() for name in ROLLUPS):
        # now the same questions again, but answered from the small summary tables
        con = connect(cfg.root / "tmp")
        add_views(con, cfg, [*ROLLUPS, "error_codes"])
        for query in queries(cfg):
            if query.rollup_sql:
                # these files are tiny, so I just count the whole file of whichever summary was used
                used = [rollup_file(cfg, name) for name in ROLLUPS if name in query.rollup_sql]
                results.append(_result("rollup", query, _time(con, query.rollup_sql, RUNS),
                                       sum(f.stat().st_size for f in used), len(used)))
        con.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"bench_{cfg.label}.json"
    out.write_text(json.dumps({
        "scale": cfg.label, "rows": cfg.rows_per_day * cfg.days, "days": cfg.days,
        "machine": {"os": platform.platform(), "cpu": platform.processor(),
                    "logical_cpus": os.cpu_count(), "python": platform.python_version(),
                    "duckdb": duckdb.__version__},
        "note": "timings are warm (one unmeasured run first). mb_scanned is an estimate "
                "from parquet metadata, see estimate_scan in bench.py",
        "questions": {q.name: q.question for q in queries(cfg)},
        "sizes": sizes(cfg),
        "results": results,
        "distinct_counts": _distinct_counts(cfg),
    }, indent=2))
    return out


def _result(layout: str, query: Query, times: list, scanned: int, touched: int) -> dict:
    log.info("%-11s %-24s median %8.1f ms", layout, query.name, statistics.median(times))
    return {"layout": layout, "query": query.name,
            "median_ms": round(statistics.median(times), 1), "slowest_ms": round(max(times), 1),
            "runs": len(times), "mb_scanned": round(scanned / 1e6, 2), "files_touched": touched}


def _time(con, sql: str, runs: int) -> list[float]:
    con.execute(sql).fetchall()   # warm up, not counted
    times = []
    for _ in range(runs):
        begin = time.perf_counter()
        con.execute(sql).fetchall()
        times.append((time.perf_counter() - begin) * 1000)
    return times


def _median_ms(con, sql: str) -> float:
    return round(statistics.median(_time(con, sql, RUNS)), 1)


def _distinct_counts(cfg: Config) -> dict:
    """Two things I wanted to know about "how many different devices":
    1. how much faster is the approximate count, and how far off is it
    2. what happens if you add up daily unique counts to get a weekly one (spoiler: too high)"""
    layout = "sorted" if list(cfg.root.glob(LAYOUTS["sorted"])) else "by_day"
    con = connect(cfg.root / "tmp")
    add_views(con, cfg, ["events"], layout)
    week = cfg.window_sql(7)

    # the approximate count is not always off by the same amount, it depends on how many devices
    # are in the window. so I check a big, a medium and a small window
    windows = []
    for label, where in (("all days", ""), ("one week", f"WHERE {week}"),
                         ("one day", f"WHERE {cfg.window_sql(1)}")):
        exact_sql = f"SELECT count(DISTINCT device_id) FROM events {where}"
        approx_sql = f"SELECT approx_count_distinct(device_id) FROM events {where}"
        exact, approx = con.execute(exact_sql).fetchone()[0], con.execute(approx_sql).fetchone()[0]
        windows.append({"window": label, "exact": exact, "approx": approx,
                        "error_pct": round(100 * abs(approx - exact) / exact, 2),
                        "exact_median_ms": _median_ms(con, exact_sql),
                        "approx_median_ms": _median_ms(con, approx_sql)})

    failed_this_week = f"FROM events WHERE {week} AND status = 'fail'"
    true_week = con.execute(f"SELECT count(DISTINCT device_id) {failed_this_week}").fetchone()[0]
    added_up = con.execute(f"""SELECT sum(devices) FROM (SELECT count(DISTINCT device_id) AS devices
                               {failed_this_week} GROUP BY day)""").fetchone()[0]
    con.close()
    return {"exact_vs_approx": windows,
            "devices_with_a_failure_this_week": true_week,
            "same_thing_by_adding_daily_counts": int(added_up),
            "overcount_pct": round(100 * (added_up - true_week) / true_week, 1)}


def add_views(con, cfg: Config, names: list[str], layout: str = "by_day") -> None:
    for name in names:
        if name == "events":
            files = (cfg.root / LAYOUTS[layout]).as_posix()
            reader = (f"read_csv('{files}')" if layout == "csv"
                      else f"read_parquet('{files}', hive_partitioning = true)")
        else:
            file = cfg.root / ERROR_CODES_FILE if name == "error_codes" else rollup_file(cfg, name)
            if not file.exists():
                continue   # not built yet (build makes these), the view can wait
            reader = f"read_parquet('{file.as_posix()}')"
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM {reader}")


def read_row_groups(con, files: Path) -> dict:
    """Every parquet file stores min, max and size for each column in each chunk (row group).
    I read all of that once per layout: {(file, row_group): {column: (min, max, bytes, all_null)}}"""
    groups = defaultdict(dict)
    for file, group, column, low, high, size, all_null in con.execute(f"""
            SELECT file_name, row_group_id, path_in_schema, stats_min_value, stats_max_value,
                   total_compressed_size, stats_null_count = num_values
            FROM parquet_metadata('{files.as_posix()}')""").fetchall():
        groups[(file, group)][column] = (low, high, size, all_null)
    return groups


def estimate_scan(row_groups: dict, query: Query) -> tuple[int, int]:
    """Roughly how many bytes this query has to read, and from how many files.

    A chunk is skipped when its min/max (or the day in its folder name) cannot match the
    filter. From the chunks that are left, only the needed columns are counted. A pay per
    byte scanned engine charges in the same way, so this is the number that turns into cost.
    It is an estimate from metadata, not a measurement of real disk reads."""
    total, files = 0, set()
    for (file, _), columns in row_groups.items():
        folder_day = re.search(r"day=(\d{4}-\d{2}-\d{2})", file)
        skip = False
        for column, (want_low, want_high) in query.filters.items():
            if column in columns:
                low, high, _, all_null = columns[column]
            elif column == "day" and folder_day:
                low = high = folder_day.group(1)
                all_null = False
            else:
                continue
            # a chunk where this column is all null (ok rows have no error code) can never match either
            if all_null or _cannot_match(want_low, want_high, low, high):
                skip = True
                break
        if not skip:
            files.add(file)
            total += sum(col[2] for name, col in columns.items() if name in query.columns)
    return total, len(files)


def _cannot_match(want_low, want_high, low, high) -> bool:
    if low is None or high is None:
        return False
    try:
        want_low, want_high, low, high = float(want_low), float(want_high), float(low), float(high)
    except (TypeError, ValueError):
        want_low, want_high, low, high = str(want_low), str(want_high), str(low), str(high)
    return want_high < low or want_low > high


def print_results(path: Path) -> None:
    data = json.loads(path.read_text())
    for name, question in data["questions"].items():
        rows = [[r["layout"], r["median_ms"], r["slowest_ms"], r["mb_scanned"], r["files_touched"]]
                for r in data["results"] if r["query"] == name]
        print(f"\n{question}")
        print_table(["layout", "median ms", "slowest ms", "MB read", "files"], rows)

    d = data["distinct_counts"]
    print("\nunique devices, exact vs approximate")
    print_table(["window", "exact", "ms", "approx", "ms", "off by %"],
                [[w["window"], w["exact"], w["exact_median_ms"], w["approx"], w["approx_median_ms"],
                  w["error_pct"]] for w in d["exact_vs_approx"]])
    print(f"\ndevices with a failure this week: {d['devices_with_a_failure_this_week']:,} counted "
          f"properly, {d['same_thing_by_adding_daily_counts']:,} by adding up 7 daily counts "
          f"({d['overcount_pct']}% too high, same device counted on many days)")
