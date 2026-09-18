import json
from pathlib import Path

from .config import Config

RESULTS = Path("results")
START, END = "<!-- results:start -->", "<!-- results:end -->"
MAIN, DAILY = "10M", "5M-per-day"   # 10M = every layout compared, 5M-per-day = real daily volume
ATHENA_USD_PER_TB, ATHENA_MIN_MB = 5, 10

# the code uses short names, someone reading the README should not have to learn them
NAMES = {"csv": "Raw CSV, as it arrives", "single": "One big file", "by_day": "One file per day",
         "sorted": "**One file per day, sorted**", "tiny_files": "Thousands of tiny files",
         "rollup": "Summary tables"}
VERDICTS = {"csv": "Keep only as the untouched original",
            "single": "Fast, but fixing one day means rewriting everything",
            "by_day": "Good", "sorted": "**Chosen**",
            "tiny_files": "What to avoid: same data, far slower",
            "rollup": "Used for the known questions"}
ONE_DAY, ALL_DAYS = "failures_one_day", "fail_rate_by_model_os"


def results_dir(cfg: Config) -> Path:
    # only the sizes the README is built from land in results/. anything else is a try-out run
    # and goes to results/partial/, which git ignores
    official = cfg.label in (MAIN, DAILY) or not cfg.per_day
    return RESULTS if official else RESULTS / "partial"


def save(name: str, cfg: Config, data) -> Path:
    folder = results_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}_{cfg.label}.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def print_table(headers: list, rows: list) -> None:
    """Plain table for the terminal. Numbers on the right, text on the left."""
    cells = [[f"{v:,}" if isinstance(v, (int, float)) else str(v) for v in row] for row in rows]
    widths = [max(len(h), *(len(row[i]) for row in cells)) for i, h in enumerate(headers)]
    is_number = [all(isinstance(row[i], (int, float)) for row in rows) for i in range(len(headers))]
    for line in (headers, *cells):
        print(("  " + "  ".join(c.rjust(w) if n else c.ljust(w)
                                for c, w, n in zip(line, widths, is_number, strict=True))).rstrip())


def write_report(readme: Path = Path("README.md")) -> None:
    """Every number in the README comes from the json files in results/, I never type one by hand.
    Only the part between the two markers is replaced, my own text around it stays."""
    main, daily = _load("bench", MAIN), _load("bench", DAILY)
    growth = sorted((json.loads(p.read_text()) for p in RESULTS.glob("bench_*.json")),
                    key=lambda b: b["rows"])
    # the growth table is the 90 day sets from the main size upwards. a quick 1M test run must not sneak in
    growth = [b for b in growth if "per-day" not in b["scale"] and main and b["rows"] >= main["rows"]]
    ingest, agent = _load("ingest", DAILY), _load("agent_demo", MAIN)
    growth = growth if len(growth) > 1 else None
    biggest = growth[-1] if growth else None

    # (section, the data it needs). if the data is not there yet the section is simply left out.
    # the README gets the few numbers that matter, RESULTS.md gets every table
    everything = (main, daily, ingest, agent)
    short = [(_machine, main), (_scorecard, main), (_real_volume, daily), (_estimates, daily),
             (_growth_short, growth), (_facts, everything if all(everything) else None)]
    full = [(_machine, main), (_sizes, main), (_timings, main), (_daily_volume, daily),
            (_ingest, ingest), (_distinct, main), (_agent, agent), (_growth, growth),
            (_money, biggest)]

    def join(sections):
        return "\n\n".join(section(data) for section, data in sections if data)

    text = readme.read_text(encoding="utf-8")
    before, rest = text.split(START)
    readme.write_text(f"{before}{START}\n\n{join(short)}\n\n{END}{rest.split(END)[1]}",
                      encoding="utf-8")
    readme.with_name("RESULTS.md").write_text(
        "# Full results\n\nEvery table, written by `python -m logscale report` from the files in "
        f"`results/`.\n\n{join(full)}\n", encoding="utf-8")


