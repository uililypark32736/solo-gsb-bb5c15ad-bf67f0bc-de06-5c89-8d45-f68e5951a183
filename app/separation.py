"""Core wake-separation domain logic.

The service operates on *time in seconds* so the separation matrix (defined
in seconds) can be applied with plain integer arithmetic, no float rounding.

Wake turbulence categories follow the four-class scheme:

``L``  LIGHT    – light aircraft (e.g. PA28, C152)
``M``  MEDIUM   – medium aircraft (most airliners below 136 t)
``H``  HEAVY    – heavy aircraft (>= 136 t MTOW)
``J``  SUPER    – super aircraft (e.g. A380)

Only *adjacent* pairs on the same runway are considered, matching the
operational practice for departures on a single runway: the leader of a pair
is the flight immediately ahead in the departure order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .timeutils import to_epoch_seconds


class Wake(str, Enum):
    LIGHT = "L"
    MEDIUM = "M"
    HEAVY = "H"
    SUPER = "J"


# Default leader -> follower minimum separation in seconds.
# ICAO-style departure values (recat categories map loosely to this matrix).
#   LIGHT leader  : 100 s behind light, otherwise 60 s
#   MEDIUM leader : 100 s behind light, otherwise 60 s
#   HEAVY leader  : 120 s behind medium, 100 s behind heavy, 100 s light
#   SUPER leader  : 180 s behind heavy/medium, 180 s light, 120 s super
# Values are intentionally simple defaults; a request may override the whole
# matrix, in which case any omitted leader/follower pair defaults to zero
# (i.e. no required radar minimum for that pair).
DEFAULT_MATRIX: dict[str, dict[str, int]] = {
    "L": {"L": 100, "M": 60, "H": 60, "J": 60},
    "M": {"L": 100, "M": 60, "H": 60, "J": 60},
    "H": {"L": 100, "M": 120, "H": 100, "J": 60},
    "J": {"L": 180, "M": 180, "H": 180, "J": 120},
}


@dataclass(frozen=True, slots=True)
class Flight:
    """A flight plan item after timezone normalisation."""

    flight_no: str
    scheduled_off: datetime  # always tz-aware, converted to UTC
    wake: str
    locked: bool = False  # trial must keep this flight exactly on scheduled_off
    runway: str = ""  # owning runway identifier (filled in for joint trials)

    @property
    def t(self) -> int:
        return to_epoch_seconds(self.scheduled_off)

    def sort_key(self) -> tuple[int, str]:
        """Stable ordering key: scheduled time, then flight number."""
        return self.t, self.flight_no


@dataclass(frozen=True, slots=True)
class ClosureWindow:
    """A temporary runway closure ``[start, end)`` after UTC normalisation."""

    start: datetime  # always tz-aware, converted to UTC
    end: datetime  # always tz-aware, converted to UTC

    @property
    def s(self) -> int:
        return to_epoch_seconds(self.start)

    @property
    def e(self) -> int:
        return to_epoch_seconds(self.end)

    def contains(self, t: int) -> bool:
        return self.s <= t < self.e


@dataclass(frozen=True, slots=True)
class Capacity:
    """A rolling departure-capacity limit: at most ``max_departures`` flights
    may depart in any half-open window of ``window_seconds`` seconds."""

    window_seconds: int
    max_departures: int


@dataclass(frozen=True, slots=True)
class CapacityViolation:
    """A flight whose addition makes some ``window_seconds`` span hold more
    than ``max_departures`` departures, measured on the planned schedule.

    The triggering window is anchored at the ``max_departures``-th flight
    ahead of this one: ``[anchor_off, anchor_off + window_seconds)`` contains
    ``max_departures + 1`` departures (the anchor flight and this flight at
    its ends). ``earliest_compliant_off`` is the earliest instant at which
    this flight may depart without breaching the rolling limit against the
    flights ahead of it (``anchor_off + window_seconds``).
    """

    flight_no: str
    wake: str
    scheduled_off: datetime
    position: int
    window_seconds: int
    max_departures: int
    anchor_flight_no: str  # flight anchoring the triggering window (its start)
    anchor_position: int
    anchor_off: datetime  # triggering window start
    window_end: datetime  # triggering window end (exclusive)
    departures_in_window: int
    earliest_compliant_off: datetime


@dataclass(frozen=True, slots=True)
class ClosureConflict:
    """A flight whose scheduled off-time falls inside a closure window."""

    flight_no: str
    wake: str
    scheduled_off: datetime
    position: int
    window_index: int  # 1-based index into the runway's sorted closure list
    window_start: datetime
    window_end: datetime


@dataclass(frozen=True, slots=True)
class Violation:
    leader_no: str
    follower_no: str
    leader_wake: str
    follower_wake: str
    leader_time: datetime
    follower_time: datetime
    required_seconds: int
    actual_seconds: int
    shortfall_seconds: int
    leader_position: int
    follower_position: int


@dataclass(slots=True)
class TrialItem:
    flight_no: str
    wake: str
    original_time: datetime
    adjusted_time: datetime
    delay_seconds: int
    position: int
    triggered_by: str | None  # flight number of the leader that forced the pushback
    closure_hit: bool  # a closure window forced (further) delay
    closure_window: ClosureWindow | None  # the last window that pushed this flight
    locked: bool = False  # immovable flight: kept exactly on its scheduled time
    capacity_hit: bool = False  # the rolling capacity limit forced (further) delay
    capacity_window_seconds: int | None = None
    capacity_max_departures: int | None = None
    capacity_anchor: str | None = None  # flight anchoring the capacity window
    capacity_window_start: datetime | None = None  # [anchor_off, +window)
    capacity_window_end: datetime | None = None


@dataclass(frozen=True, slots=True)
class TrialBlocker:
    """The first locked flight that cannot stay on its scheduled time.

    Computed in stable order, this is the earliest locked flight that the
    wake-separation / closure-window / rolling-capacity requirements would
    push to a later instant. It is *not* moved and no violating time is
    proposed; later flights receive no suggestions either.
    ``required_earliest_off`` is the earliest safe time after every
    constraint was iterated, and ``constraint`` names the first constraint
    type that demanded a push (``wake`` / ``closure`` / ``capacity``).
    """

    flight_no: str
    wake: str
    scheduled_off: datetime
    position: int
    constraint: str  # "wake" | "closure" | "capacity"
    required_earliest_off: datetime
    # wake detail
    triggered_by: str | None
    required_gap_seconds: int | None
    # closure detail
    closure_hit: bool
    closure_window: ClosureWindow | None
    # capacity detail
    capacity_hit: bool
    capacity_window_seconds: int | None
    capacity_max_departures: int | None
    capacity_anchor: str | None
    capacity_window_start: datetime | None
    capacity_window_end: datetime | None


@dataclass(slots=True)
class TrialRunwayResult:
    """A runway's trial outcome: either a full feasible proposal or a
    feasible prefix followed by the first infeasible locked flight."""

    items: list[TrialItem]
    blocked: TrialBlocker | None = None

    @property
    def feasible(self) -> bool:
        return self.blocked is None


def build_matrix(
    entries: dict[tuple[str, str], int] | None,
) -> dict[tuple[str, str], int]:
    """Return a ``(leader, follower) -> seconds`` lookup.

    ``entries`` is the client-supplied override. When omitted/empty the built-in
    :data:`DEFAULT_MATRIX` is used; otherwise every unspecified pair is zero
    (no minimum for that pair).
    """
    if not entries:
        return {
            (leader, follower): seconds
            for leader, followers in DEFAULT_MATRIX.items()
            for follower, seconds in followers.items()
        }
    return dict(entries)


def required_gap(
    matrix: dict[tuple[str, str], int], leader_wake: str, follower_wake: str
) -> int:
    return matrix.get((leader_wake, follower_wake), 0)


def sort_flights(flights: list[Flight]) -> list[Flight]:
    """Stable sort by (scheduled time, flight number)."""
    return sorted(flights, key=Flight.sort_key)


def sort_windows(windows: list[ClosureWindow]) -> list[ClosureWindow]:
    """Sort closure windows by (start, end); input is already non-overlapping."""
    return sorted(windows, key=lambda w: (w.s, w.e))


def find_closure_conflicts(
    flights: list[Flight], windows: list[ClosureWindow]
) -> list[ClosureConflict]:
    """List flights whose scheduled off-time falls inside a closure window.

    Windows are half-open ``[start, end)`` and non-overlapping, so a flight
    hits at most one window. ``window_index`` is 1-based over the sorted
    window list, matching the closures echoed back in the API responses.
    """
    ordered = sort_flights(flights)
    wins = sort_windows(windows)
    conflicts: list[ClosureConflict] = []
    for position, flight in enumerate(ordered, start=1):
        for index, window in enumerate(wins, start=1):
            if window.contains(flight.t):
                conflicts.append(
                    ClosureConflict(
                        flight_no=flight.flight_no,
                        wake=flight.wake,
                        scheduled_off=flight.scheduled_off,
                        position=position,
                        window_index=index,
                        window_start=window.start,
                        window_end=window.end,
                    )
                )
                break
    return conflicts


def find_violations(
    flights: list[Flight], matrix: dict[tuple[str, str], int]
) -> list[Violation]:
    """List violations between every adjacent pair of the (sorted) sequence."""
    ordered = sort_flights(flights)
    violations: list[Violation] = []
    for i in range(len(ordered) - 1):
        leader, follower = ordered[i], ordered[i + 1]
        required = required_gap(matrix, leader.wake, follower.wake)
        actual = follower.t - leader.t
        if actual < required:
            violations.append(
                Violation(
                    leader_no=leader.flight_no,
                    follower_no=follower.flight_no,
                    leader_wake=leader.wake,
                    follower_wake=follower.wake,
                    leader_time=leader.scheduled_off,
                    follower_time=follower.scheduled_off,
                    required_seconds=required,
                    actual_seconds=actual,
                    shortfall_seconds=required - actual,
                    leader_position=i + 1,
                    follower_position=i + 2,
                )
            )
    return violations


def find_capacity_violations(
    flights: list[Flight], capacity: Capacity | None
) -> list[CapacityViolation]:
    """List departures that overflow the rolling capacity window.

    With flights in stable order, any half-open span of ``window_seconds``
    contains more than ``max_departures`` departures iff, for some flight at
    position ``p``, the flight ``max_departures`` positions ahead of it (the
    *anchor*, at index ``p - max_departures``) departs less than
    ``window_seconds`` before it: that anchor's window
    ``[t_anchor, t_anchor + window_seconds)`` then holds the anchor, the
    flights in between, and this flight — at least ``max_departures + 1``
    departures. Each overflowing flight is reported once with that window and
    with its earliest compliant time ``t_anchor + window_seconds``.
    Results follow the stable departure order.
    """
    if capacity is None:
        return []
    window_seconds = capacity.window_seconds
    k = capacity.max_departures
    ordered = sort_flights(flights)
    violations: list[CapacityViolation] = []
    for idx in range(k, len(ordered)):
        anchor = ordered[idx - k]
        flight = ordered[idx]
        if flight.t - anchor.t < window_seconds:
            window_end_t = anchor.t + window_seconds
            # every flight from the anchor through this flight is inside
            in_window = [
                other
                for other in ordered[idx - k : idx + 1]
                if anchor.t <= other.t < window_end_t
            ]
            violations.append(
                CapacityViolation(
                    flight_no=flight.flight_no,
                    wake=flight.wake,
                    scheduled_off=flight.scheduled_off,
                    position=idx + 1,
                    window_seconds=window_seconds,
                    max_departures=k,
                    anchor_flight_no=anchor.flight_no,
                    anchor_position=idx - k + 1,
                    anchor_off=anchor.scheduled_off,
                    window_end=datetime.fromtimestamp(window_end_t, tz=timezone.utc),
                    departures_in_window=len(in_window),
                    earliest_compliant_off=datetime.fromtimestamp(
                        window_end_t, tz=timezone.utc
                    ),
                )
            )
    return violations


@dataclass(frozen=True, slots=True)
class ConflictGroupFlight:
    """A flight inside a departure-conflict group."""

    position: int  # 1-based stable-order position on its runway
    flight_no: str
    wake: str
    scheduled_off: datetime


@dataclass(frozen=True, slots=True)
class ConflictGroup:
    """One connected component of a runway's departure conflicts.

    ``flights`` is sorted by (scheduled time, flight number); the counts
    say how many wake violations, capacity overflows and closure-window
    hits the component contains.
    """

    flights: tuple[ConflictGroupFlight, ...]
    wake_conflict_count: int
    capacity_conflict_count: int
    closure_conflict_count: int


def build_conflict_groups(
    flights: list[Flight],
    violations: list[Violation],
    closure_conflicts: list[ClosureConflict],
    capacity_violations: list[CapacityViolation],
) -> list[ConflictGroup]:
    """Group one runway's departure conflicts into connected components.

    The graph is built over the runway's flights: every wake violation
    links its leader and follower, every capacity overflow links *all*
    same-runway flights whose scheduled time falls inside its triggering
    window ``[anchor_off, window_end)``, and every closure-window hit marks
    its flight as a singleton conflict node. The connected components of
    this graph are the conflict groups: overlapping capacity windows and
    wake edges merge through the flights they share, while unrelated
    conflicts never merge just because their flights sit next to each other
    in the planned order. Flights untouched by any conflict are not
    returned. Groups are sorted by their earliest flight (scheduled time,
    then flight number); flights inside a group follow the same key.
    """
    ordered = sort_flights(flights)
    by_no = {f.flight_no: f for f in ordered}
    position_of = {f.flight_no: idx for idx, f in enumerate(ordered, start=1)}

    parent: dict[str, str] = {}

    def find(no: str) -> str:
        root = no
        while parent[root] != root:
            root = parent[root]
        while parent[no] != root:
            parent[no], no = root, parent[no]
        return root

    def add(no: str) -> None:
        parent.setdefault(no, no)

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for v in violations:
        add(v.leader_no)
        add(v.follower_no)
        union(v.leader_no, v.follower_no)

    for cv in capacity_violations:
        start_t = to_epoch_seconds(cv.anchor_off)
        end_t = to_epoch_seconds(cv.window_end)
        members = [f.flight_no for f in ordered if start_t <= f.t < end_t]
        if cv.flight_no not in members:
            members.append(cv.flight_no)
        for no in members:
            add(no)
            union(members[0], no)

    for c in closure_conflicts:
        add(c.flight_no)

    components: dict[str, list[str]] = {}
    for no in parent:
        components.setdefault(find(no), []).append(no)

    groups: list[ConflictGroup] = []
    for member_nos in components.values():
        member_set = set(member_nos)
        group_flights = tuple(
            ConflictGroupFlight(
                position=position_of[no],
                flight_no=no,
                wake=by_no[no].wake,
                scheduled_off=by_no[no].scheduled_off,
            )
            for no in sorted(member_nos, key=lambda n: by_no[n].sort_key())
        )
        groups.append(
            ConflictGroup(
                flights=group_flights,
                wake_conflict_count=sum(
                    1 for v in violations if v.leader_no in member_set
                ),
                capacity_conflict_count=sum(
                    1 for cv in capacity_violations if cv.flight_no in member_set
                ),
                closure_conflict_count=sum(
                    1 for c in closure_conflicts if c.flight_no in member_set
                ),
            )
        )
    groups.sort(
        key=lambda g: (
            to_epoch_seconds(g.flights[0].scheduled_off),
            g.flights[0].flight_no,
        )
    )
    return groups


def _trial_on_order(
    ordered: list[Flight],
    matrix: dict[tuple[str, str], int],
    wins: list[ClosureWindow],
    capacity: Capacity | None,
) -> TrialRunwayResult:
    """Cascade-delay engine over an explicitly ordered departure sequence.

    This is the shared core behind :func:`trial_reschedule` and
    :func:`best_adjacent_swap`: unlike the former it does *not* sort — the
    caller passes the exact sequence to evaluate, and closure windows must
    already be sorted. Flights are only ever pushed later, never earlier.
    """
    items: list[TrialItem] = []
    prev: Flight | None = None
    prev_adjusted_t = 0
    for position, flight in enumerate(ordered, start=1):
        adjusted_t = flight.t
        triggered_by: str | None = None
        wake_required = 0
        if prev is not None:
            wake_required = required_gap(matrix, prev.wake, flight.wake)
            earliest = prev_adjusted_t + wake_required
            if adjusted_t < earliest:
                adjusted_t = earliest
                triggered_by = prev.flight_no
        closure_hit = False
        closure_window: ClosureWindow | None = None
        capacity_hit = False
        capacity_anchor: TrialItem | None = None
        capacity_window_t = 0
        while True:
            pushed = False
            for window in wins:
                if adjusted_t < window.s:
                    break  # in a gap before this (and every later) window
                if window.contains(adjusted_t):
                    adjusted_t = window.e
                    closure_hit = True
                    closure_window = window
                    pushed = True
            if capacity is not None and position > capacity.max_departures:
                anchor_item = items[position - 1 - capacity.max_departures]
                anchor_t = to_epoch_seconds(anchor_item.adjusted_time)
                earliest = anchor_t + capacity.window_seconds
                if adjusted_t < earliest:
                    adjusted_t = earliest
                    capacity_hit = True
                    capacity_anchor = anchor_item
                    capacity_window_t = anchor_t
                    pushed = True
            if not pushed:
                break

        if flight.locked and adjusted_t > flight.t:
            # Immovable flight: name the first constraint that demanded a
            # push (wake, then closure, then capacity) and stop the runway.
            if triggered_by is not None:
                constraint = "wake"
            elif closure_hit:
                constraint = "closure"
            else:
                constraint = "capacity"
            blocked = TrialBlocker(
                flight_no=flight.flight_no,
                wake=flight.wake,
                scheduled_off=flight.scheduled_off,
                position=position,
                constraint=constraint,
                required_earliest_off=datetime.fromtimestamp(
                    adjusted_t, tz=timezone.utc
                ),
                triggered_by=triggered_by,
                required_gap_seconds=wake_required if triggered_by is not None else None,
                closure_hit=closure_hit,
                closure_window=closure_window,
                capacity_hit=capacity_hit,
                capacity_window_seconds=(
                    capacity.window_seconds
                    if capacity_hit and capacity is not None
                    else None
                ),
                capacity_max_departures=(
                    capacity.max_departures
                    if capacity_hit and capacity is not None
                    else None
                ),
                capacity_anchor=(
                    capacity_anchor.flight_no if capacity_anchor is not None else None
                ),
                capacity_window_start=(
                    datetime.fromtimestamp(capacity_window_t, tz=timezone.utc)
                    if capacity_anchor is not None
                    else None
                ),
                capacity_window_end=(
                    datetime.fromtimestamp(
                        capacity_window_t + capacity.window_seconds,
                        tz=timezone.utc,
                    )
                    if capacity_anchor is not None and capacity is not None
                    else None
                ),
            )
            return TrialRunwayResult(items=items, blocked=blocked)

        items.append(
            TrialItem(
                flight_no=flight.flight_no,
                wake=flight.wake,
                original_time=flight.scheduled_off,
                adjusted_time=datetime.fromtimestamp(adjusted_t, tz=timezone.utc),
                delay_seconds=adjusted_t - flight.t,
                position=position,
                triggered_by=triggered_by,
                closure_hit=closure_hit,
                closure_window=closure_window,
                locked=flight.locked,
                capacity_hit=capacity_hit,
                capacity_window_seconds=(
                    capacity.window_seconds if capacity is not None else None
                ),
                capacity_max_departures=(
                    capacity.max_departures if capacity is not None else None
                ),
                capacity_anchor=(
                    capacity_anchor.flight_no if capacity_anchor is not None else None
                ),
                capacity_window_start=(
                    datetime.fromtimestamp(capacity_window_t, tz=timezone.utc)
                    if capacity_anchor is not None
                    else None
                ),
                capacity_window_end=(
                    datetime.fromtimestamp(
                        capacity_window_t + capacity.window_seconds,
                        tz=timezone.utc,
                    )
                    if capacity_anchor is not None and capacity is not None
                    else None
                ),
            )
        )
        prev = flight
        prev_adjusted_t = adjusted_t
    return TrialRunwayResult(items=items, blocked=None)


def trial_reschedule(
    flights: list[Flight],
    matrix: dict[tuple[str, str], int],
    windows: list[ClosureWindow] | None = None,
    capacity: Capacity | None = None,
) -> TrialRunwayResult:
    """Greedily push flights back to the earliest safe time, keeping order.

    Every flight is placed at the earliest instant that (a) is not earlier
    than its scheduled time, (b) keeps at least the matrix gap behind the
    (possibly already delayed) flight directly ahead of it, (c) avoids every
    closure window, and (d) keeps the runway's rolling capacity limit: no
    half-open ``window_seconds`` span may already hold ``max_departures``
    earlier departures (equivalently the ``max_departures``-th flight ahead
    must depart at least ``window_seconds`` earlier). If the wake-separation
    push lands the flight inside a closure window, it is pushed further to
    that window's end — iterating because a window end may coincide with the
    next window's start. A capacity push may likewise land inside a later
    closure window, and a closure push is re-checked against capacity, so the
    two constraints iterate together until both hold. Delays cascade to
    successors. Original schedules are never rewritten; only
    :class:`TrialItem` proposals are returned.

    A **locked** flight is immovable: it must keep its ``scheduled_off``.
    When the constraints above would push a locked flight later, processing
    stops immediately — that flight is reported once as a
    :class:`TrialBlocker` (with the first conflicting constraint and the
    earliest compliant time), it is not moved, no violating time is produced,
    and no later flights receive suggestions. Only the feasible prefix is
    returned. Unlocked flights are still only ever moved later.
    """
    return _trial_on_order(
        sort_flights(flights), matrix, sort_windows(windows or []), capacity
    )


@dataclass(frozen=True, slots=True)
class AdjacentSwap:
    """The chosen adjacent swap, addressed against the baseline stable order.

    ``position`` is the 1-based slot of the first flight of the swapped pair
    in the baseline order; ``first_flight_no`` occupied that slot (and moves
    one slot later) while ``second_flight_no`` sat one slot later (and moves
    one slot earlier).
    """

    position: int
    first_flight_no: str
    second_flight_no: str


@dataclass(slots=True)
class SwapRunwayResult:
    """One runway's best-adjacent-swap optimisation outcome."""

    baseline_order: list[Flight]  # stable order the enumeration started from
    baseline: TrialRunwayResult  # trial outcome on the baseline order
    chosen_swap: AdjacentSwap | None  # None -> keeping the baseline order is best
    optimized_order: list[Flight]  # the departure sequence actually scheduled
    optimized: TrialRunwayResult  # trial outcome on the optimised order

    @property
    def saved_seconds(self) -> int:
        """Baseline total delay minus optimised total delay (>= 0)."""
        return delay_stats(self.baseline)[0] - delay_stats(self.optimized)[0]


