import logging
import shutil
import time

from .config import CONNECTIONS, COUNTRIES, EVENT_TYPES, FIRMWARES, MODELS, OS_VERSIONS, Config
from .db import connect
from .files import replace_with_retry
from .report import print_table

log = logging.getLogger(__name__)

# u(x, k) is my dice: a number between 0 and 1 that is always the same for the same x and k.
# I do not use random() because its output changes with the thread count. with a hash,
# the same seed gives the same data every time, and any single day can be rebuilt on its own
U_MACRO = "CREATE OR REPLACE MACRO u(x, k) AS (hash(x, k, {seed}) % 1000000) / 1000000.0"

DAY_SQL = """
COPY (
    WITH ids AS (
        SELECT {first_id} + i AS g FROM range({n}) t(i)
    ),
    dev AS (
        SELECT g,
            -- average of two dice rolls piles up around midday, so traffic has a daytime peak
            TIMESTAMP '{day} 00:00:00'
                + to_seconds(CAST(86400 * (u(g, 2) + u(g, 3)) / 2 AS BIGINT)) AS event_ts,
            -- squaring the roll pushes most events to the low device ids, so a few devices are very chatty
            10000000 + CAST(floor({devices} * pow(u(g, 1), 2)) AS BIGINT) AS device_id
        FROM ids
    ),
    attrs AS (
        -- model and os come from the device id, so a device keeps the same model in every event
        SELECT g, event_ts, device_id,
            {models}[1 + CAST(floor({n_models} * pow(u(device_id, 11), 2)) AS INT)] AS device_model,
            {oses}[1 + CAST(floor({n_oses} * pow(u(device_id, 12), 1.5)) AS INT)] AS os_version,
            {firmwares}[1 + CAST(floor({n_firmwares} * pow(u(device_id, 13), 1.5)) AS INT)]
                AS firmware_version,
            {countries}[1 + CAST(floor({n_countries} * pow(u(device_id, 14), 2)) AS INT)] AS country
        FROM dev
    ),
    flagged AS (
        SELECT *, {is_spike_day} AND device_model = '{spike_model}'
                  AND os_version = '{spike_os}' AS in_spike
        FROM attrs
    ),
    rated AS (
        -- every day gets its own base fail rate, and every model is half to double of that.
        -- still hash based, so the same seed gives the same good and bad days
        SELECT *, least({fail_rate_cap},
                        ({fail_rate_low} + ({fail_rate_cap} / 2 - {fail_rate_low}) * u({day_index}, 21))
                        * (0.5 + 1.5 * u(device_model, 22))) AS normal_rate
        FROM flagged
    ),
    outcome AS (
        SELECT *, u(g, 4) < CASE WHEN in_spike THEN {spike_fail_rate} ELSE normal_rate END AS failed
        FROM rated
    )
    SELECT
        g AS event_id,
        event_ts,
        -- when the event reached us. usually within a minute, but 5% of the time the device
        -- was offline and sends it up to 6 hours late
        event_ts + to_seconds(CAST(CASE WHEN u(g, 15) < 0.95 THEN 1 + 59 * u(g, 16)
                                        ELSE 21600 * u(g, 16) END AS BIGINT)) AS ingested_at,
        device_id,
        device_model,
        os_version,
        firmware_version,
        country,
        {event_types}[1 + CAST(floor({n_event_types} * pow(u(g, 5), 2)) AS INT)] AS event_type,
        CAST(CASE WHEN NOT failed THEN NULL
                  WHEN in_spike THEN {spike_code}
                  ELSE 1000 + floor({error_codes} * pow(u(g, 6), 3)) END AS SMALLINT) AS error_code,
        CASE WHEN failed THEN 'fail' ELSE 'ok' END AS status,
        -- most are quick, a few are slow, and failed ones take about 3x longer
        CAST(20 * exp(4.5 * pow(u(g, 7), 2)) * CASE WHEN failed THEN 3 ELSE 1 END AS INTEGER)
            AS duration_ms,
        -- real logs almost always carry a loose json blob like this. I keep it as text on purpose,
        -- later I measure what it costs to dig inside it compared to a proper column
        CAST(json_object(
            'conn', {connections}[1 + CAST(floor({n_connections} * pow(u(g, 8), 2)) AS INT)],
            'battery', 5 + CAST(floor(96 * u(g, 9)) AS INT),
            'retries', CAST(floor((CASE WHEN failed THEN 4 ELSE 2 END) * pow(u(g, 10), 3)) AS INT)
        ) AS VARCHAR) AS payload
    FROM outcome
) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)
"""


def events_glob(cfg: Config) -> str:
    return (cfg.root / "by_day" / "*" / "*.parquet").as_posix()


