"""Persistence helpers for batches, flights, violations and trial items."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .joint import JointBlocker, JointTrialItem, JointTrialResult, MutexGroup
from .separation import (
    Capacity,
    CapacityViolation,
    ClosureConflict,
    ClosureWindow,
    Flight,
    SwapRunwayResult,
    TrialRunwayResult,
    Violation,
    delay_stats,
)
from .timeutils import ensure_utc, to_epoch_seconds


def new_batch_id() -> str:
    return uuid4().hex


def _iso(dt: datetime) -> str:
    return ensure_utc(dt).isoformat()


def _dt(stored: str) -> datetime:
    """Parse a stored ISO string back to a datetime so the batch endpoint
    serialises it exactly like the POST responses (UTC, ``Z`` suffix)."""
    return datetime.fromisoformat(stored)


def save_batch(
    conn: sqlite3.Connection,
    *,
    mode: str,
    matrix: dict[tuple[str, str], int],
    runway_payloads: dict[str, list[Flight]],
    closures_by_runway: dict[str, list[ClosureWindow]] | None = None,
    capacities_by_runway: dict[str, Capacity] | None = None,
    violations_by_runway: dict[str, list[Violation]] | None = None,
    closure_conflicts_by_runway: dict[str, list[ClosureConflict]] | None = None,
    capacity_violations_by_runway: dict[str, list[CapacityViolation]] | None = None,
    trial_by_runway: dict[str, TrialRunwayResult] | None = None,
    swap_by_runway: dict[str, SwapRunwayResult] | None = None,
    joint_groups: list[MutexGroup] | None = None,
    joint_result: JointTrialResult | None = None,
    locked_runways: set[str] | None = None,
) -> str:
    batch_id = new_batch_id()
    created_at = datetime.now(timezone.utc).isoformat()
    matrix_json = json.dumps(
        {f"{leader}->{follower}": seconds for (leader, follower), seconds in matrix.items()},
        sort_keys=True,
    )
    conn.execute(
        "INSERT INTO batches (batch_id, mode, created_at, matrix_json) VALUES (?, ?, ?, ?)",
        (batch_id, mode, created_at, matrix_json),
    )

    for runway, flights in runway_payloads.items():
        locked_provided = locked_runways is not None and runway in locked_runways
        for position, flight in enumerate(flights, start=1):
            conn.execute(
                "INSERT INTO flights (batch_id, runway, position, flight_no, scheduled_off, wake, locked)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    position,
                    flight.flight_no,
                    _iso(flight.scheduled_off),
                    flight.wake,
                    int(flight.locked) if locked_provided else None,
                ),
            )

    for runway, windows in (closures_by_runway or {}).items():
        for index, window in enumerate(windows, start=1):
            conn.execute(
                "INSERT INTO closures (batch_id, runway, window_index, start_utc, end_utc)"
                " VALUES (?, ?, ?, ?, ?)",
                (batch_id, runway, index, _iso(window.start), _iso(window.end)),
            )

    for runway, capacity in (capacities_by_runway or {}).items():
        conn.execute(
            "INSERT INTO capacities (batch_id, runway, window_seconds, max_departures)"
            " VALUES (?, ?, ?, ?)",
            (
                batch_id,
                runway,
                capacity.window_seconds,
                capacity.max_departures,
            ),
        )

    for runway, violations in (violations_by_runway or {}).items():
        for v in violations:
            conn.execute(
                "INSERT INTO violations (batch_id, runway, leader_position, follower_position,"
                " leader_no, follower_no, leader_wake, follower_wake, leader_time, follower_time,"
                " required_seconds, actual_seconds, shortfall_seconds)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    v.leader_position,
                    v.follower_position,
                    v.leader_no,
                    v.follower_no,
                    v.leader_wake,
                    v.follower_wake,
                    _iso(v.leader_time),
                    _iso(v.follower_time),
                    v.required_seconds,
                    v.actual_seconds,
                    v.shortfall_seconds,
                ),
            )

    for runway, conflicts in (closure_conflicts_by_runway or {}).items():
        for c in conflicts:
            conn.execute(
                "INSERT INTO closure_conflicts (batch_id, runway, position, flight_no, wake,"
                " scheduled_off, window_index, window_start, window_end)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    c.position,
                    c.flight_no,
                    c.wake,
                    _iso(c.scheduled_off),
                    c.window_index,
                    _iso(c.window_start),
                    _iso(c.window_end),
                ),
            )

    for runway, cap_violations in (capacity_violations_by_runway or {}).items():
        for cv in cap_violations:
            conn.execute(
                "INSERT INTO capacity_violations (batch_id, runway, position, flight_no, wake,"
                " scheduled_off, window_seconds, max_departures, departures_in_window,"
                " anchor_flight_no, anchor_position, window_start, window_end,"
                " earliest_compliant_off)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    cv.position,
                    cv.flight_no,
                    cv.wake,
                    _iso(cv.scheduled_off),
                    cv.window_seconds,
                    cv.max_departures,
                    cv.departures_in_window,
                    cv.anchor_flight_no,
                    cv.anchor_position,
                    _iso(cv.anchor_off),
                    _iso(cv.window_end),
                    _iso(cv.earliest_compliant_off),
                ),
            )

    for runway, result in (trial_by_runway or {}).items():
        for item in result.items:
            conn.execute(
                "INSERT INTO trial_items (batch_id, runway, position, flight_no, wake,"
                " scheduled_off, adjusted_off, delay_seconds, triggered_by,"
                " closure_hit, closure_window_start, closure_window_end, locked,"
                " capacity_hit, capacity_window_seconds, capacity_max_departures,"
                " capacity_anchor, capacity_window_start, capacity_window_end)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    item.position,
                    item.flight_no,
                    item.wake,
                    _iso(item.original_time),
                    _iso(item.adjusted_time),
                    item.delay_seconds,
                    item.triggered_by,
                    int(item.closure_hit),
                    _iso(item.closure_window.start) if item.closure_window else None,
                    _iso(item.closure_window.end) if item.closure_window else None,
                    int(item.locked),
                    int(item.capacity_hit),
                    item.capacity_window_seconds,
                    item.capacity_max_departures,
                    item.capacity_anchor,
                    _iso(item.capacity_window_start)
                    if item.capacity_window_start
                    else None,
                    _iso(item.capacity_window_end) if item.capacity_window_end else None,
                ),
            )
        blocked = result.blocked
        if blocked is not None:
            conn.execute(
                "INSERT INTO trial_blockers (batch_id, runway, position, flight_no, wake,"
                " scheduled_off, constraint_type, required_earliest_off, triggered_by,"
                " required_gap_seconds, closure_hit, closure_window_start, closure_window_end,"
                " capacity_hit, capacity_window_seconds, capacity_max_departures,"
                " capacity_anchor, capacity_window_start, capacity_window_end)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    runway,
                    blocked.position,
                    blocked.flight_no,
                    blocked.wake,
                    _iso(blocked.scheduled_off),
                    blocked.constraint,
                    _iso(blocked.required_earliest_off),
                    blocked.triggered_by,
                    blocked.required_gap_seconds,
                    int(blocked.closure_hit),
                    _iso(blocked.closure_window.start) if blocked.closure_window else None,
                    _iso(blocked.closure_window.end) if blocked.closure_window else None,
                    int(blocked.capacity_hit),
                    blocked.capacity_window_seconds,
                    blocked.capacity_max_departures,
                    blocked.capacity_anchor,
                    _iso(blocked.capacity_window_start)
                    if blocked.capacity_window_start
                    else None,
                    _iso(blocked.capacity_window_end)
                    if blocked.capacity_window_end
                    else None,
                ),
            )

    for runway, result in (swap_by_runway or {}).items():
        baseline_total, baseline_max = delay_stats(result.baseline)
        baseline_delayed = sum(
            1 for item in result.baseline.items if item.delay_seconds > 0
        )
        chosen = result.chosen_swap
        conn.execute(
            "INSERT INTO swap_results (batch_id, runway, baseline_order_json,"
            " swap_position, swap_first_flight_no, swap_second_flight_no,"
            " baseline_delayed_count, baseline_total_delay_seconds,"
            " baseline_max_delay_seconds, saved_seconds)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                runway,
                json.dumps([f.flight_no for f in result.baseline_order]),
                chosen.position if chosen is not None else None,
                chosen.first_flight_no if chosen is not None else None,
                chosen.second_flight_no if chosen is not None else None,
                baseline_delayed,
                baseline_total,
                baseline_max,
                result.saved_seconds,
            ),
        )

    if joint_groups:
        for index, group in enumerate(joint_groups, start=1):
            conn.execute(
                "INSERT INTO mutex_groups (batch_id, group_index, name,"
                " runways_json, min_interval_seconds) VALUES (?, ?, ?, ?, ?)",
                (
                    batch_id,
                    index,
                    group.name,
                    json.dumps(list(group.runways)),
                    group.min_interval_seconds,
                ),
            )

    if joint_result is not None:
        _save_joint_items(conn, batch_id, joint_result)

    return batch_id


def _save_joint_items(
    conn: sqlite3.Connection, batch_id: str, result: JointTrialResult
) -> None:
    """Persist the joint feasible prefix and (if any) the first blocker."""
    for item in result.items:
        conn.execute(
            "INSERT INTO trial_items (batch_id, runway, position, runway_position,"
            " flight_no, wake, scheduled_off, adjusted_off, delay_seconds,"
            " constraint_type, triggered_by, closure_hit, closure_window_start,"
            " closure_window_end, locked, capacity_hit, capacity_window_seconds,"
            " capacity_max_departures, capacity_anchor, capacity_window_start,"
            " capacity_window_end, mutex_group_hit, mutex_group_name,"
            " mutex_group_anchor, mutex_group_min_interval_seconds,"
            " mutex_group_window_start, mutex_group_window_end)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                item.runway,
                item.position,
                _runway_position(result, item),
                item.flight_no,
                item.wake,
                _iso(item.original_time),
                _iso(item.adjusted_time),
                item.delay_seconds,
                item.constraint,
                item.triggered_by,
                int(item.closure_hit),
                _iso(item.closure_window.start) if item.closure_window else None,
                _iso(item.closure_window.end) if item.closure_window else None,
                int(item.locked),
                int(item.capacity_hit),
                item.capacity_window_seconds,
                item.capacity_max_departures,
                item.capacity_anchor,
                _iso(item.capacity_window_start)
                if item.capacity_window_start
                else None,
                _iso(item.capacity_window_end) if item.capacity_window_end else None,
                int(item.mutex_group_hit),
                item.mutex_group_name,
                item.mutex_group_anchor,
                item.mutex_group_min_interval_seconds,
                _iso(item.mutex_group_window_start)
                if item.mutex_group_window_start
                else None,
                _iso(item.mutex_group_window_end)
                if item.mutex_group_window_end
                else None,
            ),
        )
    blocked = result.blocked
    if blocked is not None:
        conn.execute(
            "INSERT INTO trial_blockers (batch_id, runway, position, flight_no, wake,"
            " scheduled_off, constraint_type, required_earliest_off, triggered_by,"
            " required_gap_seconds, closure_hit, closure_window_start,"
            " closure_window_end, capacity_hit, capacity_window_seconds,"
            " capacity_max_departures, capacity_anchor, capacity_window_start,"
            " capacity_window_end, mutex_group_hit, mutex_group_name,"
            " mutex_group_anchor, mutex_group_min_interval_seconds,"
            " mutex_group_window_start, mutex_group_window_end, required_times_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                blocked.runway,
                blocked.position,
                blocked.flight_no,
                blocked.wake,
                _iso(blocked.scheduled_off),
                blocked.constraint,
                _iso(blocked.required_earliest_off),
                blocked.triggered_by,
                blocked.required_gap_seconds,
                int(blocked.closure_hit),
                _iso(blocked.closure_window.start) if blocked.closure_window else None,
                _iso(blocked.closure_window.end) if blocked.closure_window else None,
                int(blocked.capacity_hit),
                blocked.capacity_window_seconds,
                blocked.capacity_max_departures,
                blocked.capacity_anchor,
                _iso(blocked.capacity_window_start)
                if blocked.capacity_window_start
                else None,
                _iso(blocked.capacity_window_end)
                if blocked.capacity_window_end
                else None,
                int(blocked.mutex_group_hit),
                blocked.mutex_group_name,
                blocked.mutex_group_anchor,
                blocked.mutex_group_min_interval_seconds,
                _iso(blocked.mutex_group_window_start)
                if blocked.mutex_group_window_start
                else None,
                _iso(blocked.mutex_group_window_end)
                if blocked.mutex_group_window_end
                else None,
                _required_times_json(blocked),
            ),
        )


def _runway_position(result: JointTrialResult, item: JointTrialItem) -> int:
    """1-based stable position of ``item`` within its own runway."""
    seen = 0
    for runway, flight in result.order:
        if runway == item.runway:
            seen += 1
            if flight.flight_no == item.flight_no:
                return seen
    return seen


def _required_times_json(blocked: JointBlocker) -> str:
    """Serialise every constraint's required time for the blocked flight."""

    def payload(kind: str) -> dict | None:
        data = blocked.required_times.get(kind)
        if data is None:
            return None
        encoded = dict(data)
        if "window" in data:
            value = data["window"]
            if isinstance(value, ClosureWindow):
                encoded["window"] = {
                    "start": _iso(value.start),
                    "end": _iso(value.end),
                }
            else:
                start_t, end_t = value
                encoded["window"] = {
                    "start": _iso(
                        datetime.fromtimestamp(start_t, tz=timezone.utc)
                    ),
                    "end": _iso(datetime.fromtimestamp(end_t, tz=timezone.utc)),
                }
        if "required_off" in encoded:
            encoded["required_off"] = _iso(encoded["required_off"])
        return encoded

    return json.dumps({kind: payload(kind) for kind in (
        "wake",
        "closure",
        "capacity",
        "mutex_group",
    )})