def delay_stats(result: TrialRunwayResult) -> tuple[int, int]:
    """``(total delay seconds, max single-flight delay)`` of a trial outcome."""
    total = sum(item.delay_seconds for item in result.items)
    peak = max((item.delay_seconds for item in result.items), default=0)
    return total, peak


def best_adjacent_swap(
    flights: list[Flight],
    matrix: dict[tuple[str, str], int],
    windows: list[ClosureWindow] | None = None,
    capacity: Capacity | None = None,
) -> SwapRunwayResult:
    """Try every adjacent swap of the stable order and keep the best schedule.

    The baseline is the stable ``(scheduled_off, flight_no)`` order. Every
    candidate — the baseline itself ("no swap") plus, for each adjacent pair,
    the order with just that pair exchanged — is rescheduled from scratch
    with the usual wake-separation / closure-window / rolling-capacity
    cascade. ``scheduled_off`` values are never rewritten; only the departure
    sequence changes. Candidates are ranked lexicographically by

    1. total delay seconds (lower wins), then
    2. maximum single-flight delay (lower wins), then
    3. swap position (lower wins; "no swap" counts as position 0, so it
       always wins a tie — a swap is chosen only when it strictly improves
       the first two criteria).

    ``chosen_swap`` is ``None`` whenever the baseline order is already best
    among the candidates; ``saved_seconds`` is then zero. Locked flights are
    not supported here — the API layer rejects them with 422.
    """
    baseline_order = sort_flights(flights)
    wins = sort_windows(windows or [])
    baseline = _trial_on_order(baseline_order, matrix, wins, capacity)
    best_key = (*delay_stats(baseline), 0)
    optimized_order = baseline_order
    optimized = baseline
    chosen: AdjacentSwap | None = None
    for idx in range(len(baseline_order) - 1):
        candidate = list(baseline_order)
        candidate[idx], candidate[idx + 1] = candidate[idx + 1], candidate[idx]
        result = _trial_on_order(candidate, matrix, wins, capacity)
        key = (*delay_stats(result), idx + 1)
        if key < best_key:
            best_key = key
            optimized_order = candidate
            optimized = result
            chosen = AdjacentSwap(
                position=idx + 1,
                first_flight_no=baseline_order[idx].flight_no,
                second_flight_no=baseline_order[idx + 1].flight_no,
            )
    return SwapRunwayResult(
        baseline_order=baseline_order,
        baseline=baseline,
        chosen_swap=chosen,
        optimized_order=optimized_order,
        optimized=optimized,
    )


