"""End-to-end API tests for the intersecting-runway joint trial endpoint."""

from __future__ import annotations


def _flight(no: str, off: str, wake: str = "M", locked=None) -> dict:
    body = {"flight_no": no, "scheduled_off": off, "wake": wake}
    if locked is not None:
        body["locked"] = locked
    return body


def _body() -> dict:
    return {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "H"),
                    _flight("CA2", "2026-09-18T00:02:00Z", "M"),
                ],
            },
            {
                "runway": "18R",
                "flights": [
                    _flight("MU1", "2026-09-18T00:00:30Z", "H"),
                ],
            },
        ],
        "groups": [
            {"name": "cross", "runways": ["36L", "18R"], "min_interval_seconds": 90}
        ],
    }


def test_joint_trial_global_order_and_group_push(client):
    r = client.post("/api/v1/trial/joint", json=_body())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mode"] == "joint_trial"
    assert data["total_runways"] == 2
    assert data["total_flights"] == 3
    assert data["groups"] == [
        {"name": "cross", "runways": ["36L", "18R"], "min_interval_seconds": 90}
    ]
    # global stable order: CA1@00:00:00, MU1@00:00:30, CA2@00:02:00
    assert data["order"] == [
        {"runway": "36L", "flight_no": "CA1"},
        {"runway": "18R", "flight_no": "MU1"},
        {"runway": "36L", "flight_no": "CA2"},
    ]
    rw36 = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    rw18 = next(rw for rw in data["runways"] if rw["runway"] == "18R")
    assert rw36["mutex_group"] == "cross"
    assert rw18["mutex_group"] == "cross"
    # legacy fields absent until a lock is supplied
    assert "locked_flights" not in rw36
    assert "blocked" not in rw36

    items = {i["flight_no"]: i for i in rw36["items"]}
    assert items["CA1"]["position"] == 1
    assert items["CA1"]["runway_position"] == 1
    assert items["CA1"]["adjusted_off"] == "2026-09-18T00:00:00Z"
    assert items["CA1"]["constraint"] is None
    assert items["CA2"]["runway_position"] == 2
    # H->M 120s same-runway gap binds (02:00 -> 02:00 exactly); MU1 group:
    mu = {i["flight_no"]: i for i in rw18["items"]}["MU1"]
    assert mu["position"] == 2 and mu["runway_position"] == 1
    # group anchor CA1 -> pushed from 00:30 to 00:90
    assert mu["adjusted_off"] == "2026-09-18T00:01:30Z"
    assert mu["constraint"] == "mutex_group"
    assert mu["mutex_group_hit"] is True
    assert mu["mutex_group"] == "cross"
    assert mu["mutex_group_anchor"] == "CA1"
    assert mu["mutex_group_window"] == {
        "start": "2026-09-18T00:00:00Z",
        "end": "2026-09-18T00:01:30Z",
    }
    # CA2: wake 02:00 stays; group anchor MU1(01:30)+90=03:00 -> 03:00
    assert items["CA2"]["adjusted_off"] == "2026-09-18T00:03:00Z"
    assert items["CA2"]["constraint"] == "mutex_group"
    assert items["CA2"]["mutex_group_anchor"] == "MU1"
    # MU1 delay 60s (00:30 -> 01:30), CA2 delay 60s (02:00 -> 03:00)
    assert data["total_delay_seconds"] == 120
    assert data["max_delay_seconds"] == 60
    assert data["total_delayed"] == 2


def test_joint_trial_without_groups_is_independent(client):
    body = _body()
    del body["groups"]
    data = client.post("/api/v1/trial/joint", json=body).json()
    assert data["groups"] == []
    rw18 = next(rw for rw in data["runways"] if rw["runway"] == "18R")
    item = rw18["items"][0]
    # no group spacing: MU1 keeps 00:30 despite being 30s behind CA1 on 36L
    assert item["adjusted_off"] == "2026-09-18T00:00:30Z"
    assert item["constraint"] is None
    assert "mutex_group_hit" not in item
    assert "mutex_group" not in rw18


def test_joint_trial_group_auto_named_when_name_omitted(client):
    body = _body()
    body["groups"] = [{"runways": ["36L", "18R"], "min_interval_seconds": 90}]
    data = client.post("/api/v1/trial/joint", json=body).json()
    assert data["groups"][0]["name"] == "group-1"
    rw36 = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    assert rw36["mutex_group"] == "group-1"
    assert rw36["items"][0]["mutex_group"] == "group-1"


def test_joint_trial_persists_and_batch_roundtrips(client):
    posted = client.post("/api/v1/trial/joint", json=_body()).json()
    got = client.get(f"/api/v1/batches/{posted['batch_id']}")
    assert got.status_code == 200
    data = got.json()
    assert data["mode"] == "joint_trial"
    assert data["groups"] == posted["groups"]
    assert data["order"] == posted["order"]
    assert data["total_delay_seconds"] == posted["total_delay_seconds"]
    rw36 = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    assert rw36["total"] == 2
    assert [i["flight_no"] for i in rw36["items"]] == ["CA1", "CA2"]
    assert rw36["items"][1]["runway_position"] == 2
    rw18 = next(rw for rw in data["runways"] if rw["runway"] == "18R")
    assert rw18["items"][0]["mutex_group_anchor"] == "CA1"
    assert "locked_flights" not in rw36


