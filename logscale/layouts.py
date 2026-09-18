import logging
from pathlib import Path

from .config import Config
from .db import connect
from .files import remove, replace_with_retry
from .report import print_table

log = logging.getLogger(__name__)

# same rows every time, only the way they sit on disk changes.
# name -> where the files are inside the size folder (data/10M/...)
LAYOUTS = {
    "csv": "csv/events.csv",
    "single": "single/events.parquet",
    "by_day": "by_day/*/*.parquet",
    "sorted": "sorted/*/*.parquet",
    "tiny_files": "tiny_files/*/*/*.parquet",
}

# most questions are about failures, and failures are only about 6% of rows.
# sorting like this keeps them together, so the reader can skip the ok chunks completely
SORT_KEY = "status, error_code, device_model, event_ts"

# sorted is my "cleaned" layer. besides the ordering, the json field people ask about most
# (conn) is pulled out into a normal column. the raw payload stays as it is
CLEAN_SQL = f"SELECT *, payload->>'conn' AS conn FROM {{src}} ORDER BY {SORT_KEY}"

# small ready-made summaries. careful with the group by columns: every extra column multiplies
# the row count, and with too many the "summary" ends up as big as the raw data
ROLLUPS = {
    "rollup_daily_health": """
        SELECT day, device_model, os_version, status,
               count(*) AS events, sum(duration_ms) AS total_duration_ms
        FROM {src} GROUP BY ALL ORDER BY day""",
    "rollup_daily_errors": """
        SELECT day, error_code, count(*) AS failures, count(DISTINCT device_id) AS devices
        FROM {src} WHERE status = 'fail' GROUP BY ALL ORDER BY day""",
}

ERROR_CODES_FILE = "lookup/error_codes.parquet"
ERROR_MEANINGS = ["connection timeout", "pairing rejected", "update download failed",
                  "sync conflict", "firmware checksum mismatch", "battery too low",
                  "signal lost", "auth token expired", "storage full", "unknown"]

# a streaming loader that writes every few minutes leaves a mess of small files like this
TINY_FILES_PER_DAY = 50

# csv and tiny files are only there to show what not to do, no point building them at big sizes
BASELINE_MAX_ROWS = 10_000_000

PARQUET = "(FORMAT PARQUET, COMPRESSION ZSTD)"


def build(cfg: Config, force: bool = False) -> None:
    day_folders = sorted((cfg.root / "by_day").glob("day=*"))
    if not day_folders:
        raise SystemExit(f"No data in {cfg.root / 'by_day'}, run generate first.")

    con = connect(cfg.root / "tmp")
    all_days = f"read_parquet('{(cfg.root / LAYOUTS['by_day']).as_posix()}', hive_partitioning = true)"
    with_baselines = cfg.rows <= BASELINE_MAX_ROWS

    # small lookup table, so there is a join to measure and error codes have a readable meaning
    lookup = f"""SELECT CAST(code AS SMALLINT) AS code,
                        {ERROR_MEANINGS}[1 + code % {len(ERROR_MEANINGS)}] AS meaning
                 FROM range(1000, {1000 + cfg.error_codes}) t(code)"""
    write_atomic(con, f"COPY ({lookup}) TO '{{out}}' (FORMAT PARQUET)",
                 cfg.root / ERROR_CODES_FILE, force)

    for name, sql in ROLLUPS.items():
        write_atomic(con, f"COPY ({sql.format(src=all_days)}) TO '{{out}}' (FORMAT PARQUET)",
                     rollup_file(cfg, name), force)

    write_atomic(con, f"COPY (SELECT * FROM {all_days}) TO '{{out}}' {PARQUET}",
                 cfg.root / "single" / "events.parquet", force)
    if with_baselines:
        write_atomic(con, f"COPY (SELECT * FROM {all_days}) TO '{{out}}' (FORMAT CSV, HEADER)",
                     cfg.root / "csv" / "events.csv", force)

    for folder in day_folders:
        # hive_partitioning off here, otherwise the day column ends up written inside the file too
        one_day = f"read_parquet('{(folder / 'part-0.parquet').as_posix()}', hive_partitioning = false)"
        write_atomic(con, f"COPY ({CLEAN_SQL.format(src=one_day)}) TO '{{out}}' {PARQUET}",
                     cfg.root / "sorted" / folder.name / "part-0.parquet", force)
        if with_baselines:
            tiny = f"SELECT *, event_id % {TINY_FILES_PER_DAY} AS chunk FROM {one_day}"
            write_atomic(con, f"""COPY ({tiny}) TO '{{out}}'
                                  (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (chunk))""",
                         cfg.root / "tiny_files" / folder.name, force)
    log.info("build done for %s", cfg.label)


def rollup_file(cfg: Config, name: str) -> Path:
    return cfg.root / "rollups" / f"{name}.parquet"


def write_atomic(con, sql: str, final: Path, force: bool) -> None:
    """Run a COPY into a temp name, then rename. Works for a single file and for a folder of files."""
    if final.exists() and not force:
        return
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(final.name + ".tmp")
    remove(tmp)
    # plain replace, not .format(), because the sql itself can contain { } (the csv column types)
    con.execute(sql.replace("{out}", tmp.as_posix()))
    remove(final)
    replace_with_retry(tmp, final)
    log.debug("wrote %s", final)


def sizes(cfg: Config) -> list[dict]:
    rows = cfg.rows_per_day * cfg.days
    table = []
    patterns = LAYOUTS | {name: f"rollups/{name}.parquet" for name in ROLLUPS}
    for name, pattern in patterns.items():
        files = list(cfg.root.glob(pattern))
        if files:
            total = sum(f.stat().st_size for f in files)
            table.append({"layout": name, "files": len(files), "mb": round(total / 1e6, 1),
                          "bytes_per_row": round(total / rows, 1)})
    return table


def print_sizes(cfg: Config) -> None:
    print()
    print_table(["layout", "files", "MB", "bytes/row"],
                [[r["layout"], r["files"], r["mb"], r["bytes_per_row"]] for r in sizes(cfg)])
