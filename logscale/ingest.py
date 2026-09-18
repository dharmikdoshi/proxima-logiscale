import logging
import time

from .config import Config
from .db import connect
from .layouts import CLEAN_SQL, PARQUET, write_atomic
from .report import print_table

log = logging.getLogger(__name__)

# a csv has no types, everything is text. if I let the reader guess, error_code becomes a big int
# and the parquet file no longer matches the generated one. so the types are spelled out here
CSV_TYPES = {
    "event_id": "BIGINT", "event_ts": "TIMESTAMP", "ingested_at": "TIMESTAMP",
    "device_id": "BIGINT", "device_model": "VARCHAR", "os_version": "VARCHAR",
    "firmware_version": "VARCHAR", "country": "VARCHAR", "event_type": "VARCHAR",
    "error_code": "SMALLINT", "status": "VARCHAR", "duration_ms": "INTEGER",
    "payload": "VARCHAR",
}

# devices retry uploads, so some events arrive twice. 1 in 1000 here
RESEND_ONE_IN = 1000


def ingest(cfg: Config, days: range, force: bool = False) -> list[dict]:
    """The daily job: one day's raw csv.gz in, one cleaned parquet file out.
    Cleaning means: give columns their types, drop re-sent events, pull conn out of the json, sort."""
    con = connect(cfg.root / "tmp")
    report = []
    for day in days:
        date = cfg.day_date(day)
        generated = cfg.root / "by_day" / f"day={date}" / "part-0.parquet"
        if not generated.exists():
            raise SystemExit(f"No generated data for {date}, run generate for that date first.")

        # step 0, not timed, this only plays the devices: drop the day into the landing folder
        # the way it would really arrive, gzipped csv with a few repeated events in it
        landing = cfg.root / "landing" / f"day={date}" / "events.csv.gz"
        arrived = f"""SELECT * FROM read_parquet('{generated.as_posix()}', hive_partitioning = false)
                      UNION ALL
                      SELECT * FROM read_parquet('{generated.as_posix()}', hive_partitioning = false)
                      WHERE event_id % {RESEND_ONE_IN} = 0"""
        write_atomic(con, f"COPY ({arrived}) TO '{{out}}' (FORMAT CSV, HEADER, COMPRESSION GZIP)",
                     landing, force)

        # step 1, timed: the actual conversion.
        # hive_partitioning off, otherwise the reader takes "day" from the folder name and
        # sneaks it in as an extra column (a test caught exactly that)
        raw = (f"read_csv('{landing.as_posix()}', header = true, columns = {CSV_TYPES}, "
               f"hive_partitioning = false)")
        deduped = f"(SELECT DISTINCT ON (event_id) * FROM {raw})"
        cleaned = cfg.root / "sorted" / f"day={date}" / "part-0.parquet"
        begin = time.perf_counter()
        write_atomic(con, f"COPY ({CLEAN_SQL.format(src=deduped)}) TO '{{out}}' {PARQUET}",
                     cleaned, force=True)
        seconds = time.perf_counter() - begin

        rows_in = con.execute(f"SELECT count(*) FROM {raw}").fetchone()[0]
        rows_out = con.execute(f"SELECT count(*) FROM '{cleaned.as_posix()}'").fetchone()[0]
        if rows_out != cfg.rows_per_day:
            raise SystemExit(f"{date}: expected {cfg.rows_per_day} rows after cleaning, "
                             f"got {rows_out}. The cleaned file should not be trusted.")
        report.append({
            "date": str(date), "rows_in": rows_in, "duplicates_dropped": rows_in - rows_out,
            "rows_out": rows_out, "seconds": round(seconds, 1),
            "rows_per_second": round(rows_in / seconds),
            "csv_gz_mb": round(landing.stat().st_size / 1e6, 1),
            "parquet_mb": round(cleaned.stat().st_size / 1e6, 1),
        })
        log.info("%s ingested: %d rows in, %d repeats dropped, %.1fs",
                 date, rows_in, rows_in - rows_out, seconds)
    return report


def print_ingest(report: list[dict]) -> None:
    print()
    print_table(["date", "rows in", "repeats", "rows out", "seconds", "rows/sec", "csv.gz MB",
                 "parquet MB"], [list(r.values()) for r in report])
