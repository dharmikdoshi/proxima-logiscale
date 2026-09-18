"""The "why not just postgres" comparison. Same cleaned rows, same questions, postgres instead of
parquet files. Needs the postgres from docker-compose.yml and `uv sync --group pg`."""

import logging
import os
import statistics
import time

from .config import Config
from .db import connect
from .files import remove
from .layouts import ERROR_CODES_FILE, ROLLUPS, rollup_file
from .queries import queries
from .report import save

log = logging.getLogger(__name__)

PG_URL = os.environ.get("LOGSCALE_PG", "postgresql://logscale:logscale@localhost:5433/logscale")
RUNS = 7

# same columns as the cleaned parquet, plus day. payload is jsonb so payload->>'conn' works here too.
# the table is split by day like the files are, and gets the indexes a dba would add for these questions
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id BIGINT, event_ts TIMESTAMP, ingested_at TIMESTAMP, device_id BIGINT,
    device_model TEXT, os_version TEXT, firmware_version TEXT, country TEXT, event_type TEXT,
    error_code SMALLINT, status TEXT, duration_ms INTEGER, payload JSONB, conn TEXT, day DATE NOT NULL
) PARTITION BY RANGE (day);
CREATE INDEX IF NOT EXISTS events_day_status ON events (day, status);
CREATE INDEX IF NOT EXISTS events_device_day ON events (device_id, day);
CREATE INDEX IF NOT EXISTS events_error_day ON events (error_code, day) WHERE error_code IS NOT NULL;
CREATE TABLE IF NOT EXISTS error_codes (code SMALLINT PRIMARY KEY, meaning TEXT);
CREATE TABLE IF NOT EXISTS rollup_daily_health (
    day DATE, device_model TEXT, os_version TEXT, status TEXT, events BIGINT, total_duration_ms BIGINT);
CREATE TABLE IF NOT EXISTS rollup_daily_errors (
    day DATE, error_code SMALLINT, failures BIGINT, devices BIGINT);
