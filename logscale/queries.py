from dataclasses import dataclass, field

from .config import Config

# device ids start here, and thanks to the skew in generate.py the very first one is the chattiest
BUSIEST_DEVICE = 10_000_000


@dataclass(frozen=True)
class Query:
    name: str
    question: str                  # the same thing in plain english, goes into the report
    sql: str                       # always reads from the views `events` and `error_codes`
    columns: tuple[str, ...]       # columns the engine has to read for this
    filters: dict = field(default_factory=dict)   # column -> (low, high), used to estimate skipping
    only_layouts: tuple[str, ...] = ()            # empty means every layout
    rollup_sql: str = ""           # same question answered from the small summary tables, if possible


def queries(cfg: Config) -> list[Query]:
    """The questions someone on call (or an agent) would actually ask, all built around the bad day."""
    (week_start, bad_day), (month_start, _) = cfg.window(7), cfg.window(30)
    week = cfg.window_sql(7)

    return [
        Query("failures_one_day", "How many events failed on one given day?",
              f"SELECT count(*) FROM events WHERE day = DATE '{bad_day}' AND status = 'fail'",
              columns=("day", "status"),
              filters={"day": (bad_day, bad_day), "status": ("fail", "fail")},
              rollup_sql=f"""SELECT sum(events) FROM rollup_daily_health
                             WHERE day = DATE '{bad_day}' AND status = 'fail'"""),

        Query("top_errors_week", "Top 10 error codes of the week, with their meaning (a join)",
              f"""SELECT e.error_code, c.meaning, count(*) AS failures
                  FROM events e JOIN error_codes c ON c.code = e.error_code
                  WHERE {week} AND e.status = 'fail'
                  GROUP BY 1, 2 ORDER BY failures DESC, e.error_code LIMIT 10""",
              columns=("day", "status", "error_code"),
              filters={"day": (week_start, bad_day), "status": ("fail", "fail")},
              rollup_sql=f"""SELECT r.error_code, c.meaning, sum(r.failures) AS failures
                             FROM rollup_daily_errors r JOIN error_codes c ON c.code = r.error_code
                             WHERE {week}
                             GROUP BY 1, 2 ORDER BY failures DESC, r.error_code LIMIT 10"""),

        Query("devices_hit_by_error", "How many different devices hit error 1042 this week?",
              f"""SELECT count(DISTINCT device_id) FROM events
                  WHERE {week} AND error_code = {cfg.spike_code}""",
              columns=("day", "error_code", "device_id"),
              filters={"day": (week_start, bad_day),
                       "error_code": (cfg.spike_code, cfg.spike_code)}),

        Query("fail_rate_by_model_os", "Which model + os combos fail the most, over all days?",
              """SELECT device_model, os_version, count(*) AS events,
                        round(100.0 * count(*) FILTER (WHERE status = 'fail') / count(*), 2) AS fail_pct
                 FROM events GROUP BY 1, 2 ORDER BY fail_pct DESC, 1, 2 LIMIT 10""",
              columns=("device_model", "os_version", "status"),
              rollup_sql="""SELECT device_model, os_version, sum(events) AS events,
                                   round(100.0 * sum(events) FILTER (WHERE status = 'fail')
                                         / sum(events), 2) AS fail_pct
                            FROM rollup_daily_health
                            GROUP BY 1, 2 ORDER BY fail_pct DESC, 1, 2 LIMIT 10"""),

        Query("one_device_history", "Last 100 events of one device in the past 30 days",
              f"""SELECT event_ts, event_type, status, error_code FROM events
                  WHERE device_id = {BUSIEST_DEVICE} AND {cfg.window_sql(30)}
                  ORDER BY event_ts DESC LIMIT 100""",
              columns=("day", "device_id", "event_ts", "event_type", "status", "error_code"),
              filters={"day": (month_start, bad_day),
                       "device_id": (BUSIEST_DEVICE, BUSIEST_DEVICE)}),

        Query("failures_by_conn_json", "Failures per connection type, digging inside the json blob",
              f"""SELECT payload->>'conn' AS conn, count(*) FROM events
                  WHERE {week} AND status = 'fail' GROUP BY 1""",
              columns=("day", "status", "payload"),
              filters={"day": (week_start, bad_day), "status": ("fail", "fail")}),

        Query("failures_by_conn_column", "Same question, but conn is a proper column now",
              f"SELECT conn, count(*) FROM events WHERE {week} AND status = 'fail' GROUP BY 1",
              columns=("day", "status", "conn"),
              filters={"day": (week_start, bad_day), "status": ("fail", "fail")},
              only_layouts=("sorted",)),
    ]
