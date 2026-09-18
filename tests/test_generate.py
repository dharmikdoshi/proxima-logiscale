from dataclasses import replace

import duckdb
import pytest

from logscale.config import Config, parse_rows
from logscale.generate import events_glob, generate


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    # small on purpose, the full thing finishes in a second or two
    cfg = replace(Config(), rows=200_000, days=5, spike_day=3, devices=5_000,
                  data_root=tmp_path_factory.mktemp("data"))
    generate(cfg)
    return cfg


def query(cfg, sql):
    src = f"read_parquet('{events_glob(cfg)}', hive_partitioning = true)"
    return duckdb.sql(sql.format(src=src)).fetchall()


def day_file(cfg, day=0):
    return cfg.root / f"by_day/day={cfg.day_date(day)}/part-0.parquet"


# each of these counts rows that should not exist, so the right answer is always 0
BAD_ROWS = {
    "event id used twice":
        "SELECT count(*) - count(DISTINCT event_id) FROM {src}",
    "device changed its model, os, firmware or country":
        """SELECT count(*) FROM (SELECT device_id FROM {src} GROUP BY 1 HAVING
           count(DISTINCT device_model || os_version || firmware_version || country) > 1)""",
    "failed without an error code, or ok with one":
        "SELECT count(*) FROM {src} WHERE (status = 'fail') <> (error_code IS NOT NULL)",
    "reached us before it happened, or more than 6 hours late":
        """SELECT count(*) FROM {src}
           WHERE ingested_at < event_ts OR ingested_at > event_ts + INTERVAL 6 HOUR""",
    "payload json is off":
        """SELECT count(*) FROM {src}
           WHERE payload->>'conn' NOT IN ('bluetooth', 'dongle', 'usb')
              OR CAST(payload->>'battery' AS INT) NOT BETWEEN 5 AND 100
              OR CAST(payload->>'retries' AS INT) NOT BETWEEN 0 AND 3""",
}


@pytest.mark.parametrize("problem", BAD_ROWS)
def test_data_has_no(cfg, problem):
    assert query(cfg, BAD_ROWS[problem]) == [(0,)]


def test_row_count_and_days(cfg):
    assert query(cfg, "SELECT count(*), count(DISTINCT day) FROM {src}") == [(200_000, 5)]


def test_spike_is_visible(cfg):
    rates = dict(query(cfg, f"""
        SELECT day = DATE '{cfg.day_date(cfg.spike_day)}',
               avg(CASE WHEN status = 'fail' THEN 1.0 ELSE 0 END)
        FROM {{src}}
        WHERE device_model = '{cfg.spike_model}' AND os_version = '{cfg.spike_os}'
        GROUP BY 1"""))
    assert rates[True] > 3 * rates[False]


def test_normal_fail_rate_varies_but_stays_under_cap(cfg):
    rates = [r for (r,) in query(cfg, f"""
        SELECT avg(CASE WHEN status = 'fail' THEN 1.0 ELSE 0 END)
        FROM {{src}}
        WHERE NOT (day = DATE '{cfg.day_date(cfg.spike_day)}'
                   AND device_model = '{cfg.spike_model}' AND os_version = '{cfg.spike_os}')
        GROUP BY day, device_model HAVING count(*) > 2000""")]
    assert max(rates) - min(rates) > 0.01          # not one flat number
    assert max(rates) < cfg.fail_rate_cap + 0.02   # small room for sampling noise


def test_split_run_gives_the_same_files_as_a_single_run(cfg, tmp_path):
    # two workers taking different day ranges should land on the same bytes as one worker.
    # this also covers "same seed gives same data"
    split = replace(cfg, data_root=tmp_path)
    generate(split, days=range(2))
    generate(split, days=range(2, 5))
    for day in range(cfg.days):
        assert day_file(cfg, day).read_bytes() == day_file(split, day).read_bytes()


def test_rerun_skips_finished_days_but_redoes_a_half_written_one(cfg, tmp_path):
    mine = replace(cfg, data_root=tmp_path)
    generate(mine, days=range(2))
    day_file(mine, 1).rename(day_file(mine, 1).with_name("part-0.parquet.tmp"))  # crash mid write
    untouched = day_file(mine, 0).stat().st_mtime_ns

    generate(mine, days=range(2))
    assert day_file(mine, 0).stat().st_mtime_ns == untouched
    assert day_file(mine, 1).exists()


@pytest.mark.parametrize("text, rows", [("10M", 10_000_000), ("1b", 1_000_000_000),
                                        ("250K", 250_000), ("1234", 1234), ("2.5M", 2_500_000)])
def test_parse_rows(text, rows):
    assert parse_rows(text) == rows


@pytest.mark.parametrize("text", ["", "abc", "-5M", "0"])
def test_parse_rows_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_rows(text)
