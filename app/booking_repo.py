"""Persistence for runway slot reservation books.

A reservation book freezes one runway's feasible schedule (fixed flights,
closure windows, rolling capacity) and the wake matrix at creation time.
Reservations are additional flights solved against the fixed schedule plus
all *active* (non-canceled) reservations with the slots/insert insertion
algorithm. Canceling a reservation flips it to ``canceled`` so its slot is
released immediately; the row is retained so a repeated cancel still answers
``204`` (idempotent) instead of ``404``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .separation import Capacity, ClosureWindow, Flight
from .timeutils import ensure_utc, to_epoch_seconds


def new_id() -> str:
    return uuid4().hex


def _iso(dt: datetime) -> str:
    return ensure_utc(dt).isoformat()


def _dt(stored: str) -> datetime:
    return datetime.fromisoformat(stored)


def _matrix_json(matrix: dict[tuple[str, str], int]) -> str:
    return json.dumps(
        {f"{leader}->{follower}": seconds for (leader, follower), seconds in matrix.items()},
        sort_keys=True,
    )


def _matrix_nested(matrix_json: str) -> dict[str, dict[str, int]]:
    nested: dict[str, dict[str, int]] = {}
    for pair, seconds in json.loads(matrix_json).items():
        leader, follower = pair.split("->", 1)
        nested.setdefault(leader, {})[follower] = seconds
    return nested


def save_book(
    conn: sqlite3.Connection,
    *,
    runway: str,
    flights: list[Flight],
    windows: list[ClosureWindow],
    capacity: Capacity | None,
    matrix: dict[tuple[str, str], int],
) -> tuple[str, datetime]:
    """Freeze a freshly validated feasible schedule into a new book."""
    book_id = new_id()
    created_at = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO reservation_books (book_id, runway, created_at, matrix_json,"
        " window_seconds, max_departures) VALUES (?, ?, ?, ?, ?, ?)",
        (
            book_id,
            runway,
            created_at.isoformat(),
            _matrix_json(matrix),
            capacity.window_seconds if capacity is not None else None,
            capacity.max_departures if capacity is not None else None,
        ),
    )
    for position, flight in enumerate(flights, start=1):
        conn.execute(
            "INSERT INTO reservation_flights (book_id, position, flight_no,"
            " scheduled_off, wake) VALUES (?, ?, ?, ?, ?)",
            (
                book_id,
                position,
                flight.flight_no,
                _iso(flight.scheduled_off),
                flight.wake,
            ),
        )
    for index, window in enumerate(windows, start=1):
        conn.execute(
            "INSERT INTO reservation_closures (book_id, window_index, start_utc, end_utc)"
            " VALUES (?, ?, ?, ?)",
            (book_id, index, _iso(window.start), _iso(window.end)),
        )
    return book_id, created_at


def load_book(conn: sqlite3.Connection, book_id: str) -> dict | None:
    """Load a book with its fixed schedule and active reservations.

    Returned flights/reservations are domain objects (UTC-normalised), sorted
    by the stable ``(time, flight_no)`` key. ``None`` when the book is
    unknown.
    """
    row = conn.execute(
        "SELECT book_id, runway, created_at, matrix_json, window_seconds,"
        " max_departures FROM reservation_books WHERE book_id = ?",
        (book_id,),
    ).fetchone()
    if row is None:
        return None

    fixed = [
        Flight(
            flight_no=fr["flight_no"],
            scheduled_off=_dt(fr["scheduled_off"]),
            wake=fr["wake"],
            runway=row["runway"],
        )
        for fr in conn.execute(
            "SELECT flight_no, scheduled_off, wake FROM reservation_flights"
            " WHERE book_id = ? ORDER BY position",
            (book_id,),
        )
    ]
    windows = [
        ClosureWindow(start=_dt(cr["start_utc"]), end=_dt(cr["end_utc"]))
        for cr in conn.execute(
            "SELECT start_utc, end_utc FROM reservation_closures"
            " WHERE book_id = ? ORDER BY window_index",
            (book_id,),
        )
    ]
    capacity = (
        Capacity(
            window_seconds=row["window_seconds"],
            max_departures=row["max_departures"],
        )
        if row["window_seconds"] is not None
        else None
    )
    reservations = [
        {
            "reservation_id": rr["reservation_id"],
            "flight_no": rr["flight_no"],
            "wake": rr["wake"],
            "earliest_off": _dt(rr["earliest_off"]),
            "latest_off": _dt(rr["latest_off"]),
            "assigned_off": _dt(rr["assigned_off"]),
            "created_at": _dt(rr["created_at"]),
        }
        for rr in conn.execute(
            "SELECT reservation_id, flight_no, wake, earliest_off, latest_off,"
            " assigned_off, created_at FROM reservations"
            " WHERE book_id = ? AND status = 'active' ORDER BY id",
            (book_id,),
        )
    ]
    return {
        "book_id": row["book_id"],
        "runway": row["runway"],
        "created_at": _dt(row["created_at"]),
        "matrix": _matrix_nested(row["matrix_json"]),
        "matrix_flat": {
            tuple(pair.split("->", 1)): seconds
            for pair, seconds in json.loads(row["matrix_json"]).items()
        },
        "capacity": capacity,
        "windows": windows,
        "fixed": fixed,
        "reservations": reservations,
    }


def insert_reservation(
    conn: sqlite3.Connection,
    *,
    book_id: str,
    flight_no: str,
    wake: str,
    earliest_off: datetime,
    latest_off: datetime,
    assigned_off: datetime,
) -> str:
    """Insert one active reservation row (the caller already solved the slot)."""
    reservation_id = new_id()
    conn.execute(
        "INSERT INTO reservations (reservation_id, book_id, flight_no, wake,"
        " earliest_off, latest_off, assigned_off, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
        (
            reservation_id,
            book_id,
            flight_no,
            wake,
            _iso(earliest_off),
            _iso(latest_off),
            _iso(assigned_off),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return reservation_id


def get_reservation_status(
    conn: sqlite3.Connection, reservation_id: str, book_id: str | None = None
) -> str | None:
    """``'active'`` / ``'canceled'`` for an existing reservation, else ``None``.

    When ``book_id`` is given, a reservation that belongs to another book is
    treated as unknown.
    """
    if book_id is None:
        row = conn.execute(
            "SELECT status FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT status FROM reservations WHERE reservation_id = ? AND book_id = ?",
            (reservation_id, book_id),
        ).fetchone()
    return None if row is None else row["status"]


def cancel_reservation(conn: sqlite3.Connection, reservation_id: str) -> None:
    """Release the slot immediately (idempotent)."""
    conn.execute(
        "UPDATE reservations SET status = 'canceled',"
        " canceled_at = ? WHERE reservation_id = ? AND status = 'active'",
        (datetime.now(timezone.utc).isoformat(), reservation_id),
    )


def current_flights(book: dict) -> list[Flight]:
    """Fixed schedule plus active reservations as one stable-sorted sequence."""
    flights = list(book["fixed"])
    flights.extend(
        Flight(
            flight_no=r["flight_no"],
            scheduled_off=r["assigned_off"],
            wake=r["wake"],
            runway=book["runway"],
        )
        for r in book["reservations"]
    )
    return sorted(flights, key=lambda f: (to_epoch_seconds(f.scheduled_off), f.flight_no))
