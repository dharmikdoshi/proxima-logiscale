from dataclasses import replace

import pytest

from logscale import agent_tool
from logscale.agent_tool import MAX_ROWS, AgentTool, Rejected, check
from logscale.config import Config
from logscale.generate import generate
from logscale.layouts import build

WEEK = "day BETWEEN DATE '2026-06-01' AND DATE '2026-06-07'"


@pytest.fixture(scope="module")
def tool(tmp_path_factory):
    cfg = replace(Config(), rows=80_000, days=8, spike_day=7, devices=5_000,
                  data_root=tmp_path_factory.mktemp("data"))
    generate(cfg)
    build(cfg)
    return AgentTool(cfg)


@pytest.mark.parametrize("sql, reason", [
    # anything that is not a plain read
    ("DELETE FROM events", "only SELECT"),
    ("UPDATE events SET status = 'ok'", "only SELECT"),
    ("DROP TABLE events", "only SELECT"),
    ("COPY events TO 'out.csv'", "only SELECT"),
    ("ATTACH 'other.db'", "only SELECT"),
    ("PRAGMA database_list", "only SELECT"),
    ("SELECT 1; DROP TABLE events", "exactly one statement"),
    ("", "exactly one statement"),
    ("SELEC count(*) FRM events", "could not read"),
    # reaching outside the approved tables
    ("SELECT * FROM read_csv('C:/secrets.csv')", "files or table functions"),
    ("SELECT * FROM 'C:/data/other.parquet'", "is not allowed"),
    ("SELECT * FROM information_schema.tables", "is not allowed"),
    ("SELECT * FROM main.events", "is not allowed"),
    ("SELECT getenv('USERNAME')", "function 'getenv'"),
    # a forbidden table hidden one level down
    (f"SELECT n FROM (SELECT count(*) AS n FROM secrets WHERE {WEEK}) t", "is not allowed"),
    (f"WITH x AS (SELECT * FROM users) SELECT count(*) FROM events WHERE {WEEK}", "is not allowed"),
    (f"SELECT count(*) FROM events WHERE {WEEK} UNION SELECT count(*) FROM passwords", "is not allowed"),
    # careless use of the big table
    ("SELECT * FROM events WHERE day = DATE '2026-06-01'", "SELECT \\*"),
    ("SELECT e.* FROM events e WHERE e.day = DATE '2026-06-01'", "SELECT \\*"),
    ("SELECT count(*) FROM events", "needs a day filter"),
    ("SELECT count(*) FROM events WHERE status = 'fail'", "needs a day filter"),
    ("SELECT count(*) FROM events WHERE day >= DATE '2026-06-01'", "needs a day filter"),
    ("SELECT count(*) FROM events WHERE day = DATE '2026-06-01' OR status = 'fail'",
     "needs a day filter"),
    ("SELECT count(*) FROM events -- WHERE day = DATE '2026-06-01'", "needs a day filter"),
    ("SELECT count(*) FROM events WHERE day >= current_date - 7 AND day <= current_date",
     "plain dates"),
    ("SELECT count(*) FROM events WHERE day BETWEEN DATE '2026-06-01' AND DATE '2026-08-29'",
     "wider than 31 days"),
    (f"SELECT count(*) FROM rollup_daily_health r JOIN events e ON e.day = r.day WHERE r.{WEEK}",
     "needs a day filter"),
])
def test_bad_sql_is_rejected_with_a_useful_reason(sql, reason):
    with pytest.raises(Rejected, match=reason):
        check(sql)


@pytest.mark.parametrize("sql", [
    f"SELECT count(*) FROM events WHERE {WEEK} AND status = 'fail'",
    "SELECT count(*) FROM events WHERE day > DATE '2026-06-01' AND day < DATE '2026-06-05'",
    "SELECT day, sum(events) FROM rollup_daily_health GROUP BY 1",       # summaries need no dates
    "SELECT * FROM error_codes",                                         # small table, star is fine
    f"WITH f AS (SELECT device_id FROM events WHERE {WEEK}) SELECT count(*) FROM f",
    f"SELECT payload->>'conn', approx_count_distinct(device_id) FROM events WHERE {WEEK} GROUP BY 1",
])
def test_reasonable_sql_passes(sql):
    check(sql)


def test_limit_is_added_or_pulled_down():
    assert check("SELECT code FROM error_codes")[0].sql().endswith(f"LIMIT {MAX_ROWS}")
    assert check("SELECT code FROM error_codes LIMIT 999999")[0].sql().endswith(f"LIMIT {MAX_ROWS}")
    tree, note = check("SELECT code FROM error_codes LIMIT 5")
    assert tree.sql().endswith("LIMIT 5") and note == ""


def test_summary_and_raw_events_agree(tool):
    raw = tool.run(f"SELECT count(*) FROM events WHERE {WEEK} AND status = 'fail'")
    summary = tool.run(f"SELECT sum(events) FROM rollup_daily_health WHERE {WEEK} AND status = 'fail'")
    assert raw.status == summary.status == "ok"
    assert raw.rows[0][0] == summary.rows[0][0] > 0


def test_same_question_twice_is_a_cache_hit_even_if_typed_differently(tool):
    first = tool.run("SELECT meaning FROM error_codes WHERE code = 1042")
    second = tool.run("select   meaning\nfrom error_codes where code = 1042")
    assert (first.status, second.status) == ("ok", "cache hit")
    assert second.rows == first.rows == [("update download failed",)]


def test_rejections_come_back_as_answers_not_crashes(tool):
    answer = tool.run("DELETE FROM events")
    assert answer.status == "rejected" and "only SELECT" in answer.note and answer.rows == []


def test_engine_errors_are_also_turned_into_a_rejection(tool):
    answer = tool.run("SELECT no_such_column FROM error_codes")
    assert answer.status == "rejected" and "could not run" in answer.note


def test_over_budget_query_is_refused_before_it_runs(tool, monkeypatch):
    monkeypatch.setattr(agent_tool, "SCAN_BUDGET_MB", 0.001)
    answer = tool.run(f"SELECT device_id, payload FROM events WHERE {WEEK}")
    assert answer.status == "rejected" and "budget" in answer.note


def test_slow_query_is_stopped(tool, monkeypatch):
    monkeypatch.setattr(agent_tool, "TIMEOUT_SECONDS", 0.01)
    answer = tool.run("SELECT count(*) FROM rollup_daily_health a, rollup_daily_health b, "
                      "rollup_daily_health c")
    assert answer.status == "rejected" and "stopped" in answer.note