def test_joint_trial_rejects_unknown_runway_in_group(client):
    body = _body()
    body["groups"][0]["runways"] = ["36L", "09"]
    r = client.post("/api/v1/trial/joint", json=body)
    assert r.status_code == 422
    assert "不存在的跑道" in r.text


def test_joint_trial_rejects_runway_in_two_groups(client):
    body = _body()
    body["groups"] = [
        {"name": "g1", "runways": ["36L", "18R"], "min_interval_seconds": 60},
        {"name": "g2", "runways": ["36L", "18R"], "min_interval_seconds": 30},
    ]
    r = client.post("/api/v1/trial/joint", json=body)
    assert r.status_code == 422
    assert "多个互斥跑道组" in r.text


def test_joint_trial_rejects_duplicate_runway_within_group(client):
    body = _body()
    body["groups"][0]["runways"] = ["36L", "36L"]
    r = client.post("/api/v1/trial/joint", json=body)
    assert r.status_code == 422


def test_joint_trial_rejects_group_with_single_runway(client):
    body = _body()
    body["groups"][0]["runways"] = ["36L"]
    r = client.post("/api/v1/trial/joint", json=body)
    assert r.status_code == 422


def test_joint_trial_rejects_non_positive_interval(client):
    for bad in (0, -10, 60.0, "60", True):
        body = _body()
        body["groups"][0]["min_interval_seconds"] = bad
        r = client.post("/api/v1/trial/joint", json=body)
        assert r.status_code == 422, bad


def test_joint_trial_rejects_duplicate_group_names(client):
    body = _body()
    body["runways"].append(
        {"runway": "09", "flights": [_flight("CZ1", "2026-09-18T01:00:00Z")]}
    )
    body["groups"] = [
        {"name": "same", "runways": ["36L", "18R"], "min_interval_seconds": 60},
        {"name": "same", "runways": ["09"], "min_interval_seconds": 60},
    ]
    # single-runway group is itself invalid; use two runways for g2 instead
    body["runways"].append(
        {"runway": "27", "flights": [_flight("SC1", "2026-09-18T01:01:00Z")]}
    )
    body["groups"][1]["runways"] = ["09", "27"]
    r = client.post("/api/v1/trial/joint", json=body)
    assert r.status_code == 422
    assert "组名称重复" in r.text


def test_joint_trial_rejects_unknown_group_field(client):
    body = _body()
    body["groups"][0]["extra"] = 1
    r = client.post("/api/v1/trial/joint", json=body)
    assert r.status_code == 422


