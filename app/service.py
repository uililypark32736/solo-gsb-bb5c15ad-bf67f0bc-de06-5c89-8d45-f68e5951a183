"""Application service: request -> domain objects -> persistence payloads."""

from __future__ import annotations

from .schemas import (
    JointTrialRequest,
    SlotInsertRequest,
    WakeRequest,
    matrix_to_nested,
)
from .joint import JointTrialResult, MutexGroup, joint_trial
from .separation import (
    Capacity,
    ClosureConflict,
    ClosureWindow,
    Flight,
    SlotInsertResult,
    SwapRunwayResult,
    TrialRunwayResult,
    Violation,
    best_adjacent_swap,
    build_matrix,
    find_capacity_violations,
    find_closure_conflicts,
    find_insert_slot,
    find_violations,
    sort_flights,
    sort_windows,
    trial_reschedule,
)
from .timeutils import ensure_utc


def _build_runway_flights(req: WakeRequest) -> dict[str, list[Flight]]:
    """Normalise every flight to UTC and return per-runway sorted lists."""
    payloads: dict[str, list[Flight]] = {}
    for rw in req.runways:
        flights = [
            Flight(
                flight_no=f.flight_no,
                scheduled_off=ensure_utc(f.scheduled_off),
                wake=f.wake.value,
                locked=f.locked,
                runway=rw.runway,
            )
            for f in rw.flights
        ]
        payloads[rw.runway] = sort_flights(flights)
    return payloads


def _runways_with_locked_input(req: WakeRequest) -> set[str]:
    """Runways whose request explicitly carried the ``locked`` field on at
    least one flight — only those runways echo lock detail, so requests that
    never mention ``locked`` keep the legacy response shape."""
    runways: set[str] = set()
    for rw in req.runways:
        if any("locked" in f.model_fields_set for f in rw.flights):
            runways.add(rw.runway)
    return runways


def _locked_flights_by_runway(
    payloads: dict[str, list[Flight]],
) -> dict[str, list[str]]:
    """Stable-order flight numbers of the locked flights per runway."""
    return {
        runway: [f.flight_no for f in flights if f.locked]
        for runway, flights in payloads.items()
    }


def _build_runway_windows(req: WakeRequest) -> dict[str, list[ClosureWindow]]:
    """Normalise every closure window to UTC, sorted per runway."""
    windows: dict[str, list[ClosureWindow]] = {}
    for rw in req.runways:
        windows[rw.runway] = sort_windows(
            [
                ClosureWindow(start=ensure_utc(w.start), end=ensure_utc(w.end))
                for w in rw.closures
            ]
        )
    return windows


def _build_runway_capacities(req: WakeRequest) -> dict[str, Capacity]:
    """Return per-runway rolling capacity limits (runways without one omitted)."""
    return {
        rw.runway: Capacity(
            window_seconds=rw.capacity.window_seconds,
            max_departures=rw.capacity.max_departures,
        )
        for rw in req.runways
        if rw.capacity is not None
    }


def _matrix_from_request(
    req: WakeRequest | SlotInsertRequest,
) -> dict[tuple[str, str], int]:
    if req.matrix is None:
        return build_matrix(None)
    return build_matrix(
        {(e.leader.value, e.follower.value): e.seconds for e in req.matrix}
    )


def run_validate(req: WakeRequest) -> tuple[
    dict[str, list[Flight]],
    dict[tuple[str, str], int],
    dict[str, list[ClosureWindow]],
    dict[str, Capacity],
    dict[str, list[Violation]],
    dict[str, list[ClosureConflict]],
    dict[str, list],
    set[str],
    dict[str, list[str]],
]:
    payloads = _build_runway_flights(req)
    windows = _build_runway_windows(req)
    capacities = _build_runway_capacities(req)
    matrix = _matrix_from_request(req)
    violations = {
        runway: find_violations(flights, matrix)
        for runway, flights in payloads.items()
    }
    conflicts = {
        runway: find_closure_conflicts(flights, windows[runway])
        for runway, flights in payloads.items()
    }
    capacity_violations = {
        runway: find_capacity_violations(flights, capacities.get(runway))
        for runway, flights in payloads.items()
    }
    locked_runways = _runways_with_locked_input(req)
    locked_flights = _locked_flights_by_runway(payloads)
    return (
        payloads,
        matrix,
        windows,
        capacities,
        violations,
        conflicts,
        capacity_violations,
        locked_runways,
        locked_flights,
    )


