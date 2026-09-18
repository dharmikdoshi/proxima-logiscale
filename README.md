# log-scaling

## Problem

> You are ingesting 5M+ rows a day, around 1B rows over a 90 day window. How would you handle
> analytical and agent queries at that scale efficiently? Back it with experimental code.

5M rows a day is about 58 rows a second. 1B rows over 90 days is about 130 a second. In compressed
files that is around 10 to 15 GB. So this is not a cluster problem, it is a "how do I lay the data out"
problem. That is what I tested.

## Options I looked at

| Option | Good at | Problem for this workload | What I measured |
|---|---|---|---|
| CSV files | Simple, it is how data arrives | Every question reads the whole file. Biggest on disk | 1,607 MB for 10M rows (127 MB per 5M-row day when gzipped), about 4 s per question |
| Parquet files | Reads only the days and columns a question needs. Compresses well | Cannot edit one row. A bad day means rewriting that day's file | 173 MB for the same rows, 17 ms for a one-day question |
| PostgreSQL | Finding one record by index. Data that gets edited | Reads whole rows to count one column. Slow to load, slower as days pile up | 82 to 188 s to load a day Parquet takes in 10 to 14 s. 2.9 s against 45 ms on the same question after 4 days |
| Polars DataFrames | Extremely fast on the same Parquet files | Every query is DataFrame code. Moving to Athena later means rewriting each one as SQL. With SQL over Parquet there is nothing to rewrite | Not tried |

## What I picked

Raw data stays as it arrives. Once a day it is cleaned into one sorted Parquet file for that day.
Questions that get asked again and again are answered from two small summary tables. Anything else
reads only the day files and columns it needs. The agent never touches the engine directly, it goes
through one tool that checks its SQL first. PostgreSQL keeps the jobs it is best at: the summary
tables, single-record lookups and data that gets edited.

## How it works, step by step

| Step | File | What it does | Took here |
|---|---|---|---|
| Make test data | `generate.py` | One SQL statement makes a day of events. Same seed, same data, on any machine | 12 s per 5M rows |
| Daily job | `ingest.py` | Raw `csv.gz` in. Sets column types, drops re-sent events, pulls `conn` out of the JSON, sorts, writes Parquet | 15 to 28 s per 5M rows |
| Other layouts | `layouts.py` | Same rows as CSV, one big file, tiny files, plus the summary tables | about 2 min for 10M rows |
| Questions and timing | `queries.py`, `bench.py` | Seven questions, 7 runs each, per layout | about 4 min, mostly CSV |
| Agent tool | `agent_tool.py` | Checks SQL before it runs. `ask "SELECT ..."` lets you try it | milliseconds |
| PostgreSQL side | `pg.py` | Same cleaned days into Postgres. `ask --both` asks both engines | 82 to 188 s per 3M rows |
| Report | `report.py` | Fills the results block below from `results/` | 1 s |

A note on the daily job timing. There are no real devices here, so before each day is ingested the
tool first writes that day out as a gzipped CSV, to play the file arriving. That takes about 50 s
and is not part of the measurement. The 15 to 28 s is only the real job: raw file in, cleaned file out.

## Six ways I stored the same rows

| Way | Why I tried it | Verdict |
|---|---|---|
| Raw CSV | Baseline, it is how data arrives | Keep only as the untouched original |
| One big Parquet file | Simplest Parquet setup | Fine when small. At 50M rows a one-day question was 2 to 3x slower than on daily files, full scans about the same. Fixing one day means rewriting everything |
| One Parquet file per day | A question about one day should open one file | Good |
| One file per day, sorted | Failures are about 6% of rows, so keep them together | **Chosen** |
| Thousands of tiny files | What a streaming loader leaves behind | Avoid. Same data, 10 to 30x slower |
| Summary tables | Known questions should not rescan raw events | For the repeated questions |

## Seven questions, and what each one tests

| Question | Tests |
|---|---|
| Failures on one day | Does a one-day question open only one day? |
| Top error codes this week, with their meaning | A join, over a week |
| Devices hit by error 1042 this week | Unique counting, filter on a rare value |
| Fail rate by model and OS, every day | The worst case, a full scan |
| Last 100 events of one device | Single-record lookup, where a row store should win |
| Failures by connection type, from the JSON | Cost of digging inside a JSON blob |
| Same, from a proper column | What pulling that field out saves |

The data has one planted incident: on 2026-07-31 one device model on one OS fails about 40% of the
time with one error code. So there is a known right answer to find.

## Tests I ran

