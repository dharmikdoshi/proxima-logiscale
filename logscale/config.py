from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# order matters, the first ones get the most traffic (see the pow() trick in generate.py)
MODELS = ["DX-100", "DX-200", "KR-10", "KR-20", "VM-1", "DX-300",
          "KR-30", "VM-2", "HT-5", "HT-7", "VM-3", "HT-9"]
OS_VERSIONS = ["win11", "win10", "macos15", "macos14", "android14", "ios17", "ubuntu22", "macos13"]
FIRMWARES = ["2.4.1", "2.4.0", "2.5.0", "2.3.0", "3.0.1"]
COUNTRIES = ["US", "IN", "DE", "GB", "FR", "JP", "BR", "CA", "AU", "MX", "ES", "IT", "NL", "SE", "SG"]
CONNECTIONS = ["bluetooth", "dongle", "usb"]
EVENT_TYPES = ["heartbeat", "connect", "sync", "update", "pair", "disconnect"]


@dataclass(frozen=True)
class Config:
    rows: int = 10_000_000
    days: int = 90            # the window. always 90 here, it only changes in tests
    per_day: int = 0          # set when the size was given per day, only for the folder name
    seed: int = 7
    start: date = date(2026, 6, 1)
    devices: int = 500_000
    error_codes: int = 200
    # the normal fail rate is not one fixed number. it moves day to day and some models are
    # worse than others, but it never goes above the cap. only the bad day below does
    fail_rate_low: float = 0.02
    fail_rate_cap: float = 0.15

    # one bad day for one model + os combo, so the questions have something real to find
    spike_day: int = 60
    spike_model: str = "KR-20"
    spike_os: str = "win11"
    spike_fail_rate: float = 0.40
    spike_code: int = 1042

    data_root: Path = Path("data")

    @property
    def rows_per_day(self) -> int:
        return self.rows // self.days

    @property
    def label(self) -> str:
        # "10M" when the total was given, "5M-per-day" when the daily size was given
        name = _short(self.per_day) + "-per-day" if self.per_day else _short(self.rows)
        return name if self.days == 90 else f"{name}-{self.days}d"

    @property
    def root(self) -> Path:
        # each size gets its own folder, so the 10M and 100M runs can live side by side
        return self.data_root / self.label

    def day_date(self, day: int) -> date:
        return self.start + timedelta(days=day)

    def rows_present(self) -> int:
        # a per-day dataset may only have a few of its 90 days made, count what is really there
        return self.rows_per_day * len(list((self.root / "by_day").glob("day=*")))

    def window(self, days: int) -> tuple[date, date]:
        # the last N days ending on the bad day. all my test questions look at this stretch
        return self.day_date(max(0, self.spike_day - days + 1)), self.day_date(self.spike_day)

    def window_sql(self, days: int) -> str:
        first, last = self.window(days)
        return f"day BETWEEN DATE '{first}' AND DATE '{last}'"


def _short(n: int) -> str:
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= size and n % size == 0:
            return f"{n // size}{suffix}"
    return str(n)


def parse_rows(text: str) -> int:
    """'10M' -> 10000000. Accepts K, M, B or a plain number."""
    text = text.strip().upper().replace("_", "")
    scale = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(text[-1:], 1)
    number = text[:-1] if scale > 1 else text
    try:
        rows = int(float(number) * scale)
    except ValueError:
        raise ValueError(f"cannot read row count '{text}', try something like 10M") from None
    if rows <= 0:
        raise ValueError("row count has to be more than zero")
    return rows