def test_joint_trial_blocked_locked_flight_stops_prefix_and_persists(client):
    body = _body()
    # MU1 locked at 00:30: group requires 01:30 -> blocked.
    body["runways"][1]["flights"][0] = _flight(
        "MU1", "2026-09-18T00:00:30Z", "H", locked=True
    )
    # CA2 (02:00) is later in global order -> must not be suggested.
    r = client.post("/api/v1/trial/joint", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total_locked"] == 1
    rw36 = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    rw18 = next(rw for rw in data["runways"] if rw["runway"] == "18R")
    # only the feasible global prefix: CA1 on 36L; CA2 is after the blocker
    assert [i["flight_no"] for i in rw36["items"]] == ["CA1"]
    assert rw18["items"] == []
    assert [f["flight_no"] for f in rw18["locked_flights"]] == ["MU1"]
    blocked = rw18["blocked"]
    assert blocked["flight_no"] == "MU1"
    assert blocked["position"] == 2
    assert blocked["runway_position"] == 1
    assert blocked["constraint"] == "mutex_group"
    assert blocked["required_earliest_off"] == "2026-09-18T00:01:30Z"
    assert blocked["triggered_by"] is None
    assert blocked["required_gap_seconds"] is None
    assert blocked["closure_hit"] is False
    assert blocked["closure_window"] is None
    assert blocked["mutex_group_hit"] is True
    assert blocked["mutex_group"] == "cross"
    assert blocked["mutex_group_anchor"] == "CA1"
    assert blocked["mutex_group_window"] == {
        "start": "2026-09-18T00:00:00Z",
        "end": "2026-09-18T00:01:30Z",
    }
    required = blocked["required_times"]
    assert set(required) == {"wake", "closure", "capacity", "mutex_group"}
    assert required["wake"] is None
    assert required["closure"] is None
    assert required["capacity"] is None
    assert required["mutex_group"] == {
        "required_off": "2026-09-18T00:01:30Z",
        "triggered_by": "CA1",
        "group": "cross",
        "min_interval_seconds": 90,
        "window": {
            "start": "2026-09-18T00:00:00Z",
            "end": "2026-09-18T00:01:30Z",
        },
    }
    # 36L is feasible and carried no lock input -> no blocked key is emitted
    assert "blocked" not in rw36

    # prefix and blocker persist for batch retrieval
    got = client.get(f"/api/v1/batches/{data['batch_id']}").json()
    g36 = next(rw for rw in got["runways"] if rw["runway"] == "36L")
    g18 = next(rw for rw in got["runways"] if rw["runway"] == "18R")
    assert [i["flight_no"] for i in g36["items"]] == ["CA1"]
    assert g18["items"] == []
    assert g18["blocked"]["flight_no"] == "MU1"
    assert g18["blocked"]["runway_position"] == 1
    assert (
        g18["blocked"]["required_times"]["mutex_group"]["required_off"]
        == "2026-09-18T00:01:30Z"
    )
    assert got["total_locked"] == 1


def test_joint_trial_blocked_reports_each_constraint_required_time(client):
    # 36L: J CA1@00:00, M CA2@00:01 locked (J->M 180); 18R: H MU1@00:00:30.
    # global order: CA1, MU1 (group 90 -> 01:30), CA2 locked:
    # wake requires 03:00 (CA1+180), group requires 03:00 (MU1+90).
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _flight("CA2", "2026-09-18T00:01:00Z", "M", locked=True),
                ],
            },
            {
                "runway": "18R",
                "flights": [_flight("MU1", "2026-09-18T00:00:30Z", "H")],
            },
        ],
        "groups": [
            {"name": "cross", "runways": ["36L", "18R"], "min_interval_seconds": 90}
        ],
    }
    data = client.post("/api/v1/trial/joint", json=body).json()
    rw36 = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    blocked = rw36["blocked"]
    assert blocked["flight_no"] == "CA2"
    assert blocked["constraint"] == "wake"
    assert blocked["triggered_by"] == "CA1"
    assert blocked["required_gap_seconds"] == 180
    assert blocked["required_earliest_off"] == "2026-09-18T00:03:00Z"
    required = blocked["required_times"]
    assert required["wake"] == {
        "required_off": "2026-09-18T00:03:00Z",
        "triggered_by": "CA1",
        "required_gap_seconds": 180,
    }
    assert required["mutex_group"]["required_off"] == "2026-09-18T00:03:00Z"
    assert required["mutex_group"]["triggered_by"] == "MU1"
    assert required["closure"] is None
    assert required["capacity"] is None


def test_joint_trial_lock_fields_follow_explicit_locked_flag(client):
    body = _body()
    body["runways"][0]["flights"][0] = _flight(
        "CA1", "2026-09-18T00:00:00Z", "H", locked=False
    )
    data = client.post("/api/v1/trial/joint", json=body).json()
    assert data["total_locked"] == 0
    rw36 = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    assert rw36["locked_flights"] == []
    assert rw36["blocked"] is None
    assert rw36["items"][0]["locked"] is False
    rw18 = next(rw for rw in data["runways"] if rw["runway"] == "18R")
    # 18R never carried locked -> its items omit the key entirely
    assert "locked" not in rw18["items"][0]


def test_joint_trial_respects_closures_and_capacity_flags(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z"),
                    _flight("CA2", "2026-09-18T00:00:20Z"),
                    _flight("CA3", "2026-09-18T00:00:40Z"),
                ],
                "capacity": {"window_seconds": 120, "max_departures": 1},
            },
            {
                "runway": "18R",
                "flights": [
                    _flight("MU1", "2026-09-18T00:01:00Z"),
                ],
                "closures": [
                    {
                        "start": "2026-09-18T00:02:00Z",
                        "end": "2026-09-18T00:02:30Z",
                    }
                ],
            },
        ],
        "groups": [
            {"name": "cross", "runways": ["36L", "18R"], "min_interval_seconds": 60}
        ],
        "matrix": [{"leader": "M", "follower": "M", "seconds": 0}],
    }
    data = client.post("/api/v1/trial/joint", json=body).json()
    rw36 = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    items = {i["flight_no"]: i for i in rw36["items"]}
    # CA1=0; CA2 capacity 0+120=120; CA3 capacity anchor CA2 120+120=240
    assert items["CA1"]["adjusted_off"] == "2026-09-18T00:00:00Z"
    assert items["CA2"]["adjusted_off"] == "2026-09-18T00:02:00Z"
    assert items["CA2"]["capacity_hit"] is True
    assert items["CA2"]["capacity_anchor"] == "CA1"
    assert items["CA3"]["adjusted_off"] == "2026-09-18T00:04:00Z"
    rw18 = next(rw for rw in data["runways"] if rw["runway"] == "18R")
    mu = rw18["items"][0]
    # group anchor CA3(04:00)+60=05:00, scheduled 01:00 -> 05:00, no closure
    assert mu["adjusted_off"] == "2026-09-18T00:05:00Z"
    assert mu["closure_hit"] is False


def test_joint_trial_unknown_batch_returns_404(client):
    r = client.get("/api/v1/batches/nope-not-there")
    assert r.status_code == 404
