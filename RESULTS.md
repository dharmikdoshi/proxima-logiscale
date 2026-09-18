# Full results

Every table, written by `python -m logscale report` from the files in `results/`.

_Measured on: Windows-11-10.0.26200-SP0, 8 logical CPUs, DuckDB 1.5.5, Python 3.12.2. Timings are the median of 7 warm runs (3 for csv)._

#### Same 9,999,990 rows, stored in different ways

| layout | files | MB | bytes per row |
|---|---|---|---|
| csv | 1 | 1,606.6 | 160.7 |
| single | 1 | 179.6 | 18.0 |
| by_day | 90 | 177.8 | 17.8 |
| sorted | 90 | 172.8 | 17.3 |
| tiny_files | 4,500 | 235.5 | 23.6 |
| rollup_daily_health | 1 | 0.2 | 0.0 |
| rollup_daily_errors | 1 | 0.1 | 0.0 |

#### Median time per question, in ms

| question | csv | single | by_day | sorted | tiny_files | rollup |
|---|---|---|---|---|---|---|
| How many events failed on one given day? | 4,798.7 | 14.3 | 17.6 | 16.9 | 490.8 | 2.4 |
| Top 10 error codes of the week, with their meaning (a join) | 4,176.9 | 27.3 | 40.7 | 25.2 | 638.9 | 5.8 |
| How many different devices hit error 1042 this week? | 4,499.9 | 27.1 | 28.1 | 27.3 | 611.1 |  |
| Which model + os combos fail the most, over all days? | 4,221.1 | 170.2 | 213.9 | 159.1 | 1,820.3 | 5.7 |
| Last 100 events of one device in the past 30 days | 4,956.0 | 103.4 | 139.6 | 107.7 | 1,124.5 |  |
| Failures per connection type, digging inside the json blob | 5,300.3 | 25.2 | 35.1 | 27.0 | 652.5 |  |
| Same question, but conn is a proper column now |  |  |  | 18.5 |  |  |

#### Estimated MB read per question

| question | csv | single | by_day | sorted | tiny_files | rollup |
|---|---|---|---|---|---|---|
| How many events failed on one given day? | 1,606.61 | 0.01 | 0.01 | 0.0 | 0.01 | 0.15 |
| Top 10 error codes of the week, with their meaning (a join) | 1,606.61 | 0.15 | 0.13 | 0.01 | 0.27 | 0.07 |
| How many different devices hit error 1042 this week? | 1,606.61 | 2.86 | 2.57 | 2.45 | 2.63 |  |
| Which model + os combos fail the most, over all days? | 1,606.61 | 8.52 | 8.52 | 3.84 | 10.84 | 0.15 |
| Last 100 events of one device in the past 30 days | 1,606.61 | 28.11 | 26.79 | 22.06 | 28.0 |  |
| Failures per connection type, digging inside the json blob | 1,606.61 | 1.2 | 1.08 | 1.04 | 3.14 |  |
| Same question, but conn is a proper column now |  |  |  | 0.14 |  |  |

#### Real daily volume (5M rows a day): plain daily files vs sorted daily files

| question | by_day ms | sorted ms | by_day MB | sorted MB |
|---|---|---|---|---|
| How many events failed on one given day? | 19.7 | 9.9 | 0.26 | 0.0 |
| Top 10 error codes of the week, with their meaning (a join) | 239.9 | 67.8 | 5.99 | 0.01 |
| How many different devices hit error 1042 this week? | 366.7 | 33.1 | 115.81 | 2.73 |
| Which model + os combos fail the most, over all days? | 587.5 | 603.6 | 42.41 | 18.53 |
| Last 100 events of one device in the past 30 days | 1185.5 | 210.1 | 401.06 | 37.48 |
| Failures per connection type, digging inside the json blob | 371.3 | 253.6 | 48.57 | 3.41 |

#### The daily job: raw csv.gz in, cleaned parquet out

