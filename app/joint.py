"""Intersecting-runway joint trial scheduling.

Where :mod:`app.separation` trials every runway independently, a *joint*
trial also knows **mutually exclusive runway groups**: two intersecting (or
otherwise operationally exclusive) runways may belong to one group whose
departures must keep a configurable minimum occupancy interval between
*any* two successive departures of the group, no matter which member runway
each one uses.

The joint engine first builds one **global stable order** over every runway
``(scheduled_off 升序, flight_no 升序)`` and then walks that single sequence.
Each flight is pushed to the earliest instant satisfying, simultaneously:

1. not earlier than its own scheduled time;
2. the same-runway wake gap behind the previous departure of that runway;
3. outside every closure window of its own runway;
4. the rolling capacity limit of its own runway;
5. the mutex-group interval behind the previous departure of its group.

Because a push from one constraint can land the flight inside another
constraint's reach (a group push into a closure, a closure push past the
capacity anchor, …), every constraint is **re-checked after every push**
until a fixed point is reached. The first constraint that ever demanded a
push — detected in priority order wake → closure → capacity → mutex group —
is reported as the flight's ``constraint``.

A locked flight is immovable, exactly as in the single-runway trial: when
any constraint would move it later, the whole joint calculation stops at
that flight. The blocker carries the earliest compliant time *and* the
earliest time demanded by each applicable constraint (``required_times``);
no suggestion is produced for the blocker or any later flight.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .separation import (
    Capacity,
    ClosureWindow,
    Flight,
    required_gap,
    sort_windows,
)
from .timeutils import to_epoch_seconds

# Detection priority of the constraint that first forces a flight later.
CONSTRAINTS = ("wake", "closure", "capacity", "mutex_group")


def _utc(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


@dataclass(frozen=True, slots=True)
class MutexGroup:
    """A set of mutually exclusive runways.

    Any two successive departures of the group (across all member runways)
    must be at least ``min_interval_seconds`` apart.
    """

    name: str
    runways: tuple[str, ...]
    min_interval_seconds: int


@dataclass(slots=True)
class JointTrialItem:
    """One (feasible) flight of a joint trial, positioned in the global order."""

    position: int  # 1-based position in the global stable order
    runway_position: int  # 1-based stable position within the flight's own runway
    runway: str
    flight_no: str
    wake: str
    original_time: datetime
    adjusted_time: datetime
    delay_seconds: int
    # First constraint (CONSTRAINTS order) that demanded a push; None when
    # the flight departs exactly on schedule.
    constraint: str | None
    # Wake detail: the same-runway predecessor that forced a wake push.
    triggered_by: str | None
    # Closure detail.
    closure_hit: bool
    closure_window: ClosureWindow | None
    locked: bool = False
    # Same-runway rolling-capacity detail.
    capacity_hit: bool = False
    capacity_window_seconds: int | None = None
    capacity_max_departures: int | None = None
    capacity_anchor: str | None = None
    capacity_window_start: datetime | None = None
    capacity_window_end: datetime | None = None
    # Mutex-group detail.
    mutex_group_hit: bool = False
    mutex_group_name: str | None = None
    mutex_group_anchor: str | None = None  # previous group departure
    mutex_group_min_interval_seconds: int | None = None
    mutex_group_window_start: datetime | None = None
    mutex_group_window_end: datetime | None = None


@dataclass(slots=True)
class JointBlocker:
    """The first locked flight that cannot keep its scheduled time.

    ``required_times`` states the earliest time demanded by every applicable
    constraint at the converged solution (``None`` when that constraint does
    not apply to this flight); ``required_earliest_off`` is the earliest time
    satisfying all of them.
    """

    position: int
    runway_position: int
    runway: str
    flight_no: str
    wake: str
    scheduled_off: datetime
    constraint: str  # first push, CONSTRAINTS priority
    required_earliest_off: datetime
    # wake detail
    triggered_by: str | None
    required_gap_seconds: int | None
    # closure detail
    closure_hit: bool
    closure_window: ClosureWindow | None
    # capacity detail (usable only when the blocker's runway has a capacity)
    capacity_hit: bool
    capacity_window_seconds: int | None
    capacity_max_departures: int | None
    capacity_anchor: str | None
    capacity_window_start: datetime | None
    capacity_window_end: datetime | None
    # mutex-group detail (usable only when the blocker's runway is in a group)
    mutex_group_hit: bool
    mutex_group_name: str | None
    mutex_group_anchor: str | None
    mutex_group_min_interval_seconds: int | None
    mutex_group_window_start: datetime | None
    mutex_group_window_end: datetime | None
    # keys: "wake" / "closure" / "capacity" / "mutex_group"; payloads carry
    # domain datetimes and are serialised by the API/persistence layers.
    required_times: dict[str, dict | None] = field(default_factory=dict)


@dataclass(slots=True)
class JointTrialResult:
    groups: list[MutexGroup]
    order: list[tuple[str, Flight]]  # global stable order
    items: list[JointTrialItem]  # feasible global prefix
    blocked: JointBlocker | None = None

    @property
    def feasible(self) -> bool:
        return self.blocked is None


def joint_trial(
    runway_flights: dict[str, list[Flight]],
    *,
    groups: list[MutexGroup],
    matrix: dict[tuple[str, str], int],
    windows_by_runway: dict[str, list[ClosureWindow]] | None = None,
    capacities_by_runway: dict[str, Capacity] | None = None,
) -> JointTrialResult:
    """Greedily schedule every runway together under mutex-group spacing.

    Flights are walked once in the global stable order. Each flight is only
    ever moved later; the wake/closure/capacity/group requirements are
    iterated together until none of them moves the flight any more
    (convergence). A locked flight that the iteration moves later stops the
    whole calculation: it is returned once as a :class:`JointBlocker` with
    the per-constraint required times, and no later flight (on any runway)
    receives a suggestion.
    """
    windows_by_runway = windows_by_runway or {}
    capacities_by_runway = capacities_by_runway or {}

    group_by_runway: dict[str, MutexGroup] = {
        runway: group for group in groups for runway in group.runways
    }
    wins_by_runway = {
        runway: sort_windows(windows)
        for runway, windows in windows_by_runway.items()
    }

    global_order = sorted(
        ((runway, flight) for runway, fl in runway_flights.items() for flight in fl),
        key=lambda pair: (pair[1].t, pair[1].flight_no),
    )

    # Per-runway feasible prefix (wake predecessor and capacity anchors) and
    # the most recent departure of each mutex group.
    rw_items: dict[str, list[JointTrialItem]] = defaultdict(list)
    rw_seen: dict[str, int] = defaultdict(int)
    last_group_item: dict[str, JointTrialItem] = {}
    items: list[JointTrialItem] = []

    for position, (runway, flight) in enumerate(global_order, start=1):
        rw_seen[runway] += 1
        runway_position = rw_seen[runway]
        wins = wins_by_runway.get(runway, [])
        capacity = capacities_by_runway.get(runway)
        group = group_by_runway.get(runway)
        rw_prefix = rw_items[runway]

        adjusted_t = flight.t
        first_constraint: str | None = None

        # Wake working detail.
        wake_trigger_no: str | None = None
        wake_gap = 0
        # Closure working detail.
        closure_hit = False
        closure_window: ClosureWindow | None = None
        # Capacity working detail.
        cap_hit = False
        cap_anchor_item: JointTrialItem | None = None
        cap_window_t = 0
        # Mutex-group working detail.
        grp_hit = False
        grp_anchor_item: JointTrialItem | None = None
        group_prev_item = (
            last_group_item.get(group.name) if group is not None else None
        )

        def note_first(kind: str) -> None:
            nonlocal first_constraint
            if first_constraint is None:
                first_constraint = kind

        while True:
            pushed = False

            # 1) same-runway wake separation
            prev_item = rw_prefix[-1] if rw_prefix else None
            if prev_item is not None:
                gap = required_gap(matrix, prev_item.wake, flight.wake)
                bound = to_epoch_seconds(prev_item.adjusted_time) + gap
                if adjusted_t < bound:
                    adjusted_t = bound
                    wake_trigger_no = prev_item.flight_no
                    wake_gap = gap
                    note_first("wake")
                    pushed = True

            # 2) own-runway closure windows (sorted, non-overlapping)
            for window in wins:
                if adjusted_t < window.s:
                    break  # in the open gap before this and every later window
                if window.contains(adjusted_t):
                    adjusted_t = window.e
                    closure_hit = True
                    closure_window = window
                    note_first("closure")
                    pushed = True

            # 3) same-runway rolling capacity
            if capacity is not None and len(rw_prefix) >= capacity.max_departures:
                anchor_item = rw_prefix[len(rw_prefix) - capacity.max_departures]
                anchor_t = to_epoch_seconds(anchor_item.adjusted_time)
                bound = anchor_t + capacity.window_seconds
                if adjusted_t < bound:
                    adjusted_t = bound
                    cap_hit = True
                    cap_anchor_item = anchor_item
                    cap_window_t = anchor_t
                    note_first("capacity")
                    pushed = True

            # 4) mutex-group spacing: behind the latest group departure.
            #    Group adjusted times form a strictly increasing chain
            #    (every member is forced to interval after the previous one),
            #    so the immediately previous group departure is the only
            #    predecessor that can bind.
            if group_prev_item is not None:
                gprev_t = to_epoch_seconds(group_prev_item.adjusted_time)
                bound = gprev_t + group.min_interval_seconds
                if adjusted_t < bound:
                    adjusted_t = bound
                    grp_hit = True
                    grp_anchor_item = group_prev_item
                    note_first("mutex_group")
                    pushed = True

            if not pushed:
                break

        if flight.locked and adjusted_t > flight.t:
            blocker = _build_blocker(
                position=position,
                runway_position=runway_position,
                runway=runway,
                flight=flight,
                constraint=first_constraint,
                adjusted_t=adjusted_t,
                rw_prefix=rw_prefix,
                matrix=matrix,
                closure_window=closure_window,
                closure_hit=closure_hit,
                capacity=capacity,
                cap_hit=cap_hit,
                cap_anchor_item=cap_anchor_item,
                group=group,
                grp_hit=grp_hit,
                grp_anchor_item=grp_anchor_item,
                group_prev_item=group_prev_item,
                wake_trigger_no=wake_trigger_no,
                wake_gap=wake_gap,
            )
            return JointTrialResult(
                groups=groups, order=global_order, items=items, blocked=blocker
            )

        cap_window_end_t = (
            cap_window_t + capacity.window_seconds
            if cap_anchor_item is not None and capacity is not None
            else None
        )
        grp_window_start_t = (
            to_epoch_seconds(grp_anchor_item.adjusted_time)
            if grp_anchor_item is not None
            else None
        )
        item = JointTrialItem(
            position=position,
            runway_position=runway_position,
            runway=runway,
            flight_no=flight.flight_no,
            wake=flight.wake,
            original_time=flight.scheduled_off,
            adjusted_time=_utc(adjusted_t),
            delay_seconds=adjusted_t - flight.t,
            constraint=first_constraint,
            triggered_by=wake_trigger_no,
            closure_hit=closure_hit,
            closure_window=closure_window,
            locked=flight.locked,
            capacity_hit=cap_hit,
            capacity_window_seconds=(
                capacity.window_seconds if capacity is not None else None
            ),
            capacity_max_departures=(
                capacity.max_departures if capacity is not None else None
            ),
            capacity_anchor=(
                cap_anchor_item.flight_no if cap_anchor_item is not None else None
            ),
            capacity_window_start=(
                _utc(cap_window_t) if cap_anchor_item is not None else None
            ),
            capacity_window_end=(
                _utc(cap_window_end_t) if cap_window_end_t is not None else None
            ),
            mutex_group_hit=grp_hit,
            mutex_group_name=group.name if group is not None else None,
            mutex_group_anchor=(
                grp_anchor_item.flight_no if grp_anchor_item is not None else None
            ),
            mutex_group_min_interval_seconds=(
                group.min_interval_seconds if group is not None else None
            ),
            mutex_group_window_start=(
                _utc(grp_window_start_t) if grp_window_start_t is not None else None
            ),
            mutex_group_window_end=(
                _utc(grp_window_start_t + group.min_interval_seconds)
                if grp_window_start_t is not None and group is not None
                else None
            ),
        )
        items.append(item)
        rw_prefix.append(item)
        if group is not None:
            last_group_item[group.name] = item

    return JointTrialResult(
        groups=groups, order=global_order, items=items, blocked=None
    )


def _build_blocker(
    *,
    position: int,
    runway_position: int,
    runway: str,
    flight: Flight,
    constraint: str | None,
    adjusted_t: int,
    rw_prefix: list[JointTrialItem],
    matrix: dict[tuple[str, str], int],
    closure_window: ClosureWindow | None,
    closure_hit: bool,
    capacity: Capacity | None,
    cap_hit: bool,
    cap_anchor_item: JointTrialItem | None,
    group: MutexGroup | None,
    grp_hit: bool,
    grp_anchor_item: JointTrialItem | None,
    group_prev_item: JointTrialItem | None,
    wake_trigger_no: str | None,
    wake_gap: int,
) -> JointBlocker:
    """Assemble the blocker, including the per-constraint required times."""
    # Wake: applicable whenever a same-runway predecessor exists.
    wake_required: dict | None = None
    if rw_prefix:
        prev_item = rw_prefix[-1]
        gap = required_gap(matrix, prev_item.wake, flight.wake)
        wake_required = {
            "required_off": _utc(to_epoch_seconds(prev_item.adjusted_time) + gap),
            "triggered_by": prev_item.flight_no,
            "required_gap_seconds": gap,
        }

    # Closure: applicable iff iteration had to leave a window; the reported
    # time is the end of the last window that pushed the flight.
    closure_required = None
    if closure_window is not None:
        closure_required = {
            "required_off": closure_window.end,
            "window": closure_window,
        }

    # Capacity: applicable once max_departures earlier runway departures exist.
    capacity_required = None
    capacity_anchor_item = cap_anchor_item
    if capacity is not None and len(rw_prefix) >= capacity.max_departures:
        anchor = rw_prefix[len(rw_prefix) - capacity.max_departures]
        anchor_t = to_epoch_seconds(anchor.adjusted_time)
        capacity_anchor_item = anchor
        capacity_required = {
            "required_off": _utc(anchor_t + capacity.window_seconds),
            "anchor_flight_no": anchor.flight_no,
            "window_seconds": capacity.window_seconds,
            "max_departures": capacity.max_departures,
            "window": (anchor_t, anchor_t + capacity.window_seconds),
        }

    # Mutex group: applicable once an earlier group departure exists; the
    # required time is always reported then (it may bind even when another
    # constraint pushed first).
    group_required = None
    if group is not None and group_prev_item is not None:
        gprev_t = to_epoch_seconds(group_prev_item.adjusted_time)
        group_required = {
            "required_off": _utc(gprev_t + group.min_interval_seconds),
            "triggered_by": group_prev_item.flight_no,
            "group": group.name,
            "min_interval_seconds": group.min_interval_seconds,
            "window": (gprev_t, gprev_t + group.min_interval_seconds),
        }

    # Top-level group detail anchors at the most recent group departure
    # (regardless of whether the group constraint itself triggered a push).
    grp_detail_item = grp_anchor_item or group_prev_item

    cap_window_t = (
        to_epoch_seconds(capacity_anchor_item.adjusted_time)
        if capacity_anchor_item is not None
        else None
    )
    grp_window_t = (
        to_epoch_seconds(grp_detail_item.adjusted_time)
        if grp_detail_item is not None
        else None
    )
    return JointBlocker(
        position=position,
        runway_position=runway_position,
        runway=runway,
        flight_no=flight.flight_no,
        wake=flight.wake,
        scheduled_off=flight.scheduled_off,
        constraint=constraint,
        required_earliest_off=_utc(adjusted_t),
        triggered_by=wake_trigger_no,
        required_gap_seconds=wake_gap if wake_trigger_no is not None else None,
        closure_hit=closure_hit,
        closure_window=closure_window,
        capacity_hit=cap_hit,
        capacity_window_seconds=(
            capacity.window_seconds if capacity is not None else None
        ),
        capacity_max_departures=(
            capacity.max_departures if capacity is not None else None
        ),
        capacity_anchor=(
            capacity_anchor_item.flight_no
            if capacity_anchor_item is not None
            else None
        ),
        capacity_window_start=(
            _utc(cap_window_t) if cap_window_t is not None else None
        ),
        capacity_window_end=(
            _utc(cap_window_t + capacity.window_seconds)
            if cap_window_t is not None and capacity is not None
            else None
        ),
        mutex_group_hit=grp_hit,
        mutex_group_name=group.name if group is not None else None,
        mutex_group_anchor=(
            grp_detail_item.flight_no if grp_detail_item is not None else None
        ),
        mutex_group_min_interval_seconds=(
            group.min_interval_seconds if group is not None else None
        ),
        mutex_group_window_start=(
            _utc(grp_window_t) if grp_window_t is not None else None
        ),
        mutex_group_window_end=(
            _utc(grp_window_t + group.min_interval_seconds)
            if grp_window_t is not None and group is not None
            else None
        ),
        required_times={
            "wake": wake_required,
            "closure": closure_required,
            "capacity": capacity_required,
            "mutex_group": group_required,
        },
    )
