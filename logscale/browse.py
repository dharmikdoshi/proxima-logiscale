import duckdb

from .config import Config
from .layouts import ERROR_CODES_FILE, LAYOUTS, ROLLUPS, rollup_file


def make_browse_file(cfg: Config):
    """A tiny .duckdb file with no data in it, only views that point at the parquet folders.
    Open it in TablePlus or DBeaver and every layout shows up as a normal table."""
    path = cfg.root / "browse.duckdb"
    con = duckdb.connect(str(path))
    made = []
    for name, pattern in LAYOUTS.items():
        if not list(cfg.root.glob(pattern)):
            continue
        # full path here, the viewer app is not going to be started from this folder
        files = (cfg.root / pattern).resolve().as_posix()
        reader = (f"read_csv('{files}')" if name == "csv"
                  else f"read_parquet('{files}', hive_partitioning = true)")
        con.execute(f"CREATE OR REPLACE VIEW events_{name} AS SELECT * FROM {reader}")
        made.append(f"events_{name}")

    small_tables = {name: rollup_file(cfg, name) for name in ROLLUPS}
    small_tables["error_codes"] = cfg.root / ERROR_CODES_FILE
    for name, file in small_tables.items():
        if file.exists():
            con.execute(f"""CREATE OR REPLACE VIEW {name} AS
                            SELECT * FROM read_parquet('{file.resolve().as_posix()}')""")
            made.append(name)

    runs = (cfg.data_root / "runs.jsonl").resolve()
    if runs.exists():
        con.execute(f"CREATE OR REPLACE VIEW runs AS SELECT * FROM read_json('{runs.as_posix()}')")
        made.append("runs")
    con.close()
    return path, made
