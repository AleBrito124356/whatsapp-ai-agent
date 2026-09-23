"""Injectable clocks.

Everything time-dependent (which days and hours can be booked, the 24-hour
customer-service window, timestamps in SQLite) reads the time through a
``Clock``: a zero-argument callable returning a timezone-aware ``datetime``.
Production uses :func:`system_clock`; tests and the chat simulator use
:class:`FrozenClock` so transcripts are reproducible.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

Clock = Callable[[], datetime]

UTC = timezone.utc


def system_clock() -> datetime:
    return datetime.now(UTC)


class FrozenClock:
    """A clock that only moves when told to (``advance``/``set``)."""

    def __init__(self, when: datetime):
        if when.tzinfo is None:
            raise ValueError("FrozenClock needs a timezone-aware datetime")
        self._now = when

    def __call__(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("FrozenClock needs a timezone-aware datetime")
        self._now = when

    def advance(self, delta: timedelta | None = None, **kwargs) -> datetime:
        self._now = self._now + (delta if delta is not None else timedelta(**kwargs))
        return self._now


def parse_when(value: str, tz_name: str) -> datetime:
    """Parse an ISO datetime; naive values are interpreted in ``tz_name``."""
    when = datetime.fromisoformat(value)
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(tz_name))
    return when


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([mhd])\s*$", re.IGNORECASE)


def parse_duration(value: str) -> timedelta:
    """'90m', '25h', '2d' -> timedelta. Raises ValueError otherwise."""
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"not a duration: {value!r} (use e.g. 30m, 25h, 2d)")
    amount, unit = int(match.group(1)), match.group(2).lower()
    return {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]


def to_db(when: datetime) -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS', the same format as SQLite's datetime('now')."""
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def from_db(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