| Test | Size | Parquet | PostgreSQL |
|---|---|---|---|
| Six layouts, seven questions, agent tool | 10M rows over 90 days | yes | no |
| Real volume, days arriving one by one | 5M rows a day, 10 days, 50M rows | yes | no |
| Same check at 10x the rows | 10M to 100M rows | yes | no |
| Both engines side by side | 3M rows a day, 4 days | yes | yes |

DuckDB stands in for Athena. Both read Parquet with SQL and skip data the same way. I had no cloud
access, so the claim is about the layout, not about one engine matching the other's speed.

## Results

<!-- results:start -->

_Measured on: Windows-11-10.0.26200-SP0, 8 logical CPUs, DuckDB 1.5.5, Python 3.12.2. Timings are the median of 7 warm runs (3 for csv)._

#### The same 9,999,990 rows, stored six ways

| Way of storing | Disk | "Failures on one day" | "Fail rate by model, all 90 days" | Data read for the 90 day question | Verdict |
|---|---|---|---|---|---|
| Raw CSV, as it arrives | 1,606.6 MB | 4,798.7 ms | 4,221.1 ms | 1,606.61 MB | Keep only as the untouched original |
| One big file | 179.6 MB | 14.3 ms | 170.2 ms | 8.52 MB | Fast, but fixing one day means rewriting everything |
| One file per day | 177.8 MB | 17.6 ms | 213.9 ms | 8.52 MB | Good |
| **One file per day, sorted** | 172.8 MB | 16.9 ms | 159.1 ms | 3.84 MB | **Chosen** |
| Thousands of tiny files | 235.5 MB | 490.8 ms | 1,820.3 ms | 10.84 MB | What to avoid: same data, far slower |
| Summary tables | 0.3 MB | 2.4 ms | 5.7 ms | 0.15 MB | Used for the known questions |

#### Real volume: 10 days at 5M rows a day (50 million rows)

| Question (time, data read) | One file per day | One file per day, sorted | Summary table |
|---|---|---|---|
| How many events failed on one given day? | 19.7 ms, 0.26 MB | 9.9 ms, under 0.01 MB | 2.3 ms, 0.02 MB |
| Top 10 error codes of the week, with their meaning (a join) | 239.9 ms, 5.99 MB | 67.8 ms, 0.01 MB | 6.7 ms, 0.02 MB |
| How many different devices hit error 1042 this week? | 366.7 ms, 115.81 MB | 33.1 ms, 2.73 MB |  |
| Which model + os combos fail the most, over all days? | 587.5 ms, 42.41 MB | 603.6 ms, 18.53 MB | 4.8 ms, 0.02 MB |
| Last 100 events of one device in the past 30 days | 1,185.5 ms, 401.06 MB | 210.1 ms, 37.48 MB |  |
| Failures per connection type, digging inside the json blob | 371.3 ms, 48.57 MB | 253.6 ms, 3.41 MB |  |
| Same question, but conn is a proper column now |  | 27.8 ms, 0.45 MB |  |

#### From the measured days to the 90 day goal

| Rows | Disk, sorted files | Scan every day | Data read by that scan | Same answer from the summary |
|---|---|---|---|---|
| 50 million (measured) | 0.7 GB | 0.6 s | 19 MB | 4.8 ms |
| 450 million, 90 days at 5M (estimate) | about 7 GB | about 5 s | about 167 MB, $0.83 per 1,000 | about the same |
| 1 billion (estimate) | about 14 GB | about 12 s | about 371 MB, $1.85 per 1,000 | about the same |

Straight-line estimates from the run above, to be checked on the real stack. A question about one day does not grow with history, it only opens that day's file.

#### Same check at 10x the rows (90 day sets)

| Rows | Scan all 90 days | Ask about one day | Ask the summary table |
|---|---|---|---|
| 10 million | 159.1 ms | 16.9 ms | 5.7 ms |
| 100 million | 1,298.1 ms | 22.5 ms | 7.3 ms |

#### Other measured facts

- The daily job, 10 days in a row: 5,005,000 rows of raw `csv.gz` in (127.4 MB), 5,000,000 cleaned rows out (72.5 MB), 5,000 re-sent events dropped, **14.6 to 27.9 seconds per day** on a laptop.
- A question that digs inside the JSON text: 27.0 ms, 1.04 MB read. Same question on a proper column: **18.5 ms, 0.14 MB read**.
- Approximate unique counts: off by 1.16% over all days, 7.65% over one week, 14.34% over one day on the 10M set, and by 1.16% over all days, 1.16% over one week, 0.56% over one day at 5M rows a day. The error depends on how many different devices fall in the window, so it has to be checked on the real engine before it is trusted.
- Daily unique counts cannot be added up. On the 10M set that gives 41,262 against a true 38,373 (7.5% too high). At 5M rows a day it gives 1,237,278 against 455,635 (**171.6% too high**).
- The agent tool refused **7 of 12** query attempts, each with a reason, and found the planted incident: **KR-20 on win11, "update download failed", 823 failures**.

