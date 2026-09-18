import threading
import time
from dataclasses import dataclass, field
from datetime import date

import duckdb
import sqlglot
from sqlglot import exp

from .bench import add_views, estimate_scan, read_row_groups
from .config import Config
from .db import connect
from .layouts import LAYOUTS, ROLLUPS
from .queries import Query
from .report import print_table

BIG_TABLE = "events"
ALLOWED_TABLES = {BIG_TABLE, "error_codes", *ROLLUPS}
MAX_DAYS = 31          # widest day window allowed on the big table
MAX_ROWS = 1000        # every row that goes back costs the llm tokens
SCAN_BUDGET_MB = 10    # sized for the 10M test data. on athena this would be a workgroup limit
TIMEOUT_SECONDS = 10
CACHE_SIZE = 256       # answers kept in memory. redis would do this job in a real system
USE_ROLLUP = "for totals and rates use rollup_daily_health or rollup_daily_errors"


class Rejected(Exception):
    """When a query is refused. The message goes back to the model so it can fix the query and retry."""


@dataclass
class Answer:
    status: str                       # ok, rejected, cache hit
    note: str = ""
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    ms: float = 0.0
    mb_scanned: float = 0.0


class AgentTool:
    """The only door the agent has to the data: run(sql). Every query is checked before it runs."""

    def __init__(self, cfg: Config, layout: str = "sorted"):
        self.con = connect(cfg.root / "tmp")
        add_views(self.con, cfg, [BIG_TABLE, "error_codes", *ROLLUPS], layout)
        self.row_groups = read_row_groups(self.con, cfg.root / LAYOUTS[layout])
        self.cache = {}

    def run(self, sql: str) -> Answer:
        try:
            tree, note = check(sql)
            final_sql = tree.sql(dialect="duckdb")   # cleaned up form, also used as cache key
            if final_sql in self.cache:
                return Answer("cache hit", note, *self.cache[final_sql])
            mb = self._scan_mb(tree)
            if mb > SCAN_BUDGET_MB:
                raise Rejected(f"would read about {mb:.0f} MB, budget is {SCAN_BUDGET_MB} MB. "
                               f"ask for fewer days or fewer columns, {USE_ROLLUP}")
            columns, rows, ms = self._execute(final_sql)
        except Rejected as why:
            return Answer("rejected", str(why))
        if len(self.cache) >= CACHE_SIZE:
            self.cache.pop(next(iter(self.cache)))   # drop the oldest answer, dicts keep insert order
        self.cache[final_sql] = (columns, rows)
        return Answer("ok", note, columns, rows, ms, mb)

    def _scan_mb(self, tree) -> float:
        if not any(t.name == BIG_TABLE for t in tree.find_all(exp.Table)):
            return 0.0   # summary tables are under 1 MB, nothing to budget
        filters = {}
        for select in _selects_on_big_table(tree):
            filters["day"] = _day_window(select)
            for cond in _conditions(select):
                if (isinstance(cond, exp.EQ) and isinstance(cond.left, exp.Column)
                        and isinstance(cond.right, exp.Literal)):
                    filters[cond.left.name] = (cond.right.this, cond.right.this)
        columns = tuple({c.name for c in tree.find_all(exp.Column)})
        return estimate_scan(self.row_groups, Query("", "", "", columns, filters))[0] / 1e6

    def _execute(self, sql: str):
        timer = threading.Timer(TIMEOUT_SECONDS, self.con.interrupt)
        timer.start()
        begin = time.perf_counter()
        try:
            result = self.con.execute(sql)
            rows = result.fetchall()
        except duckdb.InterruptException:
            raise Rejected(f"took longer than {TIMEOUT_SECONDS}s and was stopped. {USE_ROLLUP}") from None
        except duckdb.Error as err:
            raise Rejected(f"the engine could not run it: {err}") from None
        finally:
            timer.cancel()
        return [d[0] for d in result.description], rows, (time.perf_counter() - begin) * 1000


