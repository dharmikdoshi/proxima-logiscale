import argparse
import logging
from dataclasses import replace

from .agent_tool import AgentTool, demo
from .bench import RESULTS_DIR, bench, print_results
from .browse import make_browse_file
from .config import Config, parse_rows
from .dates import day_range, parse_date
from .generate import generate, peek, summary
from .ingest import ingest, print_ingest
from .layouts import LAYOUTS, build, print_sizes
from .pg import ask_both, pg_bench, pg_load
from .report import print_table, save, write_report
from .runlog import track

GROWTH_SIZES = ["25M", "50M", "100M"]


def main() -> None:
    parser = argparse.ArgumentParser(prog="logscale")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    # all commands need to know which data size I mean, so these flags are shared
    scale = argparse.ArgumentParser(add_help=False)
    scale.add_argument("--rows", default="10M", help="total rows, like 1M, 10M, 100M (default 10M)")
    scale.add_argument("--per-day", help="rows per day, like 5M, when you work day by day")
    scale.add_argument("--force", action="store_true", help="redo work that is already done")

    dates = argparse.ArgumentParser(add_help=False)
    dates.add_argument("--date", help="one day, YYYY-MM-DD")
    dates.add_argument("--from-date", help="first day of a range, YYYY-MM-DD")
    dates.add_argument("--to-date", help="last day of a range, YYYY-MM-DD")

    sub.add_parser("all", help="run the full experiment in order and write the report (~10 min)")
    sub.add_parser("growth", help="repeat the timings at 25M, 50M and 100M rows for the growth chart")
    sub.add_parser("generate", parents=[scale, dates],
                   help="play the devices: make synthetic events, one parquet file per day")
    sub.add_parser("ingest", parents=[scale, dates],
                   help="the daily job: a day's raw csv.gz comes in, cleaned parquet goes out")
    sub.add_parser("build", parents=[scale],
                   help="save the same rows in different layouts and compare sizes")
    bch = sub.add_parser("bench", parents=[scale],
                         help="time the same questions on every layout, save to results/")
    bch.add_argument("--layouts", nargs="+", choices=list(LAYOUTS), help="default is all of them")
    sub.add_parser("agent-demo", parents=[scale],
                   help="show how the agent query tool treats good and bad sql")
    ask = sub.add_parser("ask", parents=[scale], help="run your own sql through the agent's guardrails")
    ask.add_argument("sql", help="the question, in sql, in quotes")
    ask.add_argument("--both", action="store_true", help="also run it on postgres, timings side by side")
    sub.add_parser("pg-load", parents=[scale, dates],
                   help="copy the cleaned day(s) into postgres (docker compose up -d first)")
    sub.add_parser("pg-bench", parents=[scale], help="the seven questions against postgres")
    sub.add_parser("report", help="put the numbers from results/ into README.md")
    sub.add_parser("peek", parents=[scale], help="print the first 10 rows of the data")
    sub.add_parser("browse", parents=[scale],
                   help="make browse.duckdb so the data can be opened in TablePlus / DBeaver")

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        if args.command == "all":
            run_all()
        elif args.command == "growth":
            for size in GROWTH_SIZES:
                cfg = make_config(rows=size)
                run(cfg, "generate")
                run(cfg, "build")
                run(cfg, "bench", layouts=["single", "by_day", "sorted"])
        elif args.command == "report":
            write_report()
            print("README.md updated from results/")
        else:
            cfg = make_config(args.rows, args.per_day)
            typed = [getattr(args, name, None) for name in ("date", "from_date", "to_date")]
            picked = [parse_date(text) if text else None for text in typed]
            layouts = getattr(args, "layouts", None)
            run(cfg, args.command, days=day_range(cfg, *picked), force=args.force,
                layouts=layouts, partial=bool(layouts), sql=getattr(args, "sql", None),
                both=getattr(args, "both", False))
    except ValueError as err:
        parser.error(str(err))


def make_config(rows: str = "10M", per_day: str | None = None) -> Config:
    # the window is always 90 days. a per-day size names the folder after the daily number
    if per_day:
        return replace(Config(), rows=parse_rows(per_day) * 90, per_day=parse_rows(per_day))
    return replace(Config(), rows=parse_rows(rows))


def run_all() -> None:
    """The whole experiment in one go. Kept small so it finishes in about ten minutes."""
    main = make_config(rows="10M")
    for step in ("generate", "build", "bench", "agent-demo"):
        run(main, step)
    # real daily volume: 2 days of 5M rows, the bad day and the one before it
    daily = make_config(per_day="5M")
    two_days = range(daily.spike_day - 1, daily.spike_day + 1)
    run(daily, "generate", days=two_days)
    run(daily, "ingest", days=two_days)
    run(daily, "build")
    run(daily, "bench", layouts=["single", "by_day", "sorted"])
    write_report()
    print("\nall done. numbers are in results/, README.md has been updated")


def run(cfg: Config, command: str, days=None, force=False, layouts=None, partial=False,
        sql=None, both=False) -> None:
    if command == "generate":
        with track(cfg, command):
            generate(cfg, days=days, force=force)
        summary(cfg)
    elif command == "ingest":
        if days is None:
            raise ValueError("say which day(s) to ingest, with --date or --from-date / --to-date")
        with track(cfg, command):
            report = ingest(cfg, days, force=force)
        save("ingest", cfg, report)
        print_ingest(report)
    elif command == "build":
        with track(cfg, command):
            build(cfg, force=force)
        print_sizes(cfg)
    elif command == "bench":
        # if I picked only some layouts this is just a demo run, keep it away from the README numbers
        with track(cfg, command):
            out = bench(cfg, layouts=layouts, out_dir=RESULTS_DIR / "partial" if partial else RESULTS_DIR)
        print_results(out)
        print(f"\nsaved to {out}")
    elif command == "agent-demo":
        save("agent_demo", cfg, demo(cfg))
    elif command == "ask" and both:
        ask_both(cfg, sql)
    elif command == "ask":
        answer = AgentTool(cfg).run(sql)
        print(f"\n{answer.status}: {answer.note}" if answer.note else f"\n{answer.status}")
        if answer.rows:
            print(f"{len(answer.rows)} rows, {answer.ms:.1f} ms, about {answer.mb_scanned:.2f} MB read\n")
            print_table(answer.columns, answer.rows[:20])
    elif command == "pg-load":
        if days is None:
            raise ValueError("say which day(s) to load, with --date or --from-date / --to-date")
        with track(cfg, command):
            report = pg_load(cfg, days)
        print()
        print_table(["date", "rows", "seconds", "rows/sec"], [list(r.values()) for r in report])
    elif command == "pg-bench":
        with track(cfg, command):
            out = pg_bench(cfg)
        print(f"saved to {out}")
    elif command == "peek":
        peek(cfg)
    elif command == "browse":
        path, views = make_browse_file(cfg)
        print(f"open this file in TablePlus (connection type DuckDB):\n  {path.resolve()}")
        print("tables inside: " + ", ".join(views))


if __name__ == "__main__":
    main()