| date | rows in | duplicates dropped | rows out | seconds | csv gz mb | parquet mb |
|---|---|---|---|---|---|---|
| 2026-07-22 | 5,005,000 | 5,000 | 5,000,000 | 15.4 | 127.4 | 72.5 |
| 2026-07-23 | 5,005,000 | 5,000 | 5,000,000 | 15.0 | 127.9 | 72.9 |
| 2026-07-24 | 5,005,000 | 5,000 | 5,000,000 | 15.9 | 126.9 | 72.5 |
| 2026-07-25 | 5,005,000 | 5,000 | 5,000,000 | 15.0 | 127.8 | 72.8 |
| 2026-07-26 | 5,005,000 | 5,000 | 5,000,000 | 14.8 | 127.4 | 72.6 |
| 2026-07-27 | 5,005,000 | 5,000 | 5,000,000 | 14.6 | 126.6 | 71.0 |
| 2026-07-28 | 5,005,000 | 5,000 | 5,000,000 | 27.9 | 127.1 | 71.9 |
| 2026-07-29 | 5,005,000 | 5,000 | 5,000,000 | 17.7 | 127.2 | 72.1 |
| 2026-07-30 | 5,005,000 | 5,000 | 5,000,000 | 24.7 | 128.2 | 73.1 |
| 2026-07-31 | 5,005,000 | 5,000 | 5,000,000 | 19.8 | 126.8 | 72.7 |

#### Unique devices: exact vs approximate

| window | exact | ms | approximate | ms | off by |
|---|---|---|---|---|---|
| all days | 499,991 | 518.1 | 505,811 | 174.5 | 1.16% |
| one week | 345,337 | 53.9 | 318,905 | 38.7 | 7.65% |
| one day | 91,273 | 26.5 | 104,357 | 18.0 | 14.34% |

Devices with a failure in one week, counted properly: **38,373**. Adding up the 7 daily counts gives **41,262**, which is 7.5% too high.

#### What the agent query tool did with 12 attempts

| # | what the llm tried | verdict | ms | note |
|---|---|---|---|---|
| 1 | tries to delete | rejected |  | only SELECT is allowed, this is a DELETE |
| 2 | two statements in one | rejected |  | send exactly one statement |
| 3 | reads a file from disk | rejected |  | reading files or table functions directly is not allowed |
| 4 | lazy select star, no dates | rejected |  | SELECT * on events is not allowed, name the columns you need |
| 5 | day filter with no end | rejected |  | query on events needs a day filter with a start and an end, like day BETWEEN DATE '2026-06-01' AND DATE '2026-06-07'. for totals and rates use rollup_daily_health or rollup_daily_errors |
| 6 | all 90 days of raw events | rejected |  | day window on events is wider than 31 days. for totals and rates use rollup_daily_health or rollup_daily_errors |
| 7 | a month of the fat json column | rejected |  | would read about 15 MB, budget is 10 MB. ask for fewer days or fewer columns, for totals and rates use rollup_daily_health or rollup_daily_errors |
| 8 | weekly failures, from summary | ok | 15.9 | LIMIT 1000 added |
| 9 | weekly failures, from raw events | ok | 25.7 | LIMIT 1000 added |
| 10 | what broke on the bad day | ok | 52.7 |  |
| 11 | one device, forgot the limit | ok | 77.7 | LIMIT 1000 added |
| 12 | weekly failures from summary, again | cache hit |  | LIMIT 1000 added |

Answer to "what broke on the bad day": KR-20 + win11, update download failed: 823; DX-100 + win11, connection timeout: 48; DX-100 + macos15, connection timeout: 35

#### How the time grows when the data grows (ms)

| rows | all 90 days, sorted files | one day, sorted files | all 90 days, summary table |
|---|---|---|---|
| 9,999,990 | 159.1 | 16.9 | 5.7 |
| 24,999,930 | 367.9 | 21.8 | 7.2 |
| 49,999,950 | 613.5 | 33.2 | 7.8 |
| 99,999,990 | 1298.1 | 22.5 | 7.3 |

#### What the MB would cost on a pay-per-scan engine (99,999,990 rows, sorted)

| question | MB read | cost of 1,000 such queries |
|---|---|---|
| How many events failed on one given day? | 0.0 | $0.050 |
| Top 10 error codes of the week, with their meaning (a join) | 0.01 | $0.050 |
| How many different devices hit error 1042 this week? | 2.74 | $0.050 |
| Which model + os combos fail the most, over all days? | 37.34 | $0.187 |
| Last 100 events of one device in the past 30 days | 42.29 | $0.211 |
| Failures per connection type, digging inside the json blob | 1.19 | $0.050 |
| Same question, but conn is a proper column now | 0.16 | $0.050 |

Priced at $5 per TB with a 10 MB minimum per query (Athena's public pricing). Below the minimum, reading less saves time but not money.
