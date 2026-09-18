import re
from datetime import date

from .config import Config


def parse_date(text: str) -> date:
    """Only YYYY-MM-DD. Something like 11-06-2026 means two different days in two countries,
    so I would rather refuse it than guess."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"'{text}' is not in YYYY-MM-DD format, for example 2026-06-11")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"'{text}' is not a real calendar date") from None


def day_range(cfg: Config, on: date | None, start: date | None, end: date | None) -> range | None:
    """Turn the date flags into day numbers (day 0 is cfg.start). None means every day."""
    if on and (start or end):
        raise ValueError("use --date for one day, or --from-date / --to-date for a range, not both")
    if not (on or start or end):
        return None

    first_day, last_day = cfg.start, cfg.day_date(cfg.days - 1)
    start, end = on or start or first_day, on or end or last_day
    for d in (start, end):
        if not first_day <= d <= last_day:
            raise ValueError(f"{d} is outside this dataset, it covers {first_day} to {last_day}")
    if start > end:
        raise ValueError(f"--from-date {start} is after --to-date {end}")
    return range((start - first_day).days, (end - first_day).days + 1)
