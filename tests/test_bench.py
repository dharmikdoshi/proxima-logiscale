import json
from dataclasses import replace

import duckdb
import pytest

from logscale import bench as bench_module
from logscale.config import Config
from logscale.generate import generate
from logscale.layouts import (
    ERROR_CODES_FILE,
    LAYOUTS,
    ROLLUPS,
    TINY_FILES_PER_DAY,
    build,
    rollup_file,
)
from logscale.queries import queries


@pytest.fixture(scope="module")
def cfg(tmp_path_factory):
    # 8 days so the "one week" questions still leave one day outside the window
    cfg = replace(Config(), rows=160_000, days=8, spike_day=7, devices=5_000,
                  data_root=tmp_path_factory.mktemp("data"))
    generate(cfg)
    build(cfg)
    return cfg


@pytest.fixture(scope="module")
def report(cfg, tmp_path_factory):
    bench_module.RUNS = bench_module.CSV_RUNS = 1   # we are checking the numbers, not the speed
    path = bench_module.bench(cfg, out_dir=tmp_path_factory.mktemp("results"))
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def results(report):
    return {(r["layout"], r["query"]): r for r in report["results"]}


def answer(cfg, layout, sql):
    files = (cfg.root / LAYOUTS[layout]).as_posix()
    con = duckdb.connect()
    reader = f"read_csv('{files}')" if layout == "csv" else f"read_parquet('{files}')"
    con.execute(f"CREATE VIEW events AS SELECT * FROM {reader}")
    con.execute(f"CREATE VIEW error_codes AS SELECT * FROM '{(cfg.root / ERROR_CODES_FILE).as_posix()}'")
    return con.execute(sql).fetchall()


@pytest.mark.parametrize("name", ["failures_one_day", "devices_hit_by_error", "top_errors_week"])
def test_every_layout_gives_the_same_answer(cfg, name):
    # a faster layout that gives a different answer is not faster, it is wrong
    sql = next(q.sql for q in queries(cfg) if q.name == name)
    expected = answer(cfg, "by_day", sql)
    assert expected and expected[0][0]
    for layout in LAYOUTS:
        assert answer(cfg, layout, sql) == expected, layout


def test_rollups_give_the_same_answer_as_raw_events(cfg):
    con = duckdb.connect()
    bench_module.add_views(con, cfg, ["events", "error_codes", *ROLLUPS])
    checked = 0
    for query in queries(cfg):
        if query.rollup_sql:
            from_raw = con.execute(query.sql).fetchall()
            from_rollup = con.execute(query.rollup_sql).fetchall()
            # numbers come back as different types (int, decimal), so compare them as text
            assert [[str(v) for v in row] for row in from_rollup] == \
                   [[str(v) for v in row] for row in from_raw], query.name
            checked += 1
    assert checked == 3


def test_rollups_are_tiny_next_to_the_raw_data(cfg):
    raw = sum(f.stat().st_size for f in cfg.root.glob(LAYOUTS["by_day"]))
    for name in ROLLUPS:
        assert rollup_file(cfg, name).stat().st_size < raw / 20


def test_approx_distinct_is_close_and_daily_counts_overcount(report):
    d = report["distinct_counts"]
    # the approximate count was seen up to ~15% off on real runs, so this only checks
    # that it lands in the right region, it is not a promise of accuracy
    assert all(w["error_pct"] < 25 for w in d["exact_vs_approx"])
    assert d["same_thing_by_adding_daily_counts"] > d["devices_with_a_failure_this_week"]


def test_report_fills_only_the_marked_part_of_the_readme(report, tmp_path, monkeypatch):
    from logscale import report as report_module
    monkeypatch.setattr(report_module, "RESULTS", tmp_path)
    monkeypatch.setattr(report_module, "MAIN", report["scale"])
    (tmp_path / f"bench_{report['scale']}.json").write_text(json.dumps(report))
    readme = tmp_path / "README.md"
    readme.write_text(f"my words\n{report_module.START}\nold numbers\n{report_module.END}\nmore words")

    report_module.write_report(readme)
    text = readme.read_text()
    assert text.startswith("my words") and text.endswith("more words")
    # the README gets the short scorecard in plain names, every table goes to RESULTS.md beside it
    assert "old numbers" not in text and "Thousands of tiny files" in text and "tiny_files" not in text
    assert "rollup_daily_health" in (tmp_path / "RESULTS.md").read_text()


def test_json_and_column_versions_agree(cfg):
    by_name = {q.name: q.sql for q in queries(cfg)}
    from_json = sorted(answer(cfg, "sorted", by_name["failures_by_conn_json"]))
    from_column = sorted(answer(cfg, "sorted", by_name["failures_by_conn_column"]))
    assert from_json == from_column


def test_error_1042_has_the_right_meaning(cfg):
    assert answer(cfg, "by_day", "SELECT meaning FROM error_codes WHERE code = 1042") == \
        [("update download failed",)]


def test_one_day_question_opens_one_day_only(results):
    assert results[("by_day", "failures_one_day")]["files_touched"] == 1
    assert results[("tiny_files", "failures_one_day")]["files_touched"] == TINY_FILES_PER_DAY


def test_week_question_opens_seven_days(results):
    assert results[("by_day", "top_errors_week")]["files_touched"] == 7


def test_csv_always_reads_everything(results, cfg):
    size_mb = (cfg.root / LAYOUTS["csv"]).stat().st_size / 1e6
    for (layout, _), row in results.items():
        if layout == "csv":
            assert row["mb_scanned"] == pytest.approx(size_mb, abs=0.01)


def test_parquet_reads_far_less_than_csv(results):
    csv = results[("csv", "failures_one_day")]["mb_scanned"]
    assert results[("by_day", "failures_one_day")]["mb_scanned"] < csv / 100


def test_column_question_only_runs_where_the_column_exists(results):
    layouts = {layout for layout, query in results if query == "failures_by_conn_column"}
    assert layouts == {"sorted"}