"""
EVENT_COLUMNS = ("event_id, event_ts, ingested_at, device_id, device_model, os_version, "
                 "firmware_version, country, event_type, error_code, status, duration_ms, payload, conn, day")


def pg_connect():
    try:
        import psycopg
    except ImportError:
        raise SystemExit("postgres driver missing, run: uv sync --group pg") from None
    try:
        return psycopg.connect(PG_URL, autocommit=True)
    except psycopg.OperationalError as err:
        raise SystemExit(f"cannot reach postgres at {PG_URL}\n"
                         f"start it with: docker compose up -d\n({err})") from None


def pg_load(cfg: Config, days: range) -> list[dict]:
    """Copy the cleaned parquet of each day into postgres, timed. Re-running a day replaces it."""
    pg = pg_connect()
    pg.execute(SCHEMA)
    duck = connect(cfg.root / "tmp")
    report = []
    for day in days:
        date = cfg.day_date(day)
        source = cfg.root / "sorted" / f"day={date}" / "part-0.parquet"
        if not source.exists():
            raise SystemExit(f"No cleaned file for {date}, run ingest (or build) for that date first.")
        part = f"events_{date:%Y%m%d}"
        pg.execute(f"CREATE TABLE IF NOT EXISTS {part} PARTITION OF events "
                   f"FOR VALUES FROM ('{date}') TO ('{cfg.day_date(day + 1)}')")
        pg.execute(f"TRUNCATE {part}")

        # postgres wants text, so the parquet goes through a csv file on the way in
        csv = cfg.root / "tmp" / f"{part}.csv"
        duck.execute(f"""COPY (SELECT *, DATE '{date}' AS day
                               FROM read_parquet('{source.as_posix()}', hive_partitioning = false))
                         TO '{csv.as_posix()}' (FORMAT CSV, HEADER)""")
        begin = time.perf_counter()
        with pg.cursor() as cur, cur.copy(
                f"COPY {part} ({EVENT_COLUMNS}) FROM STDIN WITH (FORMAT csv, HEADER true)") as copy, \
                open(csv, "rb") as f:
            while chunk := f.read(1 << 20):
                copy.write(chunk)
        pg.execute(f"ANALYZE {part}")
        seconds = time.perf_counter() - begin
        remove(csv)

        rows = pg.execute(f"SELECT count(*) FROM {part}").fetchone()[0]
        report.append({"date": str(date), "rows": rows, "seconds": round(seconds, 1),
                       "rows_per_second": round(rows / seconds)})
        log.info("%s loaded into postgres: %d rows in %.1fs", date, rows, seconds)

    _load_small_tables(cfg, pg, duck)
    pg.close()
    return report


def _load_small_tables(cfg: Config, pg, duck) -> None:
    """error_codes and the two summaries, whole, whenever they exist. They are tiny."""
    tables = {"error_codes": cfg.root / ERROR_CODES_FILE} | {n: rollup_file(cfg, n) for n in ROLLUPS}
    for name, file in tables.items():
        if not file.exists():
            continue
        pg.execute(f"TRUNCATE {name}")
        with pg.cursor() as cur, cur.copy(f"COPY {name} FROM STDIN") as copy:
            for row in duck.execute(f"SELECT * FROM '{file.as_posix()}'").fetchall():
                copy.write_row(row)
        pg.execute(f"ANALYZE {name}")


def pg_time(pg, sql: str, runs: int = RUNS) -> tuple[list, list[float]]:
    """Same method as bench: one warm-up, then timed runs. Returns the rows and the times in ms."""
    rows = pg.execute(sql).fetchall()
    times = []
    for _ in range(runs):
        begin = time.perf_counter()
        pg.execute(sql).fetchall()
        times.append((time.perf_counter() - begin) * 1000)
    return rows, times


def pg_bench(cfg: Config):
    """The seven questions against postgres, saved next to the duckdb results."""
    pg = pg_connect()
    results = []
    for query in queries(cfg):
        if query.only_layouts and "sorted" not in query.only_layouts:
            continue
        _, times = pg_time(pg, query.sql)
        results.append({"query": query.name, "median_ms": round(statistics.median(times), 1),
                        "slowest_ms": round(max(times), 1), "runs": len(times)})
        log.info("postgres    %-24s median %8.1f ms", query.name, results[-1]["median_ms"])
        if query.rollup_sql:
            _, times = pg_time(pg, query.rollup_sql)
            results.append({"query": query.name + " (summary)",
                            "median_ms": round(statistics.median(times), 1),
                            "slowest_ms": round(max(times), 1), "runs": len(times)})
    size_mb = pg.execute("""SELECT sum(pg_total_relation_size(inhrelid)) / 1e6
                            FROM pg_inherits WHERE inhparent = 'events'::regclass""").fetchone()[0]
    version = pg.execute("SHOW server_version").fetchone()[0]
    pg.close()
    return save("pg", cfg, {"scale": cfg.label, "postgres": version,
                            "events_table_mb": round(float(size_mb or 0), 1), "results": results})


def ask_both(cfg: Config, sql: str) -> None:
    """One question, both engines, side by side. The guardrails decide first, same as for the agent."""
    from .agent_tool import AgentTool, check

    tool = AgentTool(cfg)
    answer = tool.run(sql)
    print(f"\nduckdb on parquet : {answer.status}" + (f", {answer.note}" if answer.note else ""))
    if answer.status == "rejected":
        return
    print(f"                    {len(answer.rows)} rows, {answer.ms:.1f} ms, "
          f"about {answer.mb_scanned:.2f} MB read")

    pg_sql = check(sql)[0].sql(dialect="postgres")
    pg = pg_connect()
    rows, times = pg_time(pg, pg_sql, runs=3)
    pg.close()
    same = sorted(map(str, rows)) == sorted(map(str, answer.rows))
    print(f"postgres          : {len(rows)} rows, {statistics.median(times):.1f} ms"
          f"   {'same answer' if same else 'DIFFERENT ANSWER, look into it'}")
    if answer.rows:
        from .report import print_table
        print()
        print_table(answer.columns, answer.rows[:20])