def _load(name: str, label: str):
    path = RESULTS / f"{name}_{label}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _table(headers: list, rows: list) -> str:
    lines = ["| " + " | ".join(map(str, headers)) + " |", "|" + "---|" * len(headers)]
    return "\n".join(lines + ["| " + " | ".join(map(str, row)) + " |" for row in rows])


def _pivot(bench: dict, value: str, layouts: list[str]) -> list:
    cell = {(r["query"], r["layout"]): r[value] for r in bench["results"]}
    return [[bench["questions"][q]] + [f"{cell[q, lay]:,}" if (q, lay) in cell else "" for lay in layouts]
            for q in bench["questions"]]


def _cells(bench: dict) -> dict:
    return {(r["query"], r["layout"]): r for r in bench["results"]}


def _scorecard(bench):
    cell = _cells(bench)
    disk = {s["layout"]: s["mb"] for s in bench["sizes"]}
    disk["rollup"] = round(sum(mb for name, mb in disk.items() if name.startswith("rollup_")), 1)
    rows = [[NAMES[lay], f"{disk[lay]:,} MB", f"{cell[ONE_DAY, lay]['median_ms']:,} ms",
             f"{cell[ALL_DAYS, lay]['median_ms']:,} ms", f"{cell[ALL_DAYS, lay]['mb_scanned']:,} MB",
             VERDICTS[lay]] for lay in NAMES if (ONE_DAY, lay) in cell]
    return (f"#### The same {bench['rows']:,} rows, stored six ways\n\n"
            + _table(["Way of storing", "Disk", "\"Failures on one day\"",
                      "\"Fail rate by model, all 90 days\"", "Data read for the 90 day question",
                      "Verdict"], rows))


def _real_volume(bench):
    """The run that matters most: real daily volume, days arriving one after another."""
    cell = _cells(bench)

    def show(query, layout):
        r = cell.get((query, layout))
        return f"{r['median_ms']:,} ms, {r['mb_scanned'] or 'under 0.01'} MB" if r else ""

    rows = [[text, show(q, "by_day"), show(q, "sorted"), show(q, "rollup")]
            for q, text in bench["questions"].items()]
    return (f"#### Real volume: {bench['rows'] // 5_000_000} days at 5M rows a day "
            f"({bench['rows'] / 1e6:,.0f} million rows)\n\n"
            + _table(["Question (time, data read)", "One file per day", "One file per day, sorted",
                      "Summary table"], rows))


def _estimates(bench):
    """Stretch the real-volume run to the 90 day goal. Straight lines, and labelled as estimates."""
    cell, measured = _cells(bench), bench["rows"]
    sorted_mb = next(s["mb"] for s in bench["sizes"] if s["layout"] == "sorted")
    scan, summary = cell[ALL_DAYS, "sorted"], cell[ALL_DAYS, "rollup"]
    rows = [[f"{measured / 1e6:,.0f} million (measured)", f"{sorted_mb / 1000:,.1f} GB",
             f"{scan['median_ms'] / 1000:,.1f} s", f"{scan['mb_scanned']:,.0f} MB",
             f"{summary['median_ms']} ms"]]
    for target, label in ((450_000_000, "450 million, 90 days at 5M"), (1_000_000_000, "1 billion")):
        times = target / measured
        scan_mb = scan["mb_scanned"] * times
        rows.append([f"{label} (estimate)", f"about {sorted_mb * times / 1000:,.0f} GB",
                     f"about {scan['median_ms'] * times / 1000:,.0f} s",
                     f"about {scan_mb:,.0f} MB, ${scan_mb / 1e6 * ATHENA_USD_PER_TB * 1000:,.2f} per 1,000",
                     "about the same"])
    return ("#### From the measured days to the 90 day goal\n\n"
            + _table(["Rows", "Disk, sorted files", "Scan every day", "Data read by that scan",
                      "Same answer from the summary"], rows)
            + "\n\nStraight-line estimates from the run above. They get replaced by real measurements "
              "when this moves from experiment to build, on the production stack. A question about one "
              "day does not grow with history at all, it only opens that day's file.")


