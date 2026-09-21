"""One batch file in, tables out.

    batch file -> reports_raw (as it came) -> id check -> reports_hot (columns, sorted)

reports_raw is kept per batch (batch=<name>/), the way it arrived, a report is looked up there by
batch and id. reports_hot goes into day folders, written one day at a time: duckdb's own
PARTITION_BY write ran out of memory on a file that touched 1,970 days.

Everything is written under _incoming/ first and moved into the tables only when the whole
batch is good, so a crash in the middle leaves the tables as they were."""
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from logscale.db import connect
from logscale.files import remove, replace_with_retry

from .measure import Steps, machine, peak_memory_mb

try:
    from . import fields as default_spec
except ImportError:   # the real list is not in git
    from . import fields_example as default_spec

GZIP_START = b"\x1f\x8b"
MAX_LINE_BYTES = 20_000_000
# a raw row is about 2.5 KB. the default of 122,880 rows per group, times 8 threads, does not fit in 3 GB
RAW_ROW_GROUP = 20_000
TABLES = ("reports_raw", "reports_hot")


class BadBatch(Exception):
    """The batch file cannot be loaded as it is. The message says what to fix."""


def batch_name(path: Path) -> str:
    name = path.name
    for suffix in (".gz", ".csv"):
        name = name.removesuffix(suffix)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise BadBatch(f"file name '{path.name}' should only have letters, numbers, dot, dash, underscore")
    return name


def source_sql(path: Path, spec) -> str:
    """read_csv call for this file. Zipped or not is decided from the first two bytes, not the name."""
    if not path.is_file():
        raise BadBatch(f"no such file: {path}")
    if path.stat().st_size == 0:
        raise BadBatch(f"{path.name} is empty")
    with path.open("rb") as f:
        zipped = f.read(2) == GZIP_START
    types = ", ".join(f"'{col}': '{kind}'" for col, kind in spec.FILE_COLUMNS.items())
    # column types are given, so a small sample is enough to work out the csv dialect
    return (f"read_csv('{path.as_posix()}', header = true, compression = '{'gzip' if zipped else 'none'}', "
            f"max_line_size = {MAX_LINE_BYTES}, sample_size = 2000, types = {{{types}}})")


def check_header(con, src: str, spec) -> None:
    try:
        found = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    except duckdb.Error as err:
        raise BadBatch(f"could not read the file as csv: {err}") from None
    missing = set(spec.FILE_COLUMNS) - found
    if missing:
        raise BadBatch(f"columns missing from the file: {', '.join(sorted(missing))}")


def json_shape(spec) -> str:
    """Nested description of only the parts of the json we use, for one json_transform call."""
    shape: dict = {}
    wanted = list(spec.JSON_FIELDS.values())
    wanted += [(path, "VARCHAR") for path in spec.DURATIONS.values()]
    # list items stay as they are, they are only counted here
    wanted += [(path, ["JSON"]) for path in spec.LIST_COUNTS.values()]
    for path, kind in wanted:
        node = shape
        for i, key in enumerate(path):
            if isinstance(key, int):
                continue
            if i == len(path) - 1:
                node[key] = kind
            elif isinstance(path[i + 1], int):   # a list of objects, described by one object
                node = node.setdefault(key, [{}])[0]
            else:
                node = node.setdefault(key, {})
    return json.dumps(shape)


def picked(path) -> str:
    """s."info"."Model", lists are counted from 1 in duckdb."""
    return "s" + "".join(f"[{key + 1}]" if isinstance(key, int) else f'."{key}"' for key in path)


def minutes_sql(text: str) -> str:
    def number_before(unit: str) -> str:
        found = f"regexp_extract({text}, '(\\d+)\\s*{unit}', 1)"
        return f"coalesce(try_cast(nullif({found}, '') AS BIGINT), 0)"

    parts = " + ".join(f"{number_before(unit)} * {mins}"
                       for unit, mins in (("w", 10080), ("d", 1440), ("h", 60), ("m", 1)))
    return f"CASE WHEN {text} IS NULL OR NOT regexp_matches({text}, '\\d') THEN NULL ELSE {parts} END"


def hot_columns(spec) -> str:
    cols = [f'"{src}" AS {name}' for name, src in spec.FILE_FIELDS.items()]
    cols.append("day")
    cols.append(f'("{spec.DEVICE_CLOCK}" > "{spec.SERVER_CLOCK}" + INTERVAL 1 DAY '
                f'OR "{spec.DEVICE_CLOCK}" < "{spec.SERVER_CLOCK}" - INTERVAL 30 DAY) AS clock_bad')
    cols += [f"{picked(path)} AS {name}" for name, (path, _) in spec.JSON_FIELDS.items()]
    cols += [f"{minutes_sql(picked(path))} AS {name}" for name, path in spec.DURATIONS.items()]
    cols += [f"coalesce(len({picked(path)}), 0) AS {name}" for name, path in spec.LIST_COUNTS.items()]
    return ",\n       ".join(cols)