@dataclass(frozen=True, slots=True)
class SlotInsertResult:
    """Outcome of an insertion-slot search for one candidate flight.

    ``assigned_off`` is the earliest instant (UTC, whole seconds) at which
    the candidate can be inserted without disturbing the existing schedule;
    ``feasible`` tells whether that instant falls inside the caller's
    ``[earliest_off, latest_off]`` window (both ends included). ``position``
    is the 1-based slot in the post-insertion departure order and ``order``
    the resulting flight-number sequence.
    """

    feasible: bool
    assigned_off: datetime
    position: int
    order: tuple[str, ...]


def find_insert_slot(
    flights: list[Flight],
    candidate_flight_no: str,
    candidate_wake: str,
    earliest_off: datetime,
    latest_off: datetime,
    matrix: dict[tuple[str, str], int],
    windows: list[ClosureWindow] | None = None,
    capacity: Capacity | None = None,
) -> SlotInsertResult:
    """Earliest feasible insertion of one candidate into a feasible schedule.

    The existing schedule is left untouched: the candidate is placed at the
    earliest whole second ``t >= earliest_off`` such that, with the candidate
    inserted at the slot given by the stable ``(time, flight_no)`` ordering,
    the full post-insertion sequence keeps every adjacent wake gap, the
    candidate stays outside all closure windows and the rolling capacity
    limit holds over the whole sequence. ``feasible`` is ``True`` iff that
    earliest instant is not later than ``latest_off`` (the window is
    inclusive on both ends); when it is not, ``assigned_off`` still carries
    the earliest constraint-satisfying instant beyond the window.

    The search walks the insertion slots (before the first flight, between
    neighbours, after the last) in order. Within a slot the admissible
    instants form a contiguous range: a lower bound from the ordering, the
    predecessor's wake gap, closure-window avoidance and the capacity anchor
    (the ``max_departures``-th departure ahead), and an upper bound from the
    successor's wake gap and the packed capacity window the candidate would
    head. A slot is skipped when its lower bound exceeds its upper bound or
    when it straddles an already packed capacity window (infeasible for
    every instant). The first feasible slot yields the globally earliest
    instant, because every later slot starts at or after this slot's
    successor's time.

    Precondition: the existing schedule itself satisfies all three
    constraint families (the API answers 422 otherwise), and the candidate
    flight number differs from every existing one.
    """
    ordered = sort_flights(flights)
    wins = sort_windows(windows or [])
    n = len(ordered)
    times = [f.t for f in ordered]
    wakes = [f.wake for f in ordered]
    numbers = [f.flight_no for f in ordered]
    earliest_t = to_epoch_seconds(earliest_off)
    latest_t = to_epoch_seconds(latest_off)
    k = capacity.max_departures if capacity is not None else 0
    span = capacity.window_seconds if capacity is not None else 0

    slot = 0
    while slot <= n:
        # Fresh lower bound for this slot: the user's earliest instant and
        # the ordering (not earlier than the previous flight's time).
        t = earliest_t
        if slot > 0 and times[slot - 1] > t:
            t = times[slot - 1]
        while True:
            # Ordering tie with the previous flight: at this exact instant
            # the candidate would sort before it, so the instant itself is
            # infeasible — try one second later.
            if (
                slot > 0
                and t == times[slot - 1]
                and candidate_flight_no <= numbers[slot - 1]
            ):
                t = times[slot - 1] + 1
                continue
            # Past the next flight's time (or exactly on it while sorting
            # after it): the candidate belongs to a later slot.
            if slot < n and (
                t > times[slot]
                or (t == times[slot] and candidate_flight_no >= numbers[slot])
            ):
                break
            # Wake separation behind the previous flight.
            if slot > 0:
                bound = times[slot - 1] + required_gap(
                    matrix, wakes[slot - 1], candidate_wake
                )
                if t < bound:
                    t = bound
                    continue
            # Closure windows (sorted, non-overlapping; touching windows
            # chain exactly like the trial engine).
            pushed = False
            for window in wins:
                if t < window.s:
                    break
                if window.contains(t):
                    t = window.e
                    pushed = True
                    break
            if pushed:
                continue
            # Rolling capacity: the candidate must not become the
            # (max_departures + 1)-th departure inside the window anchored
            # at the max_departures-th flight ahead of it.
            if capacity is not None and slot >= k:
                bound = times[slot - k] + span
                if t < bound:
                    t = bound
                    continue
            # Rolling capacity: a window anchored before the candidate and
            # reaching past it (existing flights on both sides) is infeasible
            # for every instant of this slot.
            if capacity is not None:
                straddled = False
                for m in range(max(0, slot - k + 1), min(slot - 1, n - k) + 1):
                    if times[m + k - 1] - times[m] < span:
                        straddled = True
                        break
                if straddled:
                    break
            # Upper bounds: the successor's wake gap and the packed capacity
            # window the candidate would head (the user's ``latest`` is
            # applied to the result, not to the search).
            upper: int | None = None  # None = unbounded (slot after the last flight)
            if slot < n:
                upper = times[slot] - required_gap(
                    matrix, candidate_wake, wakes[slot]
                )
            if capacity is not None and slot + k <= n:
                bound = times[slot + k - 1] - span
                upper = bound if upper is None else min(upper, bound)
            if upper is None or t <= upper:
                order = (
                    tuple(numbers[:slot])
                    + (candidate_flight_no,)
                    + tuple(numbers[slot:])
                )
                return SlotInsertResult(
                    feasible=t <= latest_t,
                    assigned_off=datetime.fromtimestamp(t, tz=timezone.utc),
                    position=slot + 1,
                    order=order,
                )
            break  # slot infeasible for every remaining instant -> next slot
        slot += 1
    raise AssertionError("unreachable: the slot after the last flight is unbounded")
