from dataclasses import replace
from datetime import date

import duckdb
import pytest

from logscale.config import Config
from logscale.dates import day_range, parse_date
from logscale.generate import generate
from logscale.ingest import RESEND_ONE_IN, ingest
from logscale.layouts import build


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    cfg = replace(Config(), rows=60_000, days=3, spike_day=2, devices=5_000,
                  data_root=tmp_path_factory.mktemp("data"))
    generate(cfg)
    return cfg


def test_ingest_drops_the_resent_events(cfg):
    report = ingest(cfg, range(1, 2))[0]
    assert report["date"] == "2026-06-02"
    assert report["duplicates_dropped"] == cfg.rows_per_day // RESEND_ONE_IN
    assert report["rows_out"] == cfg.rows_per_day


def test_ingest_from_csv_matches_build_from_parquet(cfg, tmp_path):
    # same day cleaned through two routes. if csv typing or dedupe was wrong these would differ
    ingest(cfg, range(1))
    via_csv = cfg.root / "sorted" / f"day={cfg.day_date(0)}" / "part-0.parquet"
    other = replace(cfg, data_root=tmp_path)
    generate(other, days=range(1))
    build(other)
    via_parquet = other.root / "sorted" / f"day={cfg.day_date(0)}" / "part-0.parquet"

    def look(path, sql):
        return duckdb.sql(sql.format(f"'{path.as_posix()}'")).fetchall()

    assert look(via_csv, "DESCRIBE SELECT * FROM {}") == look(via_parquet, "DESCRIBE SELECT * FROM {}")
    fingerprint = "SELECT count(*), sum(event_id), sum(duration_ms), count(error_code), count(conn) FROM {}"
    assert look(via_csv, fingerprint) == look(via_parquet, fingerprint)


def test_ingest_needs_generated_data_for_that_day(cfg, tmp_path):
    empty = replace(cfg, data_root=tmp_path)
    with pytest.raises(SystemExit, match="run generate"):
        ingest(empty, range(1))


def test_parse_date_accepts_only_iso_format():
    assert parse_date("2026-06-11") == date(2026, 6, 11)


@pytest.mark.parametrize("text", ["11-06-2026", "2026/06/11", "2026-6-11", "20260611",
                                  "2026-02-30", "2026-13-01", "yesterday", ""])
def test_parse_date_rejects_everything_else(text):
    with pytest.raises(ValueError):
        parse_date(text)


def test_day_range_variants(cfg):
    d = date.fromisoformat
    assert day_range(cfg, None, None, None) is None
    assert day_range(cfg, d("2026-06-02"), None, None) == range(1, 2)
    assert day_range(cfg, None, d("2026-06-01"), d("2026-06-03")) == range(3)
    assert day_range(cfg, None, d("2026-06-02"), None) == range(1, 3)   # open end = till last day
    assert day_range(cfg, None, None, d("2026-06-02")) == range(2)      # open start = from first day


@pytest.mark.parametrize("args, message", [
    ((date(2026, 5, 31), None, None), "outside this dataset"),
    ((date(2026, 6, 4), None, None), "outside this dataset"),
    ((None, date(2026, 6, 3), date(2026, 6, 1)), "is after"),
    ((date(2026, 6, 1), date(2026, 6, 1), None), "not both"),
])
def test_day_range_rejects_bad_input(cfg, args, message):
    with pytest.raises(ValueError, match=message):
        day_range(cfg, *args)