def _growth_short(benches):
    rows = []
    for b in (benches[0], benches[-1]):
        cell = _cells(b)
        rows.append([f"{b['rows'] / 1e6:,.0f} million", f"{cell[ALL_DAYS, 'sorted']['median_ms']:,} ms",
                     f"{cell[ONE_DAY, 'sorted']['median_ms']:,} ms",
                     f"{cell[ALL_DAYS, 'rollup']['median_ms']:,} ms"])
    return ("#### Same check at 10x the rows (90 day sets)\n\n"
            + _table(["Rows", "Scan all 90 days", "Ask about one day", "Ask the summary table"], rows))


def _facts(data):
    main, daily, ingest, agent = data
    cell = _cells(main)
    blob, column = cell["failures_by_conn_json", "sorted"], cell["failures_by_conn_column", "sorted"]
    seconds = [r["seconds"] for r in ingest]
    first = ingest[0]
    refused = sum(r["verdict"] == "rejected" for r in agent)
    model, os_name, why, count = next(r["found"] for r in agent if r["found"])[0]
    small, big = main["distinct_counts"], daily["distinct_counts"]

    def errors(counts):
        return ", ".join(f"{w['error_pct']}% over {w['window']}" for w in counts["exact_vs_approx"])

    facts = [
        (f"The daily job, {len(ingest)} days in a row: {first['rows_in']:,} rows of raw `csv.gz` in "
         f"({first['csv_gz_mb']} MB), {first['rows_out']:,} cleaned rows out ({first['parquet_mb']} MB), "
         f"{first['duplicates_dropped']:,} re-sent events dropped, "
         f"**{min(seconds)} to {max(seconds)} seconds per day** on a laptop."),
        (f"A question that digs inside the JSON text: {blob['median_ms']} ms, {blob['mb_scanned']} MB "
         f"read. Same question on a proper column: **{column['median_ms']} ms, "
         f"{column['mb_scanned']} MB read**."),
        (f"Approximate unique counts: off by {errors(small)} on the 10M set, and by {errors(big)} at "
         f"5M rows a day. The error depends on how many different devices fall in the window, so it "
         f"has to be checked on the real engine before it is trusted."),
        (f"Daily unique counts cannot be added up. On the 10M set that gives "
         f"{small['same_thing_by_adding_daily_counts']:,} against a true "
         f"{small['devices_with_a_failure_this_week']:,} ({small['overcount_pct']}% too high). At 5M "
         f"rows a day it gives {big['same_thing_by_adding_daily_counts']:,} against "
         f"{big['devices_with_a_failure_this_week']:,} (**{big['overcount_pct']}% too high**)."),
        (f"The agent tool refused **{refused} of {len(agent)}** query attempts, each with a reason, "
         f"and found the planted incident: **{model} on {os_name}, \"{why}\", {count:,} failures**."),
    ]
    return ("#### Other measured facts\n\n" + "\n".join("- " + fact for fact in facts)
            + "\n\nEvery table behind these numbers is in [RESULTS.md](RESULTS.md).")


def _machine(bench):
    m = bench["machine"]
    return (f"_Measured on: {m['os']}, {m['logical_cpus']} logical CPUs, DuckDB {m['duckdb']}, "
            f"Python {m['python']}. Timings are the median of 7 warm runs (3 for csv)._")


def _sizes(bench):
    rows = [[s["layout"], f"{s['files']:,}", f"{s['mb']:,}", s["bytes_per_row"]] for s in bench["sizes"]]
    return (f"#### Same {bench['rows']:,} rows, stored in different ways\n\n"
            + _table(["layout", "files", "MB", "bytes per row"], rows))


