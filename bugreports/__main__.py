import argparse
import sys
from pathlib import Path

from .ingest import BadBatch, ingest


def main() -> int:
    parser = argparse.ArgumentParser(prog="bugreports", description="Load report batches into parquet tables")
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("ingest", help="load one batch file (csv or gzipped csv, any file name)")
    load.add_argument("file", type=Path)
    load.add_argument("--data-root", type=Path, default=Path("data/bugreports"))
    load.add_argument("--memory", default="3GB", help="memory cap for duckdb, default 3GB")
    args = parser.parse_args()

    try:
        ingest(args.file, args.data_root, memory_limit=args.memory)
    except BadBatch as err:
        print(f"batch rejected: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