def ingest(path: Path, root: Path, spec=default_spec, memory_limit: str = "3GB") -> dict:
    batch = batch_name(path)
    incoming = root / "_incoming" / batch
    remove(incoming)
    incoming.mkdir(parents=True)
    steps = Steps()
    started = datetime.now(UTC).isoformat(timespec="seconds")
    con = connect(root / "_tmp", memory_limit)
    con.execute("SET TimeZone = 'UTC'; SET preserve_insertion_order = false")
    raw_file = incoming / "reports_raw" / f"batch={batch}" / f"{batch}_part0.parquet"
    raw_file.parent.mkdir(parents=True)

    with steps.step("check the file and its header") as note:
        src = source_sql(path, spec)
        check_header(con, src, spec)
        note["mb_in"] = round(path.stat().st_size / 1e6)

    with steps.step("read the file once, write reports_raw as it came") as note:
        rows = con.execute(f"""
            COPY (SELECT *, CAST("{spec.SERVER_CLOCK}" AS DATE) AS day FROM {src})
            TO '{raw_file.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {RAW_ROW_GROUP})
            """).fetchone()[0]
        note["rows"] = rows
    raw = f"read_parquet('{raw_file.as_posix()}')"

    with steps.step("repeat check on the id column") as note:
        repeats, no_json = con.execute(f"""
            SELECT count(*) - count(DISTINCT "{spec.ID}"),
                   count(*) FILTER (WHERE "{spec.JSON_COLUMN}" IS NULL)
            FROM {raw}""").fetchone()
        note["repeats"], note["rows_without_json"] = repeats, no_json

    with steps.step("parse json once into a narrow temp table") as note:
        shape = json_shape(spec).replace("'", "''")
        newest_first = f'PARTITION BY "{spec.ID}" ORDER BY "{spec.SERVER_CLOCK}" DESC'
        latest = f"QUALIFY row_number() OVER ({newest_first}) = 1"
        con.execute(f"""
            CREATE TEMP TABLE hot AS
            SELECT {hot_columns(spec)}, '{batch}' AS batch
            FROM (SELECT *, json_transform("{spec.JSON_COLUMN}", '{shape}') AS s
                  FROM {raw} {latest if repeats else ""})""")
        hot_rows = con.execute("SELECT count(*) FROM hot").fetchone()[0]
        note["rows"] = hot_rows

    with steps.step("sort by device and time, write reports_hot, one file per day") as note:
        note["days"] = write_by_day(con, "hot", spec.SORT_BY, incoming / "reports_hot", batch)

    with steps.step("safety check: rows in = rows out"):
        if hot_rows != rows - repeats:
            raise BadBatch(f"{rows:,} rows read, {repeats:,} repeats, but {hot_rows:,} rows in reports_hot")

    with steps.step("move the batch into the tables") as note:
        note["files"] = publish(incoming, root / "tables", batch)
    con.close()
    remove(incoming)

    sizes = {table: round(sum(f.stat().st_size for f in batch_files(root / "tables" / table, batch)) / 1e6)
             for table in TABLES}
    run = {"batch": batch, "started_at": started, "rows": rows, "repeats": repeats,
           "rows_without_json": no_json, "seconds": steps.total(), "peak_memory_mb": peak_memory_mb(),
           "mb": sizes, "steps": steps.rows, "machine": machine()}
    with (root / "runs.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(run) + "\n")
    steps.print()
    print("written:", ", ".join(f"{t} {mb} MB" for t, mb in sizes.items()))
    return run


def write_by_day(con, table: str, sort_by, folder: Path, batch: str) -> int:
    """A normal batch touches one or two days, so this is one or two sorted writes."""
    days = [row[0] for row in con.execute(f"SELECT DISTINCT day FROM {table} ORDER BY day").fetchall()]
    for day in days:
        day_folder = folder / f"day={day}"
        day_folder.mkdir(parents=True, exist_ok=True)
        con.execute(f"""
            COPY (SELECT * EXCLUDE (day) FROM {table} WHERE day = DATE '{day}' ORDER BY {", ".join(sort_by)})
            TO '{(day_folder / f"{batch}_part0.parquet").as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    return len(days)


def batch_files(folder: Path, batch: str):
    return folder.rglob(f"{batch}_part*.parquet")


def publish(incoming: Path, tables: Path, batch: str) -> int:
    """Same batch loaded before? Its old files go first, then the new ones move in."""
    for old in list(batch_files(tables, batch)):
        old.unlink()
    moved = 0
    for new in incoming.rglob("*.parquet"):
        final = tables / new.relative_to(incoming)
        final.parent.mkdir(parents=True, exist_ok=True)
        replace_with_retry(new, final)
        moved += 1
    return moved