def run_trial(req: WakeRequest) -> tuple[
    dict[str, list[Flight]],
    dict[tuple[str, str], int],
    dict[str, list[ClosureWindow]],
    dict[str, Capacity],
    dict[str, TrialRunwayResult],
    set[str],
    dict[str, list[str]],
]:
    payloads = _build_runway_flights(req)
    windows = _build_runway_windows(req)
    capacities = _build_runway_capacities(req)
    matrix = _matrix_from_request(req)
    trials = {
        runway: trial_reschedule(
            flights,
            matrix,
            windows[runway],
            capacities.get(runway),
        )
        for runway, flights in payloads.items()
    }
    locked_runways = _runways_with_locked_input(req)
    locked_flights = _locked_flights_by_runway(payloads)
    return payloads, matrix, windows, capacities, trials, locked_runways, locked_flights


def run_best_adjacent_swap(req: WakeRequest) -> tuple[
    dict[str, list[Flight]],
    dict[tuple[str, str], int],
    dict[str, list[ClosureWindow]],
    dict[str, Capacity],
    dict[str, SwapRunwayResult],
]:
    """Optimise each runway independently over all adjacent swaps.

    Callers must reject requests carrying ``locked=true`` flights beforehand
    (the endpoint answers 422): immovable flights contradict reordering.
    """
    payloads = _build_runway_flights(req)
    windows = _build_runway_windows(req)
    capacities = _build_runway_capacities(req)
    matrix = _matrix_from_request(req)
    swaps = {
        runway: best_adjacent_swap(
            flights,
            matrix,
            windows[runway],
            capacities.get(runway),
        )
        for runway, flights in payloads.items()
    }
    return payloads, matrix, windows, capacities, swaps


def nested_matrix(matrix: dict[tuple[str, str], int]) -> dict[str, dict[str, int]]:
    return matrix_to_nested(matrix)


class SlotBaselineError(Exception):
    """现有计划本身违反尾流间隔、关闭窗口或滚动容量约束（API 层转为 422）。"""


def _baseline_error_message(
    violations: list[Violation],
    conflicts: list[ClosureConflict],
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
    return "现有计划违反约束，无法评估插入：" + "；".join(parts)


def run_slot_insert(
    req: SlotInsertRequest,
) -> tuple[SlotInsertResult, dict[tuple[str, str], int]]:
    """单跑道候选插入：先校验现有计划可行，再求区间内最早可行时刻。

    现有航班时刻保持不变；只读计算，不落库、不改写任何计划或批次。
    现有计划违反尾流/关闭窗口/容量约束时抛出 :class:`SlotBaselineError`。
    """
    rw = req.runway
    flights = sort_flights(
        [
            Flight(
                flight_no=f.flight_no,
                scheduled_off=ensure_utc(f.scheduled_off),
                wake=f.wake.value,
                locked=f.locked,
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
    matrix = _matrix_from_request(req)

    violations = find_violations(flights, matrix)
    conflicts = find_closure_conflicts(flights, windows)
    capacity_violations = find_capacity_violations(flights, capacity)
    if violations or conflicts or capacity_violations:
        raise SlotBaselineError(
            _baseline_error_message(violations, conflicts, capacity_violations)
        )

    result = find_insert_slot(
        flights,
        candidate_flight_no=req.candidate.flight_no,
        candidate_wake=req.candidate.wake.value,
        earliest_off=ensure_utc(req.candidate.earliest_off),
        latest_off=ensure_utc(req.candidate.latest_off),
        matrix=matrix,
        windows=windows,
        capacity=capacity,
    )
    return result, matrix


def _groups_from_request(req: JointTrialRequest) -> list[MutexGroup]:
    if not req.groups:
        return []
    return [
        MutexGroup(
            name=group.name or f"group-{index}",
            runways=tuple(group.runways),
            min_interval_seconds=group.min_interval_seconds,
        )
        for index, group in enumerate(req.groups, start=1)
    ]


def run_joint_trial(req: JointTrialRequest) -> tuple[
    dict[str, list[Flight]],
    dict[tuple[str, str], int],
    dict[str, list[ClosureWindow]],
    dict[str, Capacity],
    list[MutexGroup],
    JointTrialResult,
    set[str],
    dict[str, list[str]],
]:
    """Build the global stable order and schedule all runways together.

    Flights stay bound to their own runway's wake/closure/capacity rules;
    mutex groups add cross-runway minimum intervals. Locked flights stop the
    whole joint calculation at the first one that cannot keep schedule, and
    the blocker carries every constraint's required earliest time.
    """
    payloads = _build_runway_flights(req)
    windows = _build_runway_windows(req)
    capacities = _build_runway_capacities(req)
    matrix = _matrix_from_request(req)
    groups = _groups_from_request(req)
    result = joint_trial(
        payloads,
        groups=groups,
        matrix=matrix,
        windows_by_runway=windows,
        capacities_by_runway=capacities,
    )
    locked_runways = _runways_with_locked_input(req)
    locked_flights = _locked_flights_by_runway(payloads)
    return (
        payloads,
        matrix,
        windows,
        capacities,
        groups,
        result,
        locked_runways,
        locked_flights,
    )
