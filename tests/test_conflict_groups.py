"""End-to-end tests for GET /api/v1/batches/{batch_id}/conflict-groups."""

from __future__ import annotations


def _flight(no: str, off: str, wake: str) -> dict:
    return {"flight_no": no, "scheduled_off": off, "wake": wake}


def _validate(client, body: dict) -> str:
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 200, r.text
    return r.json()["batch_id"]


def _groups(client, batch_id: str) -> dict:
    r = client.get(f"/api/v1/batches/{batch_id}/conflict-groups")
    assert r.status_code == 200, r.text
    return r.json()


def test_unknown_batch_returns_404(client):
    r = client.get("/api/v1/batches/does-not-exist/conflict-groups")
    assert r.status_code == 404
    assert "批次不存在" in r.text


def test_non_validate_batches_return_422(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _flight("CA2", "2026-09-18T00:00:50Z", "M"),
                ],
            }
        ]
    }
    trial_id = client.post("/api/v1/trial", json=body).json()["batch_id"]
    swap_id = client.post("/api/v1/trial/best-adjacent-swap", json=body).json()[
        "batch_id"
    ]
    joint_id = client.post("/api/v1/trial/joint", json=body).json()["batch_id"]
    for batch_id in (trial_id, swap_id, joint_id):
        r = client.get(f"/api/v1/batches/{batch_id}/conflict-groups")
        assert r.status_code == 422, batch_id
        assert "validate" in r.text


def test_wake_violations_form_separate_groups(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "H"),
                    _flight("CA2", "2026-09-18T00:00:30Z", "M"),  # 30 < H->M 120
                    _flight("CA3", "2026-09-18T00:05:00Z", "M"),  # uninvolved
                    _flight("CA4", "2026-09-18T00:10:00Z", "J"),
                    _flight("CA5", "2026-09-18T00:10:30Z", "M"),  # 30 < J->M 180
                ],
            }
        ]
    }
    data = _groups(client, _validate(client, body))
    assert data["mode"] == "validate"
    assert data["total_runways"] == 1
    assert data["total_groups"] == 2

    rw = data["runways"][0]
    assert rw["runway"] == "36L"
    assert rw["group_count"] == 2
    g1, g2 = rw["groups"]
    # groups sorted by earliest flight; flights sorted by (time, flight_no)
    assert g1["group_index"] == 1
    assert [f["flight_no"] for f in g1["flights"]] == ["CA1", "CA2"]
    assert [f["position"] for f in g1["flights"]] == [1, 2]
    assert g1["flights"][0]["scheduled_off"] == "2026-09-18T00:00:00Z"
    assert g1["flights"][0]["wake"] == "H"
    assert g1["wake_conflict_count"] == 1
    assert g1["capacity_conflict_count"] == 0
    assert g1["closure_conflict_count"] == 0
    assert g2["group_index"] == 2
    assert [f["flight_no"] for f in g2["flights"]] == ["CA4", "CA5"]
    assert g2["wake_conflict_count"] == 1
    # CA3 sits between the two pairs in the planned order but is uninvolved:
    # it is not output and the two conflicts stay in separate groups.
    assert "CA3" not in {f["flight_no"] for g in rw["groups"] for f in g["flights"]}


def test_capacity_windows_merge_and_pull_in_window_flights(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    _flight("CA2", "2026-09-18T00:01:00Z", "M"),
                    _flight("CA3", "2026-09-18T00:02:00Z", "M"),
                    _flight("CA4", "2026-09-18T00:03:00Z", "H"),
                    _flight("CA5", "2026-09-18T00:03:30Z", "M"),  # 30 < H->M 120
                ],
                "capacity": {"window_seconds": 600, "max_departures": 2},
            }
        ],
        # only H->M constrained: exactly one wake violation (CA4 -> CA5)
        "matrix": [{"leader": "H", "follower": "M", "seconds": 120}],
    }
    batch_id = _validate(client, body)
    v = client.get(f"/api/v1/batches/{batch_id}").json()
    assert v["total_capacity_violations"] == 3  # CA3, CA4, CA5

    data = _groups(client, batch_id)
    rw = data["runways"][0]
    # overlapping triggering windows ([00:00+10m), [00:01+10m), [00:02+10m))
    # merge into one component; the wake edge CA4->CA5 shares flights with
    # them, and CA1/CA2 join via the first window although they have no
    # conflict of their own.
    assert rw["group_count"] == 1
    group = rw["groups"][0]
    assert [f["flight_no"] for f in group["flights"]] == [
        "CA1",
        "CA2",
        "CA3",
        "CA4",
        "CA5",
    ]
    assert group["wake_conflict_count"] == 1
    assert group["capacity_conflict_count"] == 3
    assert group["closure_conflict_count"] == 0


