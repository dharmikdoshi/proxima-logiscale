import os
import shutil
import time
from pathlib import Path


def remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def replace_with_retry(tmp: Path, final: Path, attempts: int = 6) -> None:
    """Rename tmp to final. On windows the antivirus or search indexer sometimes grabs a file
    I just wrote, and the rename fails with 'access denied'. It lets go within a second, so
    this is the one place where a retry actually makes sense."""
    for attempt in range(1, attempts + 1):
        try:
            os.replace(tmp, final)
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(0.25 * attempt)
