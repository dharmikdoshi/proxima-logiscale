from pathlib import Path

import duckdb


def connect(tmp_dir: Path, memory_limit: str = "3GB"):
    # my laptop has 8 GB, so cap duckdb at 3 and let it use disk when it needs more
    tmp_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{memory_limit}'")
    con.execute(f"SET temp_directory = '{tmp_dir.as_posix()}'")
    return con