def _timings(bench):
    layouts = ["csv", "single", "by_day", "sorted", "tiny_files", "rollup"]
    return ("#### Median time per question, in ms\n\n"
            + _table(["question", *layouts], _pivot(bench, "median_ms", layouts))
            + "\n\n#### Estimated MB read per question\n\n"
            + _table(["question", *layouts], _pivot(bench, "mb_scanned", layouts)))


def _daily_volume(bench):
    cell = {(r["query"], r["layout"]): r for r in bench["results"]}
    rows = []
    for q, text in bench["questions"].items():
        if (q, "by_day") in cell:
            a, b = cell[q, "by_day"], cell[q, "sorted"]
            rows.append([text, a["median_ms"], b["median_ms"], a["mb_scanned"], b["mb_scanned"]])
    return ("#### Real daily volume (5M rows a day): plain daily files vs sorted daily files\n\n"
            + _table(["question", "by_day ms", "sorted ms", "by_day MB", "sorted MB"], rows))


def _ingest(report):
    keys = ["date", "rows_in", "duplicates_dropped", "rows_out", "seconds", "csv_gz_mb", "parquet_mb"]
    return ("#### The daily job: raw csv.gz in, cleaned parquet out\n\n"
            + _table([k.replace("_", " ") for k in keys],
                     [[f"{r[k]:,}" if isinstance(r[k], int) else r[k] for k in keys] for r in report]))


def _distinct(bench):
    d = bench["distinct_counts"]
    rows = [[w["window"], f"{w['exact']:,}", w["exact_median_ms"], f"{w['approx']:,}",
             w["approx_median_ms"], f"{w['error_pct']}%"] for w in d["exact_vs_approx"]]
    return ("#### Unique devices: exact vs approximate\n\n"
            + _table(["window", "exact", "ms", "approximate", "ms", "off by"], rows)
            + f"\n\nDevices with a failure in one week, counted properly: "
              f"**{d['devices_with_a_failure_this_week']:,}**. Adding up the 7 daily counts gives "
              f"**{d['same_thing_by_adding_daily_counts']:,}**, which is {d['overcount_pct']}% too high.")


def _agent(outcome):
    rows = [[i, r["tried"], r["verdict"], r["ms"] or "", r["note"]] for i, r in enumerate(outcome, 1)]
    found = next((r["found"] for r in outcome if r["found"]), [])
    return ("#### What the agent query tool did with 12 attempts\n\n"
            + _table(["#", "what the llm tried", "verdict", "ms", "note"], rows)
            + "\n\nAnswer to \"what broke on the bad day\": "
            + "; ".join(f"{m} + {o}, {why}: {n:,}" for m, o, why, n in found))


def _growth(benches):
    lines = {"all 90 days, sorted files": ("fail_rate_by_model_os", "sorted"),
             "one day, sorted files": ("failures_one_day", "sorted"),
             "all 90 days, summary table": ("fail_rate_by_model_os", "rollup")}
    series = {name: [next(r["median_ms"] for r in b["results"] if (r["query"], r["layout"]) == key)
                     for b in benches] for name, key in lines.items()}
    rows = [[f"{b['rows']:,}"] + [series[name][i] for name in lines] for i, b in enumerate(benches)]
    return ("#### How the time grows when the data grows (ms)\n\n"
            + _table(["rows", *lines], rows))


def _money(bench):
    rows = []
    for r in bench["results"]:
        if r["layout"] == "sorted":
            billed = max(r["mb_scanned"], ATHENA_MIN_MB)
            rows.append([bench["questions"][r["query"]], r["mb_scanned"],
                         f"${billed / 1e6 * ATHENA_USD_PER_TB * 1000:.3f}"])
    return (f"#### What the MB would cost on a pay-per-scan engine ({bench['rows']:,} rows, sorted)\n\n"
            + _table(["question", "MB read", "cost of 1,000 such queries"], rows)
            + f"\n\nPriced at ${ATHENA_USD_PER_TB} per TB with a {ATHENA_MIN_MB} MB minimum per query "
              "(Athena's public pricing). Below the minimum, reading less saves time but not money.")
