import json
from pathlib import Path

from .config import Config

RESULTS = Path("results")
START, END = "<!-- results:start -->", "<!-- results:end -->"
MAIN, DAILY = "10M", "10M-2d"   # 10M = every layout compared, 10M-2d = real daily volume (5M a day)
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


def save(name: str, cfg: Config, data) -> Path:
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{name}_{cfg.label}.json"
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
    growth = [b for b in growth if b["days"] == 90 and main and b["rows"] >= main["rows"]]
    ingest, agent = _load("ingest", DAILY), _load("agent_demo", MAIN)
    growth = growth if len(growth) > 1 else None
    biggest = growth[-1] if growth else None

    # (section, the data it needs). if the data is not there yet the section is simply left out.
    # the README gets the few numbers that matter, RESULTS.md gets every table
    everything = (main, ingest, agent)
    short = [(_machine, main), (_scorecard, main), (_sorting, daily), (_growth_short, growth),
             (_facts, everything if all(everything) else None)]
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


def _sorting(bench):
    cell = _cells(bench)
    rows = [[bench["questions"][q]]
            + [f"{cell[q, lay]['median_ms']:,} ms, {cell[q, lay]['mb_scanned'] or 'under 0.01'} MB read"
               for lay in ("by_day", "sorted")]
            for q in ("devices_hit_by_error", "one_device_history", ONE_DAY)]
    return ("#### Does sorting matter? At real volume, 5M rows a day\n\n"
            + _table(["Question", "One file per day", "One file per day, sorted"], rows))


def _growth_short(benches):
    rows = []
    for b in (benches[0], benches[-1]):
        cell = _cells(b)
        rows.append([f"{b['rows'] / 1e6:,.0f} million", f"{cell[ALL_DAYS, 'sorted']['median_ms']:,} ms",
                     f"{cell[ONE_DAY, 'sorted']['median_ms']:,} ms",
                     f"{cell[ALL_DAYS, 'rollup']['median_ms']:,} ms"])
    big = benches[-1]
    if big["rows"] < 900_000_000:
        # no 1B run on disk yet, so this line is worked out from the trend, and I say so
        cell, times = _cells(big), 1_000_000_000 / big["rows"]
        sorted_mb = next(s["mb"] for s in big["sizes"] if s["layout"] == "sorted")
        scan_mb = cell[ALL_DAYS, "sorted"]["mb_scanned"] * times
        rows.append(["1 billion (projected, not measured)",
                     f"about {cell[ALL_DAYS, 'sorted']['median_ms'] * times / 1000:,.0f} s",
                     "tens of ms", "unchanged"])
        note = (f"\n\nThe last line is a straight-line projection from the measured runs. At that size "
                f"the sorted files would take about {sorted_mb * times / 1000:,.0f} GB, and the 90 day "
                f"scan would read about {scan_mb:,.0f} MB, around "
                f"${scan_mb / 1e6 * ATHENA_USD_PER_TB * 1000:,.2f} per 1,000 such queries on a "
                f"pay-per-scan engine. This is why the agent is not allowed to scan all 90 days.")
    else:
        note = ""
    return ("#### What happens when the data grows\n\n"
            + _table(["Rows", "Scan all 90 days", "Ask about one day", "Ask the summary table"], rows)
            + note + "\n\n![growth](results/growth.png)")


def _facts(data):
    main, ingest, agent = data
    cell, d = _cells(main), main["distinct_counts"]
    blob, column = cell["failures_by_conn_json", "sorted"], cell["failures_by_conn_column", "sorted"]
    seconds = [r["seconds"] for r in ingest]
    errors = ", ".join(f"{w['error_pct']}% over {w['window']}" for w in d["exact_vs_approx"])
    refused = sum(r["verdict"] == "rejected" for r in agent)
    model, os_name, why, count = next(r["found"] for r in agent if r["found"])[0]
    return f"""#### Other measured facts

- The daily job turns a {ingest[0]['rows_in']:,} row raw `csv.gz` into a cleaned, sorted file in \
**{min(seconds)} to {max(seconds)} seconds** on a laptop, and dropped \
{ingest[0]['duplicates_dropped']:,} re-sent events on the way.
- A question that digs inside the JSON text: {blob['median_ms']} ms, {blob['mb_scanned']} MB read. \
Same question on a proper column: **{column['median_ms']} ms, {column['mb_scanned']} MB read**.
- Approximate unique counts were off by {errors}. Fine for a long window, not for a short one.
- Adding up 7 daily unique counts gives {d['same_thing_by_adding_daily_counts']:,}. The true weekly \
number is {d['devices_with_a_failure_this_week']:,}. That is {d['overcount_pct']}% too high, so \
unique counts cannot come from daily summaries.
- The agent tool refused **{refused} of {len(agent)}** query attempts, each with a reason, and found \
the planted incident: **{model} on {os_name}, "{why}", {count:,} failures**.

Every table behind these numbers is in [RESULTS.md](RESULTS.md)."""


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
    _chart([b["rows"] / 1e6 for b in benches], series)
    rows = [[f"{b['rows']:,}"] + [series[name][i] for name in lines] for i, b in enumerate(benches)]
    return ("#### How the time grows when the data grows (ms)\n\n"
            + _table(["rows", *lines], rows) + "\n\n![growth](results/growth.png)")


def _chart(million_rows: list, series: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker

    fig, ax = plt.subplots(figsize=(7, 3.6))
    for name, values in series.items():
        ax.plot(million_rows, values, marker="o", linewidth=1.5)
        ax.annotate(name, (million_rows[-1], values[-1]), xytext=(6, 0),
                    textcoords="offset points", va="center", fontsize=8)
    # log scale, otherwise the two fast lines lie flat on top of each other at the bottom
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}"))
    ax.set_xlabel("rows in the dataset (millions)")
    ax.set_ylabel("median time (ms, log scale)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_xlim(right=million_rows[-1] * 1.45)
    fig.tight_layout()
    fig.savefig(RESULTS / "growth.png", dpi=150)
    plt.close(fig)


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
