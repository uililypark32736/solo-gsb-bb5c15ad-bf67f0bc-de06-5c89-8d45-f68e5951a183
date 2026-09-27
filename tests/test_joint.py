"""Unit tests for the intersecting-runway joint trial engine."""

from __future__ import annotations

from datetime import datetime, timezone

from app.joint import MutexGroup, joint_trial
from app.separation import (
    Capacity,
    ClosureWindow,
    Flight,
    build_matrix,
)

UTC = timezone.utc

ZERO_WAKE = build_matrix({("M", "M"): 0})


def dt(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def mk(
    no: str,
    t: int,
    wake: str = "M",
    locked: bool = False,
    runway: str = "36L",
) -> Flight:
    return Flight(
        flight_no=no,
        scheduled_off=dt(t),
        wake=wake,
        locked=locked,
        runway=runway,
    )


def group(runways=("36L", "18R"), interval: int = 120, name: str = "g1") -> MutexGroup:
    return MutexGroup(name=name, runways=tuple(runways), min_interval_seconds=interval)


def _by_no(result) -> dict:
    return {item.flight_no: item for item in result.items}


def test_global_order_is_stable_by_time_then_flight_no():
    payloads = {
        "36L": [mk("CA2", 200, runway="36L"), mk("CA1", 0, runway="36L")],
        "18R": [mk("MU1", 100, runway="18R")],
    }
    result = joint_trial(payloads, groups=[], matrix=ZERO_WAKE)
    assert [(r, f.flight_no) for r, f in result.order] == [
        ("36L", "CA1"),
        ("18R", "MU1"),
        ("36L", "CA2"),
    ]
    # positions: global and per-runway
    items = _by_no(result)
    assert items["CA1"].position == 1 and items["CA1"].runway_position == 1
    assert items["MU1"].position == 2 and items["MU1"].runway_position == 1
    assert items["CA2"].position == 3 and items["CA2"].runway_position == 2


def test_group_interval_pushes_later_runway_flight():
    result = joint_trial(
        {"36L": [mk("CA1", 0)], "18R": [mk("MU1", 60, runway="18R")]},
        groups=[group(interval=120)],
        matrix=ZERO_WAKE,
    )
    items = _by_no(result)
    assert items["CA1"].delay_seconds == 0
    assert items["CA1"].constraint is None
    assert items["MU1"].adjusted_time == dt(120)
    assert items["MU1"].delay_seconds == 60
    assert items["MU1"].constraint == "mutex_group"
    assert items["MU1"].mutex_group_hit is True
    assert items["MU1"].mutex_group_name == "g1"
    assert items["MU1"].mutex_group_anchor == "CA1"
    assert (
        items["MU1"].mutex_group_window_start,
        items["MU1"].mutex_group_window_end,
    ) == (dt(0), dt(120))
    assert result.blocked is None


def test_group_anchor_is_immediately_previous_group_departure_in_global_order():
    # planned: A1@0 (36L), B1@300 (18R), A2@400 (36L); interval 120.
    # A1=0; B1 stays 300; A2 group bound is B1(300)+120=420 -> pushed.
    result = joint_trial(
        {
            "36L": [mk("CA1", 0), mk("CA2", 400)],
            "18R": [mk("MU1", 300, runway="18R")],
        },
        groups=[group(interval=120)],
        matrix=ZERO_WAKE,
    )
    items = _by_no(result)
    assert items["MU1"].delay_seconds == 0
    assert items["MU1"].mutex_group_hit is False
    assert items["CA2"].adjusted_time == dt(420)
    assert items["CA2"].mutex_group_anchor == "MU1"


def test_group_push_into_closure_rechecks_until_converged():
    # group pushes MU1 to 120 which is inside 18R's closure [120,180) -> 180.
    result = joint_trial(
        {
            "36L": [mk("CA1", 0)],
            "18R": [
                mk("MU1", 60, runway="18R"),
            ],
        },
        groups=[group(interval=120)],
        matrix=ZERO_WAKE,
        windows_by_runway={"18R": [ClosureWindow(dt(120), dt(180))]},
    )
    item = _by_no(result)["MU1"]
    assert item.adjusted_time == dt(180)
    assert item.constraint == "mutex_group"  # first push wins the label
    assert item.closure_hit is True
    assert item.closure_window == ClosureWindow(dt(120), dt(180))
    assert item.mutex_group_hit is True


def test_group_push_iterates_through_touching_closures():
    result = joint_trial(
        {
            "36L": [mk("CA1", 0)],
            "18R": [mk("MU1", 60, runway="18R")],
        },
        groups=[group(interval=120)],
        matrix=ZERO_WAKE,
        windows_by_runway={
            "18R": [
                ClosureWindow(dt(120), dt(150)),
                ClosureWindow(dt(150), dt(200)),
            ]
        },
    )
    assert _by_no(result)["MU1"].adjusted_time == dt(200)


def test_same_runway_wake_and_group_both_bind_wake_priority_label():
    # J leader CA1@0, MU1@60 group-pushed to 120; CA2@130 M:
    # wake 0+180=180, group anchor MU1(120)+120=240 -> final 240.
    result = joint_trial(
        {
            "36L": [mk("CA1", 0, "J"), mk("CA2", 130, "M")],
            "18R": [mk("MU1", 60, runway="18R")],
        },
        groups=[group(interval=120)],
        matrix=build_matrix(None),
    )
    items = _by_no(result)
    assert items["MU1"].adjusted_time == dt(120)
    assert items["CA2"].adjusted_time == dt(240)
    assert items["CA2"].constraint == "wake"
    assert items["CA2"].triggered_by == "CA1"
    assert items["CA2"].mutex_group_hit is True
    assert items["CA2"].mutex_group_anchor == "MU1"


def test_capacity_and_group_iterate_together():
    # 36L: CA1@0, CA2@30, CA3@55, capacity 60s/2; 18R: MU1@0; interval 90.
    # global ties at 0 broken by flight_no: CA1 before MU1.
    # CA1=0; MU1 ->90 (group); CA2 keeps 36L order behind CA1 -> group 180;
    # CA3 keeps order behind CA2 -> group anchor CA2(180)+90=270.
    result = joint_trial(
        {
            "36L": [mk("CA1", 0), mk("CA2", 30), mk("CA3", 55)],
            "18R": [mk("MU1", 0, runway="18R")],
        },
        groups=[group(interval=90)],
        matrix=ZERO_WAKE,
        capacities_by_runway={"36L": Capacity(60, 2)},
    )
    items = _by_no(result)
    assert items["MU1"].adjusted_time == dt(90)
    assert items["CA2"].adjusted_time == dt(180)
    assert items["CA3"].adjusted_time == dt(270)
    # the group spacing keeps same-runway flights >=90s apart by itself, so
    # the 60s capacity limit never binds; order preservation (zero-gap wake)
    # is what moves CA2/CA3 first.
    assert items["CA3"].capacity_hit is False
    assert items["CA3"].mutex_group_hit is True


def test_capacity_push_happens_alongside_group():
    # Two 18R departures interleaved so capacity binds: 36L flights occupy
    # the group slots, leaving 18R's own flights less than window apart.
    # 18R: MU1@0, MU2@100 capacity 120/1; 36L: CA1@200; interval 60.
    result = joint_trial(
        {
            "18R": [
                mk("MU1", 0, runway="18R"),
                mk("MU2", 100, runway="18R"),
            ],
            "36L": [mk("CA1", 200)],
        },
        groups=[group(interval=60)],
        matrix=ZERO_WAKE,
        capacities_by_runway={"18R": Capacity(120, 1)},
    )
    items = _by_no(result)
    # MU1=0; MU2: order >=0, capacity anchor MU1(0)+120=120 -> pushed
    assert items["MU2"].adjusted_time == dt(120)
    assert items["MU2"].capacity_hit is True
    assert items["MU2"].capacity_anchor == "MU1"
    # CA1@200: group anchor MU2(120)+60=180 <=200 -> stays
    assert items["CA1"].adjusted_time == dt(200)


def test_runway_without_group_member_is_independent():
    result = joint_trial(
        {"36L": [mk("CA1", 0)], "09": [mk("CZ1", 10, runway="09")]},
        groups=[group(("36L", "18R"), 120)],
        matrix=ZERO_WAKE,
    )
    items = _by_no(result)
    assert items["CZ1"].adjusted_time == dt(10)
    assert items["CZ1"].mutex_group_hit is False
    assert items["CZ1"].mutex_group_name is None


def test_multiple_groups_do_not_interact():
    # g1: 36L/18R interval 120; g2: 09/27 interval 60.
    result = joint_trial(
        {
            "36L": [mk("CA1", 0)],
            "18R": [mk("MU1", 60, runway="18R")],
            "09": [mk("CZ1", 0, runway="09")],
            "27": [mk("SC1", 30, runway="27")],
        },
        groups=[
            group(("36L", "18R"), 120, "g1"),
            group(("09", "27"), 60, "g2"),
        ],
        matrix=ZERO_WAKE,
    )
    items = _by_no(result)
    assert items["MU1"].adjusted_time == dt(120)
    assert items["SC1"].adjusted_time == dt(60)


def test_locked_flight_feasible_keeps_schedule():
    result = joint_trial(
        {
            "36L": [mk("CA1", 0, "J")],
            "18R": [mk("MU1", 300, "M", locked=True, runway="18R")],
        },
        groups=[group(interval=120)],
        matrix=build_matrix(None),
    )
    assert result.blocked is None
    item = _by_no(result)["MU1"]
    assert item.adjusted_time == dt(300)
    assert item.delay_seconds == 0
    assert item.locked is True


def test_locked_flight_blocked_by_group_reports_required_times():
    result = joint_trial(
        {
            "36L": [mk("CA1", 0)],
            "18R": [mk("MU1", 100, locked=True, runway="18R")],
        },
        groups=[group(interval=120)],
        matrix=ZERO_WAKE,
    )
    assert [i.flight_no for i in result.items] == ["CA1"]
    blocked = result.blocked
    assert blocked is not None
    assert blocked.flight_no == "MU1"
    assert blocked.position == 2 and blocked.runway_position == 1
    assert blocked.constraint == "mutex_group"
    assert blocked.required_earliest_off == dt(120)
    assert blocked.mutex_group_hit is True
    assert blocked.mutex_group_anchor == "CA1"
    assert blocked.required_times["mutex_group"] is not None
    assert blocked.required_times["mutex_group"]["required_off"] == dt(120)
    assert blocked.required_times["mutex_group"]["triggered_by"] == "CA1"
    assert blocked.required_times["wake"] is None
    assert blocked.required_times["closure"] is None
    assert blocked.required_times["capacity"] is None


def test_locked_blocker_names_wake_first_and_lists_all_required_times():
    # CA1 J@0 -> MU1@60 group -> 120; CA2 M@100 locked:
    # wake 180, group MU1(120)+120=240 -> blocked at 240; label wake.
    result = joint_trial(
        {
            "36L": [mk("CA1", 0, "J"), mk("CA2", 100, "M", locked=True)],
            "18R": [mk("MU1", 60, runway="18R")],
        },
        groups=[group(interval=120)],
        matrix=build_matrix(None),
    )
    blocked = result.blocked
    assert blocked is not None and blocked.flight_no == "CA2"
    assert blocked.constraint == "wake"
    assert blocked.triggered_by == "CA1"
    assert blocked.required_gap_seconds == 180
    assert blocked.required_earliest_off == dt(240)
    assert blocked.required_times["wake"]["required_off"] == dt(180)
    assert blocked.required_times["mutex_group"]["required_off"] == dt(240)
    assert blocked.required_times["mutex_group"]["triggered_by"] == "MU1"
    assert blocked.required_times["capacity"] is None
    # the blocker and nothing after it is scheduled
    assert [i.flight_no for i in result.items] == ["CA1", "MU1"]


def test_locked_blocker_by_closure_reports_window_end():
    result = joint_trial(
        {
            "36L": [mk("CA1", 0)],
            "18R": [mk("MU1", 100, locked=True, runway="18R")],
        },
        groups=[group(interval=60)],  # group bound 60 does not move MU1
        matrix=ZERO_WAKE,
        windows_by_runway={"18R": [ClosureWindow(dt(100), dt(200))]},
    )
    blocked = result.blocked
    assert blocked is not None
    assert blocked.constraint == "closure"
    assert blocked.required_earliest_off == dt(200)
    assert blocked.required_times["closure"]["required_off"] == dt(200)
    # group requirement is still reported even though it alone did not bind
    assert blocked.required_times["mutex_group"]["required_off"] == dt(60)


def test_locked_blocker_by_capacity_lists_capacity_required_time():
    result = joint_trial(
        {
            "36L": [
                mk("CA1", 0),
                mk("CA2", 10),
                mk("CA3", 20, locked=True),
            ]
        },
        groups=[],
        matrix=ZERO_WAKE,
        capacities_by_runway={"36L": Capacity(60, 2)},
    )
    blocked = result.blocked
    assert blocked is not None and blocked.constraint == "capacity"
    assert blocked.required_earliest_off == dt(60)
    assert blocked.required_times["capacity"]["required_off"] == dt(60)
    assert blocked.required_times["capacity"]["anchor_flight_no"] == "CA1"
    assert blocked.required_times["wake"] is not None  # predecessor exists
    assert blocked.required_times["mutex_group"] is None


def test_blocking_locked_flight_stops_all_runways_prefix():
    # CA1@0, then MU1@60 locked on 18R would need group 120 -> blocked;
    # CA2 (later global position) must never be suggested.
    result = joint_trial(
        {
            "36L": [mk("CA1", 0), mk("CA2", 1000)],
            "18R": [mk("MU1", 60, locked=True, runway="18R")],
        },
        groups=[group(interval=120)],
        matrix=ZERO_WAKE,
    )
    assert [i.flight_no for i in result.items] == ["CA1"]
    assert result.blocked.flight_no == "MU1"


# Allow running without pytest installed (the CI/Docker environment uses
# pytest normally; this runner is only for dependency-light smoke checks).
if __name__ == "__main__":
    import sys

    failures = 0
    for _name, _fn in sorted(list(globals().items())):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
            except Exception as exc:  # pragma: no cover - smoke runner
                failures += 1
                print(f"FAIL {_name}: {exc!r}")
            else:
                print(f"ok   {_name}")
    sys.exit(1 if failures else 0)