def demo(cfg: Config) -> list[dict]:
    """Throw the kind of sql a model would write at the tool, good ones and bad ones, and see what happens."""
    bad_day = cfg.day_date(cfg.spike_day)
    week, month = cfg.window_sql(7), cfg.window_sql(MAX_DAYS)
    attempts = [
        ("tries to delete", "DELETE FROM events WHERE status = 'fail'"),
        ("two statements in one", "SELECT 1; DROP TABLE events"),
        ("reads a file from disk", "SELECT * FROM read_csv('C:/Users/someone/secrets.csv')"),
        ("lazy select star, no dates", "SELECT * FROM events"),
        ("day filter with no end", f"SELECT count(*) FROM events WHERE day >= DATE '{bad_day}'"),
        ("all 90 days of raw events", f"""SELECT device_model, count(*) FROM events
            WHERE day BETWEEN DATE '{cfg.start}' AND DATE '{cfg.day_date(cfg.days - 1)}' GROUP BY 1"""),
        ("a month of the fat json column", f"SELECT device_id, payload FROM events WHERE {month}"),
        ("weekly failures, from summary", f"""SELECT day, sum(events) AS failures
            FROM rollup_daily_health WHERE {week} AND status = 'fail' GROUP BY 1 ORDER BY 1"""),
        ("weekly failures, from raw events", f"""SELECT day, count(*) AS failures
            FROM events WHERE {week} AND status = 'fail' GROUP BY 1 ORDER BY 1"""),
        ("what broke on the bad day", f"""SELECT e.device_model, e.os_version, c.meaning, count(*) AS n
            FROM events e JOIN error_codes c ON c.code = e.error_code
            WHERE e.day = DATE '{bad_day}' AND e.status = 'fail'
            GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 3"""),
        ("one device, forgot the limit", f"""SELECT event_ts, event_type, status FROM events
            WHERE device_id = 10000000 AND {week} ORDER BY event_ts DESC"""),
        ("weekly failures from summary, again", f"""select day, sum(events) as failures
            from rollup_daily_health where {week} and status = 'fail' group by 1 order by 1"""),
    ]
    tool = AgentTool(cfg)
    outcome = []
    for label, sql in attempts:
        answer = tool.run(sql)
        found = [list(row) for row in answer.rows] if label == "what broke on the bad day" else []
        outcome.append({"tried": label, "verdict": answer.status, "ms": round(answer.ms, 1),
                        "mb": round(answer.mb_scanned, 1), "note": answer.note, "found": found})
    print()
    print_table(["#", "what the llm tried", "verdict", "ms", "MB", "note"],
                [[i, *list(r.values())[:5]] for i, r in enumerate(outcome, 1)])
    print("\nwhat broke on the bad day:", *next(r["found"] for r in outcome if r["found"]), sep="\n  ")
    return outcome


def check(sql: str):
    """All the rules in one place. Gives back the query (maybe with a LIMIT added) and a note,
    or raises Rejected. I parse the sql properly instead of searching the text for words like
    DELETE, because that is too easy to sneak past."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None]
    except sqlglot.errors.SqlglotError as err:
        raise Rejected(f"could not read the sql: {str(err).splitlines()[0]}") from None
    if len(statements) != 1:
        raise Rejected("send exactly one statement")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise Rejected(f"only SELECT is allowed, this is a {tree.key.upper()}")

    cte_names = {cte.alias for cte in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise Rejected("reading files or table functions directly is not allowed")
        if table.db or table.catalog or table.name not in ALLOWED_TABLES | cte_names:
            raise Rejected(f"table '{table.sql()}' is not allowed, "
                           f"use one of: {', '.join(sorted(ALLOWED_TABLES))}")
    # any function the parser does not recognise (getenv, read_text...) is refused, no exceptions
    unknown = tree.find(exp.Anonymous)
    if unknown:
        raise Rejected(f"function '{unknown.name}' is not allowed")

    for select in _selects_on_big_table(tree):
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and e.is_star)
               for e in select.expressions):
            raise Rejected(f"SELECT * on {BIG_TABLE} is not allowed, name the columns you need")
        first, last = _day_window(select)
        if (last - first).days + 1 > MAX_DAYS:
            raise Rejected(f"day window on {BIG_TABLE} is wider than {MAX_DAYS} days. {USE_ROLLUP}")

    limit = tree.args.get("limit")
    if limit is None or not limit.expression.is_int or int(limit.expression.this) > MAX_ROWS:
        return tree.limit(MAX_ROWS), f"LIMIT {MAX_ROWS} added"
    return tree, ""


def _sources(select) -> list:
    # newer sqlglot calls the FROM part "from_", older ones call it "from", so I check both
    found = [select.args.get("from_") or select.args.get("from"), *(select.args.get("joins") or [])]
    return [s.this for s in found if s and isinstance(s.this, exp.Table)]


def _selects_on_big_table(tree):
    for select in tree.find_all(exp.Select):
        if any(table.name == BIG_TABLE for table in _sources(select)):
            yield select


def _conditions(select) -> list:
    # only conditions joined with AND count. a day filter hidden inside an OR does not limit anything
    where = select.args.get("where")
    return list(where.this.flatten()) if where and isinstance(where.this, exp.And) \
        else [where.this] if where else []


def _day_window(select) -> tuple[date, date]:
    # in a join, a day filter on the small table (r.day) must not count as a filter on the big one
    big_names = {"", *(t.alias_or_name for t in _sources(select) if t.name == BIG_TABLE)}
    first = last = None
    for cond in _conditions(select):
        column = cond.this if isinstance(cond, exp.Between) else getattr(cond, "left", None)
        if not (isinstance(column, exp.Column) and column.name == "day"
                and column.table in big_names):
            continue
        if isinstance(cond, exp.Between):
            first, last = _as_date(cond.args["low"]), _as_date(cond.args["high"])
        elif isinstance(cond, exp.EQ):
            first = last = _as_date(cond.right)
        elif isinstance(cond, (exp.GT, exp.GTE)):
            first = _as_date(cond.right)
        elif isinstance(cond, (exp.LT, exp.LTE)):
            last = _as_date(cond.right)
    if first is None or last is None:
        raise Rejected(f"query on {BIG_TABLE} needs a day filter with a start and an end, "
                       f"like day BETWEEN DATE '2026-06-01' AND DATE '2026-06-07'. {USE_ROLLUP}")
    return first, last


def _as_date(node) -> date:
    literal = node if isinstance(node, exp.Literal) else node.find(exp.Literal)
    try:
        return date.fromisoformat(literal.this)
    except (AttributeError, ValueError):
        raise Rejected("day filter has to use plain dates, like DATE '2026-06-11'") from None
