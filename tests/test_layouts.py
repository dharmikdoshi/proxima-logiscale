from dataclasses import replace

import duckdb
import pytest

from logscale.browse import make_browse_file
from logscale.config import Config
from logscale.generate import generate
from logscale.layouts import LAYOUTS, TINY_FILES_PER_DAY, build, sizes


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    cfg = replace(Config(), rows=100_000, days=4, spike_day=2, devices=5_000,
                  data_root=tmp_path_factory.mktemp("data"))
    generate(cfg)
    build(cfg)
    return cfg


def read(cfg, layout):
    files = (cfg.root / LAYOUTS[layout]).as_posix()
    return f"read_csv('{files}')" if layout == "csv" else f"read_parquet('{files}')"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_every_layout_holds_the_same_rows(cfg, layout):
    # only the arrangement should differ. if a layout loses or changes rows the benchmark is useless
    fingerprint = "SELECT count(*), sum(event_id), sum(duration_ms), count(error_code) FROM {}"
    expected = duckdb.sql(fingerprint.format(read(cfg, "by_day"))).fetchall()
    assert duckdb.sql(fingerprint.format(read(cfg, layout))).fetchall() == expected


def test_sorted_files_keep_failures_together(cfg):
    first_file = min(cfg.root.glob(LAYOUTS["sorted"])).as_posix()
    # once status flips from fail to ok it should never flip back inside a file
    flips = duckdb.sql(f"""
        SELECT count(*) FROM (
            SELECT status, lag(status) OVER (ORDER BY file_row_number) AS before
            FROM read_parquet('{first_file}', file_row_number = true))
        WHERE status <> before""").fetchone()[0]
    assert flips == 1


def test_tiny_layout_really_has_many_files(cfg):
    assert len(list(cfg.root.glob(LAYOUTS["tiny_files"]))) == TINY_FILES_PER_DAY * cfg.days


def test_parquet_is_smaller_than_csv(cfg):
    mb = {row["layout"]: row["mb"] for row in sizes(cfg)}
    assert mb["by_day"] < mb["csv"]


def test_rebuild_skips_finished_layouts(cfg):
    single = cfg.root / LAYOUTS["single"]
    before = single.stat().st_mtime_ns
    build(cfg)
    assert single.stat().st_mtime_ns == before


def test_browse_file_shows_layouts_as_tables(cfg):
    path, views = make_browse_file(cfg)
    assert set(views) >= {f"events_{name}" for name in LAYOUTS}
    con = duckdb.connect(str(path), read_only=True)
    assert con.sql("SELECT count(*) FROM events_sorted").fetchone()[0] == 100_000
    con.close()