def load_conflict_group_inputs(
    conn: sqlite3.Connection, batch_id: str
) -> dict | None:
    """Flights and validate-mode conflict detail for conflict grouping.

    Returns ``None`` when the batch does not exist; otherwise a dict with
    the batch ``mode``/``created_at`` and per-runway domain lists (flights
    in stable order, wake violations, closure conflicts, capacity
    violations) as persisted for a validate batch.
    """
    row = conn.execute(
        "SELECT mode, created_at FROM batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        return None

    flights_by_runway: dict[str, list[Flight]] = {}
    for fr in conn.execute(
        "SELECT runway, flight_no, scheduled_off, wake FROM flights"
        " WHERE batch_id = ? ORDER BY runway, position",
        (batch_id,),
    ):
        flights_by_runway.setdefault(fr["runway"], []).append(
            Flight(
                flight_no=fr["flight_no"],
                scheduled_off=_dt(fr["scheduled_off"]),
                wake=fr["wake"],
                runway=fr["runway"],
            )
        )

    violations_by_runway: dict[str, list[Violation]] = {}
    for vr in conn.execute(
        "SELECT * FROM violations WHERE batch_id = ?"
        " ORDER BY runway, leader_position",
        (batch_id,),
    ):
        violations_by_runway.setdefault(vr["runway"], []).append(
            Violation(
                leader_no=vr["leader_no"],
                follower_no=vr["follower_no"],
                leader_wake=vr["leader_wake"],
                follower_wake=vr["follower_wake"],
                leader_time=_dt(vr["leader_time"]),
                follower_time=_dt(vr["follower_time"]),
                required_seconds=vr["required_seconds"],
                actual_seconds=vr["actual_seconds"],
                shortfall_seconds=vr["shortfall_seconds"],
                leader_position=vr["leader_position"],
                follower_position=vr["follower_position"],
            )
        )

    conflicts_by_runway: dict[str, list[ClosureConflict]] = {}
    for cr in conn.execute(
        "SELECT * FROM closure_conflicts WHERE batch_id = ?"
        " ORDER BY runway, position",
        (batch_id,),
    ):
        conflicts_by_runway.setdefault(cr["runway"], []).append(
            ClosureConflict(
                flight_no=cr["flight_no"],
                wake=cr["wake"],
                scheduled_off=_dt(cr["scheduled_off"]),
                position=cr["position"],
                window_index=cr["window_index"],
                window_start=_dt(cr["window_start"]),
                window_end=_dt(cr["window_end"]),
            )
        )

    capacity_by_runway: dict[str, list[CapacityViolation]] = {}
    for cr in conn.execute(
        "SELECT * FROM capacity_violations WHERE batch_id = ?"
        " ORDER BY runway, position",
        (batch_id,),
    ):
        capacity_by_runway.setdefault(cr["runway"], []).append(
            CapacityViolation(
                flight_no=cr["flight_no"],
                wake=cr["wake"],
                scheduled_off=_dt(cr["scheduled_off"]),
                position=cr["position"],
                window_seconds=cr["window_seconds"],
                max_departures=cr["max_departures"],
                anchor_flight_no=cr["anchor_flight_no"],
                anchor_position=cr["anchor_position"],
                anchor_off=_dt(cr["window_start"]),
                window_end=_dt(cr["window_end"]),
                departures_in_window=cr["departures_in_window"],
                earliest_compliant_off=_dt(cr["earliest_compliant_off"]),
            )
        )

    return {
        "mode": row["mode"],
        "created_at": row["created_at"],
        "flights_by_runway": flights_by_runway,
        "violations_by_runway": violations_by_runway,
        "closure_conflicts_by_runway": conflicts_by_runway,
        "capacity_violations_by_runway": capacity_by_runway,
    }


def load_batch(conn: sqlite3.Connection, batch_id: str) -> dict | None:
    """Reconstruct a stored batch in the same shape as the POST responses."""
    row = conn.execute(
        "SELECT batch_id, mode, created_at, matrix_json FROM batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        return None

    flat = {
        tuple(pair.split("->", 1)): seconds
        for pair, seconds in json.loads(row["matrix_json"]).items()
    }
    matrix: dict[str, dict[str, int]] = {}
    for (leader, follower), seconds in flat.items():
        matrix.setdefault(leader, {})[follower] = seconds

    flight_rows = conn.execute(
        "SELECT runway, position, flight_no, scheduled_off, wake, locked FROM flights"
        " WHERE batch_id = ? ORDER BY runway, position",
        (batch_id,),
    ).fetchall()
    order_by_runway: dict[str, list[str]] = {}
    # runway -> list of locked-flight echo dicts (in stable order); the key is
    # present only when that runway's request explicitly carried ``locked``.
    locked_by_runway: dict[str, list[dict]] = {}
    for fr in flight_rows:
        order_by_runway.setdefault(fr["runway"], []).append(fr["flight_no"])
        if fr["locked"] is not None:
            locked_by_runway.setdefault(fr["runway"], [])
            if fr["locked"]:
                locked_by_runway[fr["runway"]].append(
                    {
                        "position": fr["position"],
                        "flight_no": fr["flight_no"],
                        "scheduled_off": _dt(fr["scheduled_off"]),
                        "wake": fr["wake"],
                    }
                )
    batch_has_locks = bool(locked_by_runway)

    closure_rows = conn.execute(
        "SELECT runway, window_index, start_utc, end_utc FROM closures"
        " WHERE batch_id = ? ORDER BY runway, window_index",
        (batch_id,),
    ).fetchall()
    closures_by_runway: dict[str, list[dict]] = {}
    for cr in closure_rows:
        closures_by_runway.setdefault(cr["runway"], []).append(
            {"start": _dt(cr["start_utc"]), "end": _dt(cr["end_utc"])}
        )

    capacity_rows = conn.execute(
        "SELECT runway, window_seconds, max_departures FROM capacities"
        " WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    capacities_by_runway: dict[str, dict] = {}
    for cr in capacity_rows:
        capacities_by_runway[cr["runway"]] = {
            "window_seconds": cr["window_seconds"],
            "max_departures": cr["max_departures"],
        }
    batch_has_capacity = bool(capacities_by_runway)

    if row["mode"] == "validate":
        vrows = conn.execute(
            "SELECT * FROM violations WHERE batch_id = ? ORDER BY runway, leader_position",
            (batch_id,),
        ).fetchall()
        runways = []
        violations_by_runway: dict[str, list[dict]] = {}
        for vr in vrows:
            violations_by_runway.setdefault(vr["runway"], []).append(
                {
                    "leader": vr["leader_no"],
                    "follower": vr["follower_no"],
                    "leader_wake": vr["leader_wake"],
                    "follower_wake": vr["follower_wake"],
                    "leader_scheduled_off": _dt(vr["leader_time"]),
                    "follower_scheduled_off": _dt(vr["follower_time"]),
                    "required_seconds": vr["required_seconds"],
                    "actual_seconds": vr["actual_seconds"],
                    "shortfall_seconds": vr["shortfall_seconds"],
                    "leader_position": vr["leader_position"],
                    "follower_position": vr["follower_position"],
                }
            )
        crows = conn.execute(
            "SELECT * FROM closure_conflicts WHERE batch_id = ? ORDER BY runway, position",
            (batch_id,),
        ).fetchall()
        conflicts_by_runway: dict[str, list[dict]] = {}
        for cr in crows:
            conflicts_by_runway.setdefault(cr["runway"], []).append(
                {
                    "flight_no": cr["flight_no"],
                    "wake": cr["wake"],
                    "scheduled_off": _dt(cr["scheduled_off"]),
                    "position": cr["position"],
                    "window_index": cr["window_index"],
                    "window_start": _dt(cr["window_start"]),
                    "window_end": _dt(cr["window_end"]),
                }
            )
        cap_vrows = conn.execute(
            "SELECT * FROM capacity_violations WHERE batch_id = ?"
            " ORDER BY runway, position",
            (batch_id,),
        ).fetchall()
        cap_violations_by_runway: dict[str, list[dict]] = {}
        for cr in cap_vrows:
            cap_violations_by_runway.setdefault(cr["runway"], []).append(
                {
                    "flight_no": cr["flight_no"],
                    "wake": cr["wake"],
                    "scheduled_off": _dt(cr["scheduled_off"]),
                    "position": cr["position"],
                    "window_seconds": cr["window_seconds"],
                    "max_departures": cr["max_departures"],
                    "departures_in_window": cr["departures_in_window"],
                    "anchor_flight_no": cr["anchor_flight_no"],
                    "anchor_position": cr["anchor_position"],
                    "window_start": _dt(cr["window_start"]),
                    "window_end": _dt(cr["window_end"]),
                    "earliest_compliant_off": _dt(cr["earliest_compliant_off"]),
                }
            )
        for runway in order_by_runway:
            violations = violations_by_runway.get(runway, [])
            conflicts = conflicts_by_runway.get(runway, [])
            report = {
                "runway": runway,
                "order": order_by_runway[runway],
                "total": len(order_by_runway[runway]),
                "closures": closures_by_runway.get(runway, []),
                "violation_count": len(violations),
                "violations": violations,
                "closure_conflict_count": len(conflicts),
                "closure_conflicts": conflicts,
            }
            if runway in capacities_by_runway:
                cap_violations = cap_violations_by_runway.get(runway, [])
                report["capacity"] = capacities_by_runway[runway]
                report["capacity_violation_count"] = len(cap_violations)
                report["capacity_violations"] = cap_violations
            if runway in locked_by_runway:
                report["locked_flights"] = locked_by_runway[runway]
            runways.append(report)
        result = {
            "batch_id": row["batch_id"],
            "mode": "validate",
            "created_at": row["created_at"],
            "matrix": matrix,
            "total_runways": len(runways),
            "total_flights": sum(len(o) for o in order_by_runway.values()),
            "total_violations": sum(len(v) for v in violations_by_runway.values()),
            "total_closure_conflicts": sum(
                len(c) for c in conflicts_by_runway.values()
            ),
            "runways": runways,
        }
        if batch_has_capacity:
            result["total_capacity_violations"] = sum(
                len(v) for v in cap_violations_by_runway.values()
            )
        if batch_has_locks:
            result["total_locked"] = sum(len(v) for v in locked_by_runway.values())
        return result

    if row["mode"] == "joint_trial":
        return _load_joint_batch(
            conn=conn,
            row=row,
            batch_id=batch_id,
            matrix=matrix,
            flight_rows=flight_rows,
            closures_by_runway=closures_by_runway,
            capacities_by_runway=capacities_by_runway,
            locked_by_runway=locked_by_runway,
            batch_has_locks=batch_has_locks,
        )

    # trial-style payloads: both "trial" and "best_adjacent_swap" persist
    # per-flight trial items (for the latter, in the optimised order).
    trows = conn.execute(
        "SELECT * FROM trial_items WHERE batch_id = ? ORDER BY runway, position",
        (batch_id,),
    ).fetchall()
    items_by_runway: dict[str, list[dict]] = {}
    for tr in trows:
        closure_window = None
        if tr["closure_window_start"] is not None:
            closure_window = {
                "start": _dt(tr["closure_window_start"]),
                "end": _dt(tr["closure_window_end"]),
            }
        item = {
            "position": tr["position"],
            "flight_no": tr["flight_no"],
            "wake": tr["wake"],
            "scheduled_off": _dt(tr["scheduled_off"]),
            "adjusted_off": _dt(tr["adjusted_off"]),
            "delay_seconds": tr["delay_seconds"],
            "triggered_by": tr["triggered_by"],
            "closure_hit": bool(tr["closure_hit"]),
            "closure_window": closure_window,
        }
        runway = tr["runway"]
        runway_has_capacity = runway in capacities_by_runway
        if runway_has_capacity:
            capacity_window = None
            if tr["capacity_window_start"] is not None:
                capacity_window = {
                    "start": _dt(tr["capacity_window_start"]),
                    "end": _dt(tr["capacity_window_end"]),
                }
            item["capacity_hit"] = bool(tr["capacity_hit"])
            item["capacity_window_seconds"] = tr["capacity_window_seconds"]
            item["capacity_max_departures"] = tr["capacity_max_departures"]
            item["capacity_anchor"] = tr["capacity_anchor"]
            item["capacity_window"] = capacity_window
        if runway in locked_by_runway:
            item["locked"] = bool(tr["locked"])
        items_by_runway.setdefault(runway, []).append(item)

    if row["mode"] == "best_adjacent_swap":
        srows = conn.execute(
            "SELECT * FROM swap_results WHERE batch_id = ?", (batch_id,)
        ).fetchall()
        swap_by_runway = {sr["runway"]: sr for sr in srows}
        runways = []
        for runway in order_by_runway:
            items = items_by_runway.get(runway, [])
            srow = swap_by_runway[runway]
            chosen_swap = None
            if srow["swap_position"] is not None:
                chosen_swap = {
                    "position": srow["swap_position"],
                    "first_flight_no": srow["swap_first_flight_no"],
                    "second_flight_no": srow["swap_second_flight_no"],
                }
            runway_report = {
                "runway": runway,
                "order": order_by_runway[runway],
                "baseline_order": json.loads(srow["baseline_order_json"]),
                "total": len(order_by_runway[runway]),
                "closures": closures_by_runway.get(runway, []),
                "baseline": {
                    "delayed_count": srow["baseline_delayed_count"],
                    "total_delay_seconds": srow["baseline_total_delay_seconds"],
                    "max_delay_seconds": srow["baseline_max_delay_seconds"],
                },
                "chosen_swap": chosen_swap,
                "saved_seconds": srow["saved_seconds"],
                "delayed_count": sum(1 for i in items if i["delay_seconds"] > 0),
                "total_delay_seconds": sum(i["delay_seconds"] for i in items),
                "max_delay_seconds": max(
                    (i["delay_seconds"] for i in items), default=0
                ),
                "items": items,
            }
            if runway in capacities_by_runway:
                runway_report["capacity"] = capacities_by_runway[runway]
            runways.append(runway_report)
        return {
            "batch_id": row["batch_id"],
            "mode": "best_adjacent_swap",
            "created_at": row["created_at"],
            "matrix": matrix,
            "total_runways": len(runways),
            "total_flights": sum(len(o) for o in order_by_runway.values()),
            "total_baseline_delay_seconds": sum(
                r["baseline"]["total_delay_seconds"] for r in runways
            ),
            "total_delay_seconds": sum(r["total_delay_seconds"] for r in runways),
            "total_saved_seconds": sum(r["saved_seconds"] for r in runways),
            "runways": runways,
        }

    # trial
    blockers_by_runway: dict[str, dict] = {}
    if batch_has_locks:
        brows = conn.execute(
            "SELECT * FROM trial_blockers WHERE batch_id = ?", (batch_id,)
        ).fetchall()
        for br in brows:
            closure_window = None
            if br["closure_window_start"] is not None:
                closure_window = {
                    "start": _dt(br["closure_window_start"]),
                    "end": _dt(br["closure_window_end"]),
                }
            blocked = {
                "flight_no": br["flight_no"],
                "wake": br["wake"],
                "scheduled_off": _dt(br["scheduled_off"]),
                "position": br["position"],
                "constraint": br["constraint_type"],
                "required_earliest_off": _dt(br["required_earliest_off"]),
                "triggered_by": br["triggered_by"],
                "required_gap_seconds": br["required_gap_seconds"],
                "closure_hit": bool(br["closure_hit"]),
                "closure_window": closure_window,
            }
            if br["runway"] in capacities_by_runway:
                capacity_window = None
                if br["capacity_window_start"] is not None:
                    capacity_window = {
                        "start": _dt(br["capacity_window_start"]),
                        "end": _dt(br["capacity_window_end"]),
                    }
                blocked["capacity_hit"] = bool(br["capacity_hit"])
                blocked["capacity_window_seconds"] = br["capacity_window_seconds"]
                blocked["capacity_max_departures"] = br["capacity_max_departures"]
                blocked["capacity_anchor"] = br["capacity_anchor"]
                blocked["capacity_window"] = capacity_window
            blockers_by_runway[br["runway"]] = blocked

    runways = []
    total_delayed = 0
    total_delay = 0
    for runway in order_by_runway:
        items = items_by_runway.get(runway, [])
        delayed = [i for i in items if i["delay_seconds"] > 0]
        rw_delay = sum(i["delay_seconds"] for i in items)
        max_delay = max((i["delay_seconds"] for i in items), default=0)
        total_delayed += len(delayed)
        total_delay += rw_delay
        runway_report = {
            "runway": runway,
            "order": order_by_runway[runway],
            "total": len(order_by_runway[runway]),
            "closures": closures_by_runway.get(runway, []),
            "delayed_count": len(delayed),
            "total_delay_seconds": rw_delay,
            "max_delay_seconds": max_delay,
            "items": items,
        }
        if runway in capacities_by_runway:
            runway_report["capacity"] = capacities_by_runway[runway]
        if runway in locked_by_runway:
            runway_report["locked_flights"] = locked_by_runway[runway]
            runway_report["blocked"] = blockers_by_runway.get(runway)
        runways.append(runway_report)
    result = {
        "batch_id": row["batch_id"],
        "mode": "trial",
        "created_at": row["created_at"],
        "matrix": matrix,
        "total_runways": len(runways),
        "total_flights": sum(len(o) for o in order_by_runway.values()),
        "total_delayed": total_delayed,
        "total_delay_seconds": total_delay,
        "runways": runways,
    }
    if batch_has_locks:
        result["total_locked"] = sum(len(v) for v in locked_by_runway.values())
    return result


def _load_joint_batch(
    *,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    batch_id: str,
    matrix: dict[str, dict[str, int]],
    flight_rows: list[sqlite3.Row],
    closures_by_runway: dict[str, list[dict]],
    capacities_by_runway: dict[str, dict],
    locked_by_runway: dict[str, list[dict]],
    batch_has_locks: bool,
) -> dict:
    """Reconstruct a joint_trial batch: global order, per-runway prefixes
    and (if any) the first locked-flight blocker with per-constraint times."""
    # Global stable order rebuilt from the stored plans using the same
    # integer-second key as the engine: (scheduled epoch seconds, flight_no).
    global_rows = sorted(
        flight_rows,
        key=lambda fr: (to_epoch_seconds(_dt(fr["scheduled_off"])), fr["flight_no"]),
    )
    order_by_runway: dict[str, list[str]] = {}
    for fr in flight_rows:
        order_by_runway.setdefault(fr["runway"], []).append(fr["flight_no"])

    group_rows = conn.execute(
        "SELECT group_index, name, runways_json, min_interval_seconds FROM mutex_groups"
        " WHERE batch_id = ? ORDER BY group_index",
        (batch_id,),
    ).fetchall()
    groups = [
        {
            "name": gr["name"],
            "runways": json.loads(gr["runways_json"]),
            "min_interval_seconds": gr["min_interval_seconds"],
        }
        for gr in group_rows
    ]
    group_of_runway = {
        runway: group["name"] for group in groups for runway in group["runways"]
    }

    trows = conn.execute(
        "SELECT * FROM trial_items WHERE batch_id = ? ORDER BY position",
        (batch_id,),
    ).fetchall()
    items: list[dict] = []
    for tr in trows:
        closure_window = None
        if tr["closure_window_start"] is not None:
            closure_window = {
                "start": _dt(tr["closure_window_start"]),
                "end": _dt(tr["closure_window_end"]),
            }
        item = {
            "position": tr["position"],
            "runway_position": tr["runway_position"],
            "runway": tr["runway"],
            "flight_no": tr["flight_no"],
            "wake": tr["wake"],
            "scheduled_off": _dt(tr["scheduled_off"]),
            "adjusted_off": _dt(tr["adjusted_off"]),
            "delay_seconds": tr["delay_seconds"],
            "constraint": tr["constraint_type"],
            "triggered_by": tr["triggered_by"],
            "closure_hit": bool(tr["closure_hit"]),
            "closure_window": closure_window,
        }
        runway = tr["runway"]
        if runway in capacities_by_runway:
            capacity_window = None
            if tr["capacity_window_start"] is not None:
                capacity_window = {
                    "start": _dt(tr["capacity_window_start"]),
                    "end": _dt(tr["capacity_window_end"]),
                }
            item["capacity_hit"] = bool(tr["capacity_hit"])
            item["capacity_window_seconds"] = tr["capacity_window_seconds"]
            item["capacity_max_departures"] = tr["capacity_max_departures"]
            item["capacity_anchor"] = tr["capacity_anchor"]
            item["capacity_window"] = capacity_window
        if runway in group_of_runway:
            group_window = None
            if tr["mutex_group_window_start"] is not None:
                group_window = {
                    "start": _dt(tr["mutex_group_window_start"]),
                    "end": _dt(tr["mutex_group_window_end"]),
                }
            item["mutex_group_hit"] = bool(tr["mutex_group_hit"])
            item["mutex_group"] = tr["mutex_group_name"]
            item["mutex_group_anchor"] = tr["mutex_group_anchor"]
            item["mutex_group_min_interval_seconds"] = tr[
                "mutex_group_min_interval_seconds"
            ]
            item["mutex_group_window"] = group_window
        if runway in locked_by_runway:
            item["locked"] = bool(tr["locked"])
        items.append(item)
    items_by_runway: dict[str, list[dict]] = {}
    for item in items:
        items_by_runway.setdefault(item["runway"], []).append(item)

    blocked_report: dict | None = None
    brow = conn.execute(
        "SELECT * FROM trial_blockers WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if brow is not None:
        closure_window = None
        if brow["closure_window_start"] is not None:
            closure_window = {
                "start": _dt(brow["closure_window_start"]),
                "end": _dt(brow["closure_window_end"]),
            }
        # runway position: 1-based stable position of the blocker within its
        # own runway's full planned order (later un-scheduled flights count).
        runway_position = 0
        for fr in global_rows:
            if fr["runway"] == brow["runway"]:
                runway_position += 1
                if fr["flight_no"] == brow["flight_no"]:
                    break
        blocked_report = {
            "position": brow["position"],
            "runway_position": runway_position,
            "runway": brow["runway"],
            "flight_no": brow["flight_no"],
            "wake": brow["wake"],
            "scheduled_off": _dt(brow["scheduled_off"]),
            "constraint": brow["constraint_type"],
            "required_earliest_off": _dt(brow["required_earliest_off"]),
            "triggered_by": brow["triggered_by"],
            "required_gap_seconds": brow["required_gap_seconds"],
            "closure_hit": bool(brow["closure_hit"]),
            "closure_window": closure_window,
            "required_times": _load_required_times(brow["required_times_json"]),
        }
        if brow["runway"] in capacities_by_runway:
            capacity_window = None
            if brow["capacity_window_start"] is not None:
                capacity_window = {
                    "start": _dt(brow["capacity_window_start"]),
                    "end": _dt(brow["capacity_window_end"]),
                }
            blocked_report["capacity_hit"] = bool(brow["capacity_hit"])
            blocked_report["capacity_window_seconds"] = brow[
                "capacity_window_seconds"
            ]
            blocked_report["capacity_max_departures"] = brow[
                "capacity_max_departures"
            ]
            blocked_report["capacity_anchor"] = brow["capacity_anchor"]
            blocked_report["capacity_window"] = capacity_window
        if brow["runway"] in group_of_runway:
            group_window = None
            if brow["mutex_group_window_start"] is not None:
                group_window = {
                    "start": _dt(brow["mutex_group_window_start"]),
                    "end": _dt(brow["mutex_group_window_end"]),
                }
            blocked_report["mutex_group_hit"] = bool(brow["mutex_group_hit"])
            blocked_report["mutex_group"] = brow["mutex_group_name"]
            blocked_report["mutex_group_anchor"] = brow["mutex_group_anchor"]
            blocked_report["mutex_group_min_interval_seconds"] = brow[
                "mutex_group_min_interval_seconds"
            ]
            blocked_report["mutex_group_window"] = group_window

    runways = []
    total_delayed = 0
    total_delay = 0
    max_delay = 0
    for runway in order_by_runway:
        rw_items = items_by_runway.get(runway, [])
        delayed = [i for i in rw_items if i["delay_seconds"] > 0]
        rw_delay = sum(i["delay_seconds"] for i in rw_items)
        rw_max = max((i["delay_seconds"] for i in rw_items), default=0)
        total_delayed += len(delayed)
        total_delay += rw_delay
        max_delay = max(max_delay, rw_max)
        runway_report = {
            "runway": runway,
            "total": len(order_by_runway[runway]),
            "closures": closures_by_runway.get(runway, []),
            "delayed_count": len(delayed),
            "total_delay_seconds": rw_delay,
            "max_delay_seconds": rw_max,
            "items": rw_items,
        }
        if runway in capacities_by_runway:
            runway_report["capacity"] = capacities_by_runway[runway]
        if runway in group_of_runway:
            runway_report["mutex_group"] = group_of_runway[runway]
        if runway in locked_by_runway:
            runway_report["locked_flights"] = locked_by_runway[runway]
            runway_report["blocked"] = (
                blocked_report
                if blocked_report is not None
                and blocked_report["runway"] == runway
                else None
            )
        runways.append(runway_report)

    result = {
        "batch_id": row["batch_id"],
        "mode": "joint_trial",
        "created_at": row["created_at"],
        "matrix": matrix,
        "total_runways": len(order_by_runway),
        "total_flights": sum(len(o) for o in order_by_runway.values()),
        "groups": groups,
        "order": [
            {"runway": fr["runway"], "flight_no": fr["flight_no"]}
            for fr in global_rows
        ],
        "total_delayed": total_delayed,
        "total_delay_seconds": total_delay,
        "max_delay_seconds": max_delay,
        "runways": runways,
    }
    if batch_has_locks:
        result["total_locked"] = sum(len(v) for v in locked_by_runway.values())
    return result


def _load_required_times(stored: str | None) -> dict:
    """Parse the blocker's per-constraint required-times JSON, keeping the
    fixed four-key contract (inapplicable constraints stay ``null``)."""
    data = json.loads(stored) if stored else {}

    def normalised(kind: str) -> dict | None:
        payload = data.get(kind)
        if payload is None:
            return None
        encoded = dict(payload)
        encoded["required_off"] = _dt(payload["required_off"])
        if "window" in payload and payload["window"] is not None:
            encoded["window"] = {
                "start": _dt(payload["window"]["start"]),
                "end": _dt(payload["window"]["end"]),
            }
        return encoded

    return {kind: normalised(kind) for kind in (
        "wake",
        "closure",
        "capacity",
        "mutex_group",
    )}