def generate(cfg: Config, days: range | None = None, force: bool = False) -> None:
    if cfg.rows_per_day < 1:
        raise ValueError(f"{cfg.rows} rows is too less for {cfg.days} days")
    _check_disk(cfg)

    con = connect(cfg.root / "tmp")
    con.execute(U_MACRO.format(seed=cfg.seed))

    started = time.perf_counter()
    written = 0
    for day in days or range(cfg.days):
        written += _write_day(con, cfg, day, force)
    log.info("generate done: %d day files written, %d already there, %.1fs",
             written, len(days or range(cfg.days)) - written, time.perf_counter() - started)


def _write_day(con, cfg: Config, day: int, force: bool) -> int:
    folder = cfg.root / "by_day" / f"day={cfg.day_date(day)}"
    final = folder / "part-0.parquet"
    if final.exists() and not force:
        return 0

    folder.mkdir(parents=True, exist_ok=True)
    # write under a temp name and rename at the end. if the run dies halfway,
    # the half file is never mistaken for a finished one on the next run
    tmp = folder / "part-0.parquet.tmp"
    con.execute(DAY_SQL.format(
        first_id=day * cfg.rows_per_day, n=cfg.rows_per_day, day=cfg.day_date(day),
        devices=cfg.devices, error_codes=cfg.error_codes,
        models=MODELS, n_models=len(MODELS),
        oses=OS_VERSIONS, n_oses=len(OS_VERSIONS),
        event_types=EVENT_TYPES, n_event_types=len(EVENT_TYPES),
        firmwares=FIRMWARES, n_firmwares=len(FIRMWARES),
        countries=COUNTRIES, n_countries=len(COUNTRIES),
        connections=CONNECTIONS, n_connections=len(CONNECTIONS),
        is_spike_day=str(day == cfg.spike_day).upper(),
        spike_model=cfg.spike_model, spike_os=cfg.spike_os,
        spike_fail_rate=cfg.spike_fail_rate, spike_code=cfg.spike_code,
        fail_rate_low=cfg.fail_rate_low, fail_rate_cap=cfg.fail_rate_cap, day_index=day,
        out=tmp.as_posix(),
    ))
    replace_with_retry(tmp, final)
    log.info("day %3d (%s) written, %d rows", day, cfg.day_date(day), cfg.rows_per_day)
    return 1


def _check_disk(cfg: Config) -> None:
    # about 25 bytes per row after compression, I ask for double that to be safe
    need = cfg.rows * 25 * 2
    cfg.root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(cfg.root).free
    if free < need:
        raise SystemExit(f"Not enough disk space: need ~{need / 1e9:.1f} GB, "
                         f"{free / 1e9:.1f} GB free. Nothing was written.")


def peek(cfg: Config, count: int = 10) -> None:
    """Print the first few rows of the first day, just to see what the data looks like."""
    con = connect(cfg.root / "tmp")
    first_day = (cfg.root / "by_day" / f"day={cfg.day_date(0)}" / "part-0.parquet").as_posix()
    con.sql(f"SELECT * FROM read_parquet('{first_day}') LIMIT {count}").show(max_width=250)


def summary(cfg: Config) -> None:
    """Quick look at the data, to check the skew and the bad day are really in there."""
    con = connect(cfg.root / "tmp")
    src = f"read_parquet('{events_glob(cfg)}', hive_partitioning = true)"

    fail_pct = "round(100.0 * count(*) FILTER (status = 'fail') / count(*), 2)"

    rows, days, devices, pct = con.execute(f"""
        SELECT count(*), count(DISTINCT day), count(DISTINCT device_id), {fail_pct}
        FROM {src}""").fetchone()
    size_mb = sum(f.stat().st_size for f in (cfg.root / "by_day").rglob("*.parquet")) / 1e6
    print(f"\nrows {rows:,} | days {days} | devices {devices:,} | "
          f"fail rate {pct}% | on disk {size_mb:,.0f} MB")

    low, high = con.execute(f"""SELECT min(pct), max(pct) FROM (
        SELECT {fail_pct} AS pct FROM {src} GROUP BY day)""").fetchone()
    print(f"daily fail rate moves between {low}% and {high}% (cap is {cfg.fail_rate_cap:.0%})")

    print("\ntop 5 error codes (should be clearly uneven)")
    print_table(["code", "failures"], con.execute(f"""
        SELECT CAST(error_code AS VARCHAR), count(*) FROM {src} WHERE status = 'fail'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 5""").fetchall())

    print("\ntop 3 models by events, with their fail rate")
    print_table(["model", "events", "fail %"], con.execute(f"""
        SELECT device_model, count(*), {fail_pct} FROM {src}
        GROUP BY 1 ORDER BY 2 DESC LIMIT 3""").fetchall())

    print(f"\nfail rate for {cfg.spike_model} + {cfg.spike_os}, the bad day vs other days")
    print_table(["which", "fail %"], con.execute(f"""
        SELECT CASE WHEN day = DATE '{cfg.day_date(cfg.spike_day)}' THEN 'bad day'
                    ELSE 'other days' END, {fail_pct}
        FROM {src} WHERE device_model = '{cfg.spike_model}' AND os_version = '{cfg.spike_os}'
        GROUP BY 1 ORDER BY 1 DESC""").fetchall())
