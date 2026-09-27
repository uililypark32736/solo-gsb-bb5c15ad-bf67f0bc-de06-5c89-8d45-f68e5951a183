"""Reservation-book service: baseline validation and reservation solving.

Book creation reuses the slots/insert baseline precheck (the frozen schedule
must already satisfy wake/closure/capacity constraints). Reservation solving
reuses the slots/insert insertion engine with the *active* reservations
folded into the existing schedule, so every new reservation is placed at the
earliest feasible whole-second UTC slot against everything committed so far.
"""

from __future__ import annotations

from datetime import datetime

from .booking_repo import current_flights
from .schemas import BookingCreateRequest, ReservationCreateRequest
from .separation import (
    Capacity,
    ClosureWindow,
    Flight,
    SlotInsertResult,
    build_matrix,
    find_capacity_violations,
    find_closure_conflicts,
    find_insert_slot,
    find_violations,
    sort_flights,
    sort_windows,
)
from .timeutils import ensure_utc


class BookingBaselineError(Exception):
    """冻结的现有计划本身违反尾流间隔、关闭窗口或滚动容量约束（API 层转为 422）。"""


def _baseline_error_message(
    violations: list,
    conflicts: list,
    capacity_violations: list,
) -> str:
    parts: list[str] = []
    if violations:
        first = violations[0]
        parts.append(
            f"尾流间隔违规 {len(violations)} 起"
            f"（首个：{first.leader_no}→{first.follower_no} 要求 "
            f"{first.required_seconds} 秒、实际 {first.actual_seconds} 秒）"
        )
    if conflicts:
        first = conflicts[0]
        parts.append(
            f"关闭窗口冲突 {len(conflicts)} 起"
            f"（首个：{first.flight_no} 计划于 "
            f"{first.scheduled_off.isoformat()} 落入窗口 "
            f"[{first.window_start.isoformat()}, {first.window_end.isoformat()})）"
        )
    if capacity_violations:
        first = capacity_violations[0]
        parts.append(
            f"滚动容量超限 {len(capacity_violations)} 起"
            f"（首个：{first.flight_no} 于窗口 "
            f"[{first.anchor_off.isoformat()}, {first.window_end.isoformat()}) 内"
            f"超出 {first.window_seconds} 秒/{first.max_departures} 架限制）"
        )
    return "冻结计划违反约束，无法创建预约簿：" + "；".join(parts)


def build_book_payload(
    req: BookingCreateRequest,
) -> tuple[
    str,
    list[Flight],
    list[ClosureWindow],
    Capacity | None,
    dict[tuple[str, str], int],
]:
    """Normalise a book-creation request and verify the frozen schedule is feasible.

    Raises :class:`BookingBaselineError` (API answers 422) when the existing
    flights already violate wake/closure/capacity constraints.
    """
    rw = req.runway
    flights = sort_flights(
        [
            Flight(
                flight_no=f.flight_no,
                scheduled_off=ensure_utc(f.scheduled_off),
                wake=f.wake.value,
                runway=rw.runway,
            )
            for f in rw.flights
        ]
    )
    windows = sort_windows(
        [
            ClosureWindow(start=ensure_utc(w.start), end=ensure_utc(w.end))
            for w in rw.closures
        ]
    )
    capacity = (
        Capacity(
            window_seconds=rw.capacity.window_seconds,
            max_departures=rw.capacity.max_departures,
        )
        if rw.capacity is not None
        else None
    )
    matrix = (
        build_matrix(
            {(e.leader.value, e.follower.value): e.seconds for e in req.matrix}
        )
        if req.matrix is not None
        else build_matrix(None)
    )

    violations = find_violations(flights, matrix)
    conflicts = find_closure_conflicts(flights, windows)
    capacity_violations = find_capacity_violations(flights, capacity)
    if violations or conflicts or capacity_violations:
        raise BookingBaselineError(
            _baseline_error_message(violations, conflicts, capacity_violations)
        )
    return rw.runway, flights, windows, capacity, matrix


def solve_reservation(
    book: dict,
    req: ReservationCreateRequest,
) -> SlotInsertResult:
    """Find the earliest feasible slot against the frozen plan + active reservations."""
    existing = current_flights(book)
    return find_insert_slot(
        existing,
        candidate_flight_no=req.flight_no,
        candidate_wake=req.wake.value,
        earliest_off=ensure_utc(req.earliest_off),
        latest_off=ensure_utc(req.latest_off),
        matrix=book["matrix_flat"],
        windows=book["windows"],
        capacity=book["capacity"],
    )


def reservation_window_utc(req: ReservationCreateRequest) -> tuple[datetime, datetime]:
    return ensure_utc(req.earliest_off), ensure_utc(req.latest_off)