def test_disjoint_capacity_groups_stay_separate(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    _flight("CA2", "2026-09-18T00:00:30Z", "M"),
                    _flight("CA3", "2026-09-18T00:00:59Z", "M"),
                    _flight("CA4", "2026-09-18T00:10:00Z", "M"),
                    _flight("CA5", "2026-09-18T00:10:30Z", "M"),
                    _flight("CA6", "2026-09-18T00:10:59Z", "M"),
                ],
                "capacity": {"window_seconds": 60, "max_departures": 2},
            }
        ],
        "matrix": [{"leader": "M", "follower": "M", "seconds": 0}],
    }
    data = _groups(client, _validate(client, body))
    rw = data["runways"][0]
    # CA3 and CA4 are adjacent in the planned order, but their capacity
    # windows do not overlap and nothing links them -> two groups.
    assert rw["group_count"] == 2
    g1, g2 = rw["groups"]
    assert [f["flight_no"] for f in g1["flights"]] == ["CA1", "CA2", "CA3"]
    assert g1["capacity_conflict_count"] == 1
    assert [f["flight_no"] for f in g2["flights"]] == ["CA4", "CA5", "CA6"]
    assert g2["capacity_conflict_count"] == 1


def test_closure_conflict_is_singleton_group(client):
    body = {
        "runways": [
            {
                "runway": "09",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "H"),
                    _flight("CA2", "2026-09-18T00:00:30Z", "M"),  # wake violation
                    _flight("CA3", "2026-09-18T00:05:30Z", "M"),  # in closure
                ],
                "closures": [
                    {"start": "2026-09-18T00:05:00Z", "end": "2026-09-18T00:06:00Z"}
                ],
            }
        ]
    }
    data = _groups(client, _validate(client, body))
    rw = data["runways"][0]
    assert rw["group_count"] == 2
    g1, g2 = rw["groups"]
    assert [f["flight_no"] for f in g1["flights"]] == ["CA1", "CA2"]
    assert g1["wake_conflict_count"] == 1
    assert g1["closure_conflict_count"] == 0
    # the closure-hit flight forms its own single-flight group
    assert [f["flight_no"] for f in g2["flights"]] == ["CA3"]
    assert g2["wake_conflict_count"] == 0
    assert g2["capacity_conflict_count"] == 0
    assert g2["closure_conflict_count"] == 1


def test_closure_conflict_merges_through_shared_flight(client):
    body = {
        "runways": [
            {
                "runway": "09",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "H"),
                    _flight("CA2", "2026-09-18T00:00:30Z", "M"),  # wake violation
                    _flight("CA3", "2026-09-18T00:01:00Z", "M"),  # wake + closure
                ],
                "closures": [
                    {"start": "2026-09-18T00:00:45Z", "end": "2026-09-18T00:01:30Z"}
                ],
            }
        ]
    }
    data = _groups(client, _validate(client, body))
    rw = data["runways"][0]
    # CA3 is closure-hit and also violates the wake gap behind CA2, so the
    # singleton closure conflict merges with the wake component.
    assert rw["group_count"] == 1
    group = rw["groups"][0]
    assert [f["flight_no"] for f in group["flights"]] == ["CA1", "CA2", "CA3"]
    assert group["wake_conflict_count"] == 2
    assert group["closure_conflict_count"] == 1
    assert group["capacity_conflict_count"] == 0


def test_clean_batch_has_no_groups(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    _flight("CA2", "2026-09-18T00:05:00Z", "M"),
                ],
            },
            {
                "runway": "18R",
                "flights": [
                    _flight("MU1", "2026-09-18T00:00:00Z", "H"),
                ],
            },
        ]
    }
    data = _groups(client, _validate(client, body))
    assert data["total_runways"] == 2
    assert data["total_groups"] == 0
    # every runway is echoed, with an empty group list
    assert [(rw["runway"], rw["group_count"], rw["groups"]) for rw in data["runways"]] == [
        ("18R", 0, []),
        ("36L", 0, []),
    ]


def test_multi_runway_groups_are_independent(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _flight("CA2", "2026-09-18T00:00:30Z", "M"),  # 30 < J->M 180
                ],
            },
            {
                "runway": "18R",
                "flights": [
                    _flight("MU1", "2026-09-18T00:00:00Z", "H"),
                    _flight("MU2", "2026-09-18T00:01:00Z", "M"),  # 60 < H->M 120
                    _flight("MU3", "2026-09-18T00:01:30Z", "M"),  # 30 < M->M 60
                ],
            },
        ]
    }
    data = _groups(client, _validate(client, body))
    assert data["total_groups"] == 2
    by_runway = {rw["runway"]: rw for rw in data["runways"]}
    g36 = by_runway["36L"]["groups"][0]
    assert [f["flight_no"] for f in g36["flights"]] == ["CA1", "CA2"]
    assert g36["wake_conflict_count"] == 1
    g18 = by_runway["18R"]["groups"][0]
    # MU1->MU2 and MU2->MU3 chain into a single component
    assert [f["flight_no"] for f in g18["flights"]] == ["MU1", "MU2", "MU3"]
    assert g18["wake_conflict_count"] == 2


def test_response_is_read_only_and_replayable(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "H"),
                    _flight("CA2", "2026-09-18T00:00:30Z", "M"),
                ],
            }
        ]
    }
    batch_id = _validate(client, body)
    first = _groups(client, batch_id)
    assert first["batch_id"] == batch_id
    assert first["created_at"]
    # repeated reads return the same payload and never alter the stored batch
    assert _groups(client, batch_id) == first
    fetched = client.get(f"/api/v1/batches/{batch_id}").json()
    assert fetched["mode"] == "validate"
    assert fetched["total_violations"] == 1
