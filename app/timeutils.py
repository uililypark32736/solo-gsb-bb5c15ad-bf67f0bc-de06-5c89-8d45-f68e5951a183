"""Time helpers: all instants are normalised to timezone-aware UTC."""

from __future__ import annotations

from calendar import timegm
from datetime import datetime, timezone


def to_epoch_seconds(dt: datetime) -> int:
    """Whole POSIX seconds for an aware datetime (no float drift)."""
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return timegm(dt.astimezone(timezone.utc).utctimetuple())


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc)