Every table behind these numbers is in [RESULTS.md](RESULTS.md).

<!-- results:end -->

### Parquet against PostgreSQL, day by day

Four days at 3M rows a day, the same cleaned rows into both. Postgres got one partition per day,
three indexes, fresh statistics and memory settings tuned for the laptop. Same question after each
day ("failures per day, over the days loaded so far"), and the answers matched every time.

| Days loaded | Parquet | PostgreSQL |
|---|---|---|
| 1 | 28 ms | 184 ms |
| 2 | 27 ms | 929 ms |
| 3 | 37 ms | 517 ms |
| 4 | 45 ms | 2,854 ms |

Loading one day of 3M rows: Parquet 10 to 14 s, Postgres 82 to 188 s.

These are typed from my terminal, there is no results file behind them, so treat them as
indicative. The setup is also unkind to Postgres: indexes existed before the load, no VACUUM, and
Docker on Windows costs it disk speed. The 929 then 517 ms wobble is that kind of noise. A tuned
Postgres would close part of the gap, not an order of magnitude. That was enough to answer "why not
just Postgres", so I stopped there and spent the time on the option that was working. For scale:
at 5M rows a day the same Parquet question took 14, 34, 66 and 80 ms over 1, 4, 7 and 10 days.
Steps to repeat it are under "Trying the PostgreSQL comparison" below.

### Twelve SQL attempts on the agent tool

| Attempt | Why I tested it | Result |
|---|---|---|
| `DELETE ...` | The agent must never change data | refused |
| Two statements in one | Blocks `SELECT 1; DROP ...` | refused |
| `read_csv('C:/...')` | No reading files off the disk | refused |
| `SELECT * FROM events` | No lazy full-table pulls | refused |
| Day filter with no end | An open range is a full scan in disguise | refused |
| All 90 days of raw events | Long ranges belong to the summaries | refused |
| A month of the JSON column | Scan budget, too many MB | refused |
| Weekly failures from the summary | The cheap path works | ok |
| Same question from raw events | The detail path gives the same answer | ok |
| "What broke on the bad day?" | Can it find the planted incident | ok, found it |
| One device, no LIMIT | Row limit gets added | ok, LIMIT 1000 added |
| Summary question again, typed differently | Cache | cache hit |

The SQL is parsed into a tree and checked, not text-searched, because a search for "DELETE" is easy
to get around. `tests/test_agent_tool.py` lists 34 queries it has to refuse, including seven that
got through in a review and are now closed: dates that are computed, a second copy of the table with
no date filter, the whole row selected by its alias, and `list()` packing millions of values into one
row. When it refuses, it says why, so the model can fix the query and retry. It is a first layer. A
read-only database role belongs underneath it.

## Why each decision

- **Parquet over CSV.** About 9x smaller than plain CSV, about 1.75x smaller than the gzipped CSV that arrives, and questions in ms instead of seconds.
- **One file per day, not per hour.** A day is a good file size. Hourly files would be tiny, and tiny files were the slowest thing I measured.
- **Daily files over one big file.** Same speed at 10M rows. At 50M rows one-day questions were 2 to 3x faster on daily files, full scans about equal. And a bad day can be replaced alone.
- **Sorted inside each file.** Did nothing at 111K rows a day. At 5M rows a day it made most questions several times faster and cut the data read by 10x or more.
- **Summary tables.** A few ms no matter how much raw data there is. They only answer planned questions, and unique counts cannot come from them.
- **JSON fields asked about often become real columns.** Same question, about 7x less data read, and up to 9x faster at real volume.
- **Exact unique counts unless the window is huge.** The approximate count was anywhere from 0.6% to about 20% off across my runs. I have not dug into why it swings that much, so I do not trust it yet.
- **Agent rules in code, not in the prompt.** Read only, approved tables, at most 31 days of raw events, row limit, scan budget, timeout.
- **No embeddings for event rows.** These are counting questions, and SQL does that. If runbooks or incident notes need searching later, pgvector would do.
- **PostgreSQL stays, for what it is good at.** Summaries, lookups, anything edited.

## What I would build

