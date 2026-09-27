"""Tests for the slot-insertion engine and the slots/insert API."""

from __future__ import annotations

from datetime import datetime, timezone

from app.separation import (
    Capacity,
    ClosureWindow,
    Flight,
    build_matrix,
    find_insert_slot,
)

UTC = timezone.utc


def dt(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def make(no: str, t: int, wake: str) -> Flight:
    return Flight(flight_no=no, scheduled_off=dt(t), wake=wake)


def window(start: int, end: int) -> ClosureWindow:
    return ClosureWindow(start=dt(start), end=dt(end))


MATRIX = build_matrix(None)
ZERO = build_matrix({("M", "M"): 0})  # custom matrix: every pair falls back to 0


def insert(flights, no, wake, earliest, latest, matrix=MATRIX, windows=None, capacity=None):
    return find_insert_slot(
        flights, no, wake, dt(earliest), dt(latest), matrix,
        windows=windows, capacity=capacity,
    )


# ---------- domain: wake separation ----------


def test_insert_between_flights_respects_wake_gap():
    # J@0, M@600; candidate M needs 180s behind J -> 03:00, and 60s ahead of
    # the M@600 follower is satisfied (600-180=420 >= 180... checked exactly).
    flights = [make("CA1", 0, "J"), make("CA2", 600, "M")]
    r = insert(flights, "CA9", "M", 60, 3600)
    assert r.feasible
    assert r.assigned_off == dt(180)  # J->M = 180s
    assert r.position == 2
    assert r.order == ("CA1", "CA9", "CA2")


def test_insert_before_first_flight():
    flights = [make("CA1", 600, "M")]
    r = insert(flights, "CA9", "M", 0, 3600)
    assert r.feasible
    assert r.assigned_off == dt(0)
    assert r.position == 1
    assert r.order == ("CA9", "CA1")


def test_insert_after_last_flight_pushed_by_wake():
    flights = [make("CA1", 0, "J")]
    r = insert(flights, "CA9", "M", 60, 3600)
    assert r.feasible
    assert r.assigned_off == dt(180)  # J->M = 180s
    assert r.position == 2
    assert r.order == ("CA1", "CA9")


def test_candidate_wake_gap_towards_successor_moves_slot():
    # Candidate J before M@100: J->M needs 180s, so the candidate cannot
    # depart at t in [100-180+1..] i.e. t <= -80 required -> slot before the
    # M is infeasible for t >= 0; it must go after the M (M->J = 60s).
    flights = [make("CA1", 100, "M")]
    r = insert(flights, "CA9", "J", 0, 3600)
    assert r.feasible
    assert r.assigned_off == dt(160)
    assert r.position == 2
    assert r.order == ("CA1", "CA9")


def test_ordering_tie_breaks_by_flight_number():
    flights = [make("CB1", 100, "M")]
    # zero wake gap so the tie-break alone decides the slot
    r = insert(flights, "AA1", "M", 100, 3600, matrix=ZERO)
    assert r.feasible
    assert (r.assigned_off, r.position) == (dt(100), 1)
    r = insert(flights, "ZZ1", "M", 100, 3600, matrix=ZERO)
    assert r.feasible
    assert (r.assigned_off, r.position) == (dt(100), 2)
    assert r.order == ("CB1", "ZZ1")


def test_inclusive_window_bounds():
    flights = [make("CA1", 0, "J")]
    # latest exactly at the earliest feasible instant -> still feasible
    r = insert(flights, "CA9", "M", 60, 180)
    assert r.feasible and r.assigned_off == dt(180)
    # one second earlier -> no slot
    r = insert(flights, "CA9", "M", 60, 179)
    assert not r.feasible
    assert r.assigned_off == dt(180)  # global earliest still reported


def test_no_slot_reports_global_earliest():
    flights = [make("CA1", 0, "J"), make("CA2", 600, "M")]
    r = insert(flights, "CA9", "M", 0, 179)
    assert not r.feasible
    assert r.assigned_off == dt(180)


# ---------- domain: closure windows ----------


def test_closure_window_pushes_candidate_to_window_end():
    flights = [make("CA1", 0, "M")]
    r = insert(flights, "CA9", "M", 100, 3600, windows=[window(100, 200)])
    assert r.feasible
    assert r.assigned_off == dt(200)


def test_chained_touching_windows():
    flights = [make("CA1", 0, "M")]
    r = insert(
        flights, "CA9", "M", 100, 3600,
        windows=[window(100, 200), window(200, 300)],
    )
    assert r.feasible
    assert r.assigned_off == dt(300)


def test_window_end_is_departable():
    # half-open [start, end): departing exactly at end is allowed
    flights = [make("CA1", 0, "M")]
    r = insert(flights, "CA9", "M", 200, 3600, windows=[window(100, 200)])
    assert r.feasible
    assert r.assigned_off == dt(200)


# ---------- domain: rolling capacity ----------


def test_capacity_lower_bound_pushes_candidate():
    # 2 per 600s; CA1@0, CA2@300 -> third departure no earlier than 0+600
    flights = [make("CA1", 0, "M"), make("CA2", 300, "M")]
    cap = Capacity(window_seconds=600, max_departures=2)
    r = insert(flights, "CA9", "M", 100, 3600, matrix=ZERO, capacity=cap)
    assert r.feasible
    assert r.assigned_off == dt(600)
    assert r.position == 3


def test_capacity_straddling_window_skips_slot():
    # 2 per 600s; CA1@0, CA2@300: any candidate between them completes a
    # 3-departure window anchored at CA1 -> the middle slot is infeasible.
    flights = [make("CA1", 0, "M"), make("CA2", 300, "M")]
    cap = Capacity(window_seconds=600, max_departures=2)
    r = insert(flights, "CA9", "M", 0, 3600, matrix=ZERO, capacity=cap)
    assert r.feasible
    assert r.assigned_off == dt(600)
    assert r.position == 3
    assert r.order == ("CA1", "CA2", "CA9")


def test_capacity_lower_bound_after_successor():
    # 2 per 600s; CA1@0, CA2@700 (baseline exactly feasible: 700 >= 600).
    flights = [make("CA1", 0, "M"), make("CA2", 700, "M")]
    cap = Capacity(window_seconds=600, max_departures=2)
    # between the flights the candidate fits as soon as it clears CA1's window
    r = insert(flights, "CA9", "M", 650, 3600, matrix=ZERO, capacity=cap)
    assert r.feasible
    assert (r.assigned_off, r.position) == (dt(650), 2)
    assert r.order == ("CA1", "CA9", "CA2")
    # after CA2 the candidate is anchored at CA1 (0 + 600); 701 already clears it
    r = insert(flights, "CA9", "M", 701, 3600, matrix=ZERO, capacity=cap)
    assert r.feasible
    assert (r.assigned_off, r.position) == (dt(701), 3)
    assert r.order == ("CA1", "CA2", "CA9")


def test_capacity_window_end_is_departable():
    # half-open window: departing exactly at anchor+window_seconds is allowed
    flights = [make("CA1", 0, "M"), make("CA2", 300, "M")]
    cap = Capacity(window_seconds=600, max_departures=2)
    r = insert(flights, "CA9", "M", 600, 3600, matrix=ZERO, capacity=cap)
    assert r.feasible
    assert r.assigned_off == dt(600)


# ---------- domain: combined constraints ----------


def test_wake_push_into_closure_iterates():
    # J@0 pushes the M candidate to 180, which lies inside [150, 300)
    flights = [make("CA1", 0, "J")]
    r = insert(flights, "CA9", "M", 0, 3600, windows=[window(150, 300)])
    assert r.feasible
    assert r.assigned_off == dt(300)


def test_existing_schedule_untouched_by_insert():
    flights = [make("CA1", 0, "J"), make("CA2", 600, "M")]
    r = insert(flights, "CA9", "M", 60, 3600)
    assert r.feasible
    # existing flights keep their positions/times; only the candidate is added
    assert r.order[:1] == ("CA1",) and r.order[2:] == ("CA2",)
    assert flights[0].t == 0 and flights[1].t == 600


# ---------- domain: brute-force cross-check ----------


def _sequence_ok(seq, matrix, wins, capacity):
    from app.separation import required_gap

    for a, b in zip(seq, seq[1:]):
        if b.t - a.t < required_gap(matrix, a.wake, b.wake):
            return False
    for f in seq:
        for w in wins:
            if w.contains(f.t):
                return False
    if capacity is not None:
        ts = [f.t for f in seq]
        for p in range(capacity.max_departures, len(ts)):
            if ts[p] - ts[p - capacity.max_departures] < capacity.window_seconds:
                return False
    return True


def _brute_force(flights, no, wake, earliest, latest, matrix, wins, capacity):
    """Second-by-second scan: earliest feasible instant in [earliest, latest]."""
    from app.separation import sort_flights

    ordered = sort_flights(flights)
    for t in range(earliest, latest + 1):
        seq = list(ordered)
        slot = 0
        while slot < len(seq) and (t, no) > (seq[slot].t, seq[slot].flight_no):
            slot += 1
        seq.insert(slot, Flight(flight_no=no, scheduled_off=dt(t), wake=wake))
        if _sequence_ok(seq, matrix, wins, capacity):
            return t, slot
    return None


def test_engine_matches_brute_force_on_random_feasible_schedules():
    import random

    from app.separation import sort_windows

    rng = random.Random(20260926)
    trials = 0
    while trials < 400:
        n = rng.randint(1, 6)
        flights = []
        t = 0
        for j in range(n):
            t += rng.choice([0, 30, 60, 90, 120, 200])
            flights.append(make(f"F{j:02d}", t, rng.choice("LMHJ")))
        matrix = MATRIX if rng.random() < 0.5 else ZERO
        wins = []
        if rng.random() < 0.4:
            s = rng.choice([f.t for f in flights]) + rng.choice([-60, 0, 60])
            wins = [window(s, s + rng.choice([60, 120]))]
        capacity = (
            Capacity(window_seconds=120, max_departures=rng.choice([2, 3]))
            if rng.random() < 0.5
            else None
        )
        wins = sort_windows(wins)
        if not _sequence_ok(flights, matrix, wins, capacity):
            continue  # engine precondition: feasible existing schedule
        trials += 1
        earliest = rng.choice([-120, 0, 60, 300])
        latest = earliest + rng.choice([0, 60, 300, 900])
        no = rng.choice(["AA", "C0", "C3", "ZZ"])
        wake = rng.choice("LMHJ")
        result = find_insert_slot(
            flights, no, wake, dt(earliest), dt(latest), matrix,
            windows=wins, capacity=capacity,
        )
        want = _brute_force(flights, no, wake, earliest, latest, matrix, wins, capacity)
        if want is None:
            assert not result.feasible
            # the reported global earliest must itself be feasible
            unbounded = _brute_force(
                flights, no, wake, earliest, latest + 5000, matrix, wins, capacity
            )
            if unbounded is not None:
                assert result.assigned_off == dt(unbounded[0])
        else:
            assert result.feasible
            assert (result.assigned_off, result.position) == (dt(want[0]), want[1] + 1)


# ---------- API ----------


def _api_body(**overrides):
    body = {
        "runway": {
            "runway": "36L",
            "flights": [
                {"flight_no": "CA1", "scheduled_off": "2026-09-18T00:00:00Z", "wake": "J"},
                {"flight_no": "CA2", "scheduled_off": "2026-09-18T00:05:00Z", "wake": "M"},
            ],
        },
        "candidate": {
            "flight_no": "CA9",
            "wake": "M",
            "earliest_off": "2026-09-18T00:03:00Z",
            "latest_off": "2026-09-18T00:10:00Z",
        },
    }
    body.update(overrides)
    return body


def test_api_insert_happy_path(client):
    r = client.post("/api/v1/slots/insert", json=_api_body())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True
    assert data["assigned_off"] == "2026-09-18T00:03:00Z"  # J->M = 180s
    assert data["earliest_feasible_off"] == "2026-09-18T00:03:00Z"
    assert data["insert_position"] == 2
    assert data["inserted_order"] == ["CA1", "CA9", "CA2"]
    assert data["runway"] == "36L"
    assert data["candidate_flight_no"] == "CA9"
    assert data["matrix"]["J"]["M"] == 180
    # read-only query: no batch is created
    assert "batch_id" not in data


def test_api_insert_no_slot(client):
    body = _api_body()
    body["candidate"]["earliest_off"] = "2026-09-18T00:00:00Z"
    body["candidate"]["latest_off"] = "2026-09-18T00:02:59Z"
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is False
    assert "无可行空档" in data["reason"]
    assert data["earliest_feasible_off"] == "2026-09-18T00:03:00Z"
    assert data["insert_position"] is None
    assert data["assigned_off"] is None
    assert data["inserted_order"] is None


def test_api_insert_timezone_normalised(client):
    body = _api_body()
    body["runway"] = {
        "runway": "18R",
        "flights": [
            {"flight_no": "MU1", "scheduled_off": "2026-09-18T08:00:00+08:00", "wake": "H"}
        ],
    }
    body["candidate"] = {
        "flight_no": "MU9",
        "wake": "H",
        "earliest_off": "2026-09-18T08:05:00+08:00",
        "latest_off": "2026-09-18T09:00:00+08:00",
    }
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["assigned_off"] == "2026-09-18T00:05:00Z"
    assert data["earliest_off"] == "2026-09-18T00:05:00Z"
    assert data["latest_off"] == "2026-09-18T01:00:00Z"
    assert data["inserted_order"] == ["MU1", "MU9"]


def test_api_insert_capacity_and_closure(client):
    body = _api_body()
    body["runway"]["capacity"] = {"window_seconds": 600, "max_departures": 2}
    body["runway"]["closures"] = [
        {"start": "2026-09-18T00:10:00Z", "end": "2026-09-18T00:20:00Z"}
    ]
    body["candidate"]["latest_off"] = "2026-09-18T00:30:00Z"
    body["matrix"] = [{"leader": "J", "follower": "M", "seconds": 0}]
    # zero wake gaps; capacity (2 per 600s) pushes the third departure to
    # 00:10:00, which falls inside the closure -> 00:20:00
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["feasible"] is True
    assert data["assigned_off"] == "2026-09-18T00:20:00Z"
    assert data["insert_position"] == 3
    assert data["inserted_order"] == ["CA1", "CA2", "CA9"]


def test_api_baseline_wake_violation_rejected(client):
    body = _api_body()
    body["runway"]["flights"][1]["scheduled_off"] = "2026-09-18T00:00:30Z"
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 422
    assert "尾流间隔违规" in r.json()["detail"]


def test_api_baseline_closure_conflict_rejected(client):
    body = _api_body()
    body["runway"]["closures"] = [
        {"start": "2026-09-18T00:04:00Z", "end": "2026-09-18T00:06:00Z"}
    ]
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 422
    assert "关闭窗口冲突" in r.json()["detail"]


def test_api_baseline_capacity_violation_rejected(client):
    body = _api_body()
    body["runway"]["flights"].append(
        {"flight_no": "CA3", "scheduled_off": "2026-09-18T00:06:00Z", "wake": "M"}
    )
    body["runway"]["capacity"] = {"window_seconds": 600, "max_departures": 2}
    body["matrix"] = [{"leader": "J", "follower": "M", "seconds": 0}]
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 422
    assert "滚动容量超限" in r.json()["detail"]


def test_api_candidate_flight_no_must_be_unique(client):
    body = _api_body()
    body["candidate"]["flight_no"] = "CA1"
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 422
    assert "唯一" in r.text


def test_api_earliest_after_latest_rejected(client):
    body = _api_body()
    body["candidate"]["latest_off"] = "2026-09-18T00:02:59Z"
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 422
    assert "最早离场时刻不能晚于最晚" in r.text


def test_api_naive_datetime_rejected(client):
    body = _api_body()
    body["candidate"]["earliest_off"] = "2026-09-18T00:03:00"
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 422
    assert "时区" in r.text


def test_api_subsecond_precision_rejected(client):
    body = _api_body()
    body["candidate"]["earliest_off"] = "2026-09-18T00:03:00.500Z"
    r = client.post("/api/v1/slots/insert", json=body)
    assert r.status_code == 422
    assert "整秒" in r.text


def test_api_invalid_wake_and_extra_field_rejected(client):
    body = _api_body()
    body["candidate"]["wake"] = "X"
    assert client.post("/api/v1/slots/insert", json=body).status_code == 422
    body = _api_body()
    body["candidate"]["locked"] = True
    assert client.post("/api/v1/slots/insert", json=body).status_code == 422


def test_api_insert_does_not_touch_batches(client):
    # a validate batch created before the insert stays fetchable and unchanged
    validate_body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    {"flight_no": "CA1", "scheduled_off": "2026-09-18T00:00:00Z", "wake": "H"}
                ],
            }
        ]
    }
    created = client.post("/api/v1/validate", json=validate_body).json()
    client.post("/api/v1/slots/insert", json=_api_body())
    fetched = client.get(f"/api/v1/batches/{created['batch_id']}").json()
    assert fetched == {**created, "created_at": fetched["created_at"]}