| Piece | Choice |
|---|---|
| Ingest | The existing service into Firehose, big buffers so tiny files never appear |
| Storage | Iceberg tables on S3, a partition per day, sorted inside, managed compaction, 90 day expiry |
| Daily job | One small job per day. Days are independent, so it spreads over as many workers as needed and is safe to re-run |
| Summaries, run tracking, edited data | PostgreSQL |
| Detail questions | Athena, with a per-query scan cap |
| Cache | Redis. Key is the cleaned-up SQL plus the version of the days it touches, so re-ingesting a day retires its old answers. 24h for finished days, minutes for today |
| Agent | The guarded tool, summaries first |

Wrong data: keep the raw file, re-clean that day, swap it in. Re-sent events are dropped by their id,
that day's summary rows are refreshed, the day's version goes up.

After it ships I would keep an eye on data read and p95 per query, file sizes, how fresh the
summaries are, and how often the agent gets refused. Once a month, look at the most expensive
questions and give them a summary or a better sort order.

## How to run

Needs [uv](https://docs.astral.sh/uv/). No Docker, no cloud account.

    uv sync
    uv run python -m logscale all

About 35 minutes and 8 GB of disk. It repeats the runs reported here, the six layouts at 10M rows
and then ten days at 5M rows a day, and rewrites the results block with your numbers.
`uv run pytest` runs the 109 tests.

| Command | What it does |
|---|---|
| `generate --per-day 5M --from-date 2026-07-22 --to-date 2026-07-31` | Ten days of events |
| `ingest --per-day 5M --from-date 2026-07-22 --to-date 2026-07-31` | The daily job for each of them |
| `build --per-day 5M` | Other layouts and the summary tables |
| `bench --per-day 5M` | The seven questions, timed |
| `ask --per-day 5M "SELECT ..."` | Your own SQL through the agent's checks. `--both` also asks Postgres |
| `agent-demo --rows 10M` | The twelve attempts |
| `growth` | Timings at 25M, 50M and 100M rows, about 30 min |
| `report` | Rewrites the numbers here and in RESULTS.md |
| `peek`, `browse` | Look at the data in the terminal, or in TablePlus / DBeaver |

All as `uv run python -m logscale <command>`. `--rows 10M` is a whole 90 day set, `--per-day 5M` is a
day-by-day set. Dates are `YYYY-MM-DD`, the window is 2026-06-01 to 2026-08-29. Every step skips work
already done, so a stopped run just continues. The folders under `data/` are separate test sizes. A
real system has one table with one file per day.

## Trying the PostgreSQL comparison (optional)

Only needed if you want to repeat the Parquet vs PostgreSQL test. Docker Desktop has to be running.

    docker compose up -d
    uv sync --group pg

Then one day into both, and the same question to both:

    uv run python -m logscale generate --per-day 3M --date 2026-07-31
    uv run python -m logscale ingest   --per-day 3M --date 2026-07-31
    uv run python -m logscale pg-load  --per-day 3M --date 2026-07-31
    uv run python -m logscale ask --both --per-day 3M "SELECT status, count(*) AS events FROM events WHERE day = DATE '2026-07-31' GROUP BY 1"

`ask --both` prints the time for each engine and checks both gave the same answer. Loading a day
into Postgres takes two to three minutes, so be patient on `pg-load`. Repeat the three middle
commands with other dates to add more days.

`pg-bench --rows 10M` runs all seven questions on Postgres and saves them under `results/`. It
needs the 10M set loaded first (`pg-load --rows 10M --from-date 2026-06-01 --to-date 2026-08-29`).

To look inside with TablePlus or DBeaver: host `localhost`, port `5433`, database, user and
password all `logscale`.

    docker compose down        (stops it, keeps the data)
    docker compose down -v     (stops it and wipes the data)

## Notes

Everything under Results is measured: DuckDB on Parquet files, and PostgreSQL in Docker, all on my
laptop. Timings are warm and single-user, so a few ms either way is noise, and your milliseconds
will differ from mine. The data is synthetic. "Data read" is worked out from the statistics Parquet
keeps about itself plus the columns I listed for each question in `queries.py`, not measured at the
disk.

The 90 day and 1B row numbers are estimates. I ran 10 real days at 5M rows a day and stretched the
line from there.

Athena, S3, Iceberg, Firehose and Redis are what I'd build this on. I haven't run them yet. DuckDB
reads Parquet with SQL the same way Athena does, so I expect the layout findings to hold. Next step
is to run the same seven questions there and see.

A couple of things I'd like to try when there's time: Polars on the same files, a full 90 days at
real volume, and a small set of agent questions with known answers, to see how often it gets them
right.
