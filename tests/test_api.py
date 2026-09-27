"""End-to-end API tests (FastAPI TestClient + real SQLite file)."""

from __future__ import annotations


def _flight(no: str, off: str, wake: str) -> dict:
    return {"flight_no": no, "scheduled_off": off, "wake": wake}


VALIDATE_BODY = {
    "runways": [
        {
            "runway": "36L",
            "flights": [
                _flight("CA101", "2026-09-18T00:00:00Z", "H"),
                _flight("CA102", "2026-09-18T00:01:00Z", "M"),  # 60s < H->M 120s
                _flight("CA103", "2026-09-18T00:02:10Z", "M"),  # 70s >= M->M 60s
            ],
        },
        {
            "runway": "18R",
            "flights": [
                _flight("MU201", "2026-09-18T08:00:00+08:00", "J"),
                _flight("MU202", "2026-09-18T08:01:00+08:00", "M"),  # 60 < 180
            ],
        },
    ]
}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_default_matrix_endpoint(client):
    r = client.get("/matrix/default")
    assert r.status_code == 200
    matrix = r.json()["matrix"]
    assert matrix["J"]["M"] == 180
    assert set(matrix) == {"L", "M", "H", "J"}


def test_validate_detects_violations_and_sorts(client):
    r = client.post("/api/v1/validate", json=VALIDATE_BODY)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mode"] == "validate"
    assert data["total_runways"] == 2
    assert data["total_flights"] == 5
    assert data["total_violations"] == 2

    rw36l = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    # stable sort by (time, flight_no)
    assert rw36l["order"] == ["CA101", "CA102", "CA103"]
    assert rw36l["violation_count"] == 1
    v = rw36l["violations"][0]
    assert v["leader"] == "CA101" and v["follower"] == "CA102"
    assert v["required_seconds"] == 120
    assert v["actual_seconds"] == 60
    assert v["shortfall_seconds"] == 60
    assert v["leader_position"] == 1 and v["follower_position"] == 2

    # times emitted as UTC
    assert v["leader_scheduled_off"].endswith("Z") or "+00:00" in v["leader_scheduled_off"]


def test_validate_stable_tiebreak_on_equal_times(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("ZZZ", "2026-09-18T00:00:00Z", "M"),
                    _flight("AAA", "2026-09-18T00:00:00Z", "M"),
                ],
            }
        ]
    }
    data = client.post("/api/v1/validate", json=body).json()
    assert data["runways"][0]["order"] == ["AAA", "ZZZ"]


def test_trial_cascades_and_persists(client):
    r = client.post("/api/v1/trial", json=VALIDATE_BODY)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mode"] == "trial"
    assert data["total_runways"] == 2

    rw36l = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    items = {i["flight_no"]: i for i in rw36l["items"]}
    assert items["CA101"]["delay_seconds"] == 0
    assert items["CA101"]["triggered_by"] is None
    # H->M requires 120s: pushed from 00:01:00 to 00:02:00
    assert items["CA102"]["adjusted_off"] == "2026-09-18T00:02:00Z"
    assert items["CA102"]["delay_seconds"] == 60
    assert items["CA102"]["triggered_by"] == "CA101"
    # cascade: CA103 originally 00:02:10, must be 60s after 00:02:00 -> 00:03:00
    assert items["CA103"]["adjusted_off"] == "2026-09-18T00:03:00Z"
    assert items["CA103"]["delay_seconds"] == 50
    assert items["CA103"]["triggered_by"] == "CA102"
    assert rw36l["delayed_count"] == 2
    assert rw36l["total_delay_seconds"] == 110
    assert rw36l["max_delay_seconds"] == 60

    # 18R: J->M 180s
    rw18r = next(rw for rw in data["runways"] if rw["runway"] == "18R")
    items18 = {i["flight_no"]: i for i in rw18r["items"]}
    assert items18["MU202"]["delay_seconds"] == 120
    assert items18["MU202"]["triggered_by"] == "MU201"
    # +08:00 inputs normalised to UTC
    assert items18["MU201"]["scheduled_off"] == "2026-09-18T00:00:00Z"


def test_batch_roundtrip_validate_and_trial(client):
    v = client.post("/api/v1/validate", json=VALIDATE_BODY).json()
    got = client.get(f"/api/v1/batches/{v['batch_id']}")
    assert got.status_code == 200
    fetched = got.json()
    assert fetched["mode"] == "validate"
    assert fetched["total_violations"] == 2
    assert fetched["runways"][0]["runway"] in {"36L", "18R"}

    t = client.post("/api/v1/trial", json=VALIDATE_BODY).json()
    fetched_t = client.get(f"/api/v1/batches/{t['batch_id']}").json()
    assert fetched_t["mode"] == "trial"
    assert fetched_t["total_delay_seconds"] == 110 + 120

    assert client.get("/api/v1/batches/does-not-exist").status_code == 404


def test_custom_matrix_is_honoured(client):
    body = {
        "runways": [
            {
                "runway": "09",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "H"),
                    _flight("CA2", "2026-09-18T00:00:30Z", "H"),
                ],
            }
        ],
        "matrix": [{"leader": "H", "follower": "H", "seconds": 300}],
    }
    data = client.post("/api/v1/validate", json=body).json()
    # custom matrix only defines H->H
    assert data["matrix"] == {"H": {"H": 300}}
    v = data["runways"][0]["violations"][0]
    assert v["required_seconds"] == 300 and v["shortfall_seconds"] == 270

    trial = client.post("/api/v1/trial", json=body).json()
    item2 = trial["runways"][0]["items"][1]
    assert item2["adjusted_off"] == "2026-09-18T00:05:00Z"
    assert item2["delay_seconds"] == 270


def test_naive_datetime_rejected(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    {"flight_no": "CA1", "scheduled_off": "2026-09-18T00:00:00", "wake": "H"}
                ],
            }
        ]
    }
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 422
    assert "时区" in r.text


def test_duplicate_global_flight_no_rejected(client):
    body = {
        "runways": [
            {"runway": "36L", "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "H")]},
            {"runway": "18R", "flights": [_flight("CA1", "2026-09-18T01:00:00Z", "M")]},
        ]
    }
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 422
    assert "唯一" in r.text


def test_duplicate_runway_and_bad_wake_rejected(client):
    body = {
        "runways": [
            {"runway": "36L", "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "X")]},
        ]
    }
    assert client.post("/api/v1/validate", json=body).status_code == 422

    body2 = {
        "runways": [
            {"runway": "36L", "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "H")]},
            {"runway": "36L", "flights": [_flight("CA2", "2026-09-18T00:01:00Z", "M")]},
        ]
    }
    assert client.post("/api/v1/validate", json=body2).status_code == 422


def test_empty_runways_and_negative_matrix_rejected(client):
    assert client.post("/api/v1/validate", json={"runways": []}).status_code == 422
    body = {
        "runways": [
            {"runway": "36L", "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "H")]},
        ],
        "matrix": [{"leader": "H", "follower": "H", "seconds": -1}],
    }
    assert client.post("/api/v1/validate", json=body).status_code == 422


def test_duplicate_matrix_pair_rejected(client):
    body = {
        "runways": [
            {"runway": "36L", "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "H")]},
        ],
        "matrix": [
            {"leader": "H", "follower": "M", "seconds": 90},
            {"leader": "H", "follower": "M", "seconds": 120},
        ],
    }
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 422
    assert "矩阵组合重复" in r.text


# ---------- runway closure windows ----------


CLOSURE_BODY = {
    "runways": [
        {
            "runway": "36L",
            "flights": [
                _flight("CA101", "2026-09-18T00:00:00Z", "J"),
                _flight("CA102", "2026-09-18T00:01:00Z", "M"),
                _flight("CA103", "2026-09-18T00:04:00Z", "M"),
                _flight("CA104", "2026-09-18T00:04:30Z", "M"),
            ],
            "closures": [
                {"start": "2026-09-18T00:02:30Z", "end": "2026-09-18T00:03:30Z"},
                # 带 +08:00 偏移，归一化后为 00:06:00Z–00:07:00Z，不覆盖任何航班
                {"start": "2026-09-18T08:06:00+08:00", "end": "2026-09-18T08:07:00+08:00"},
            ],
        }
    ]
}


def test_validate_lists_closure_conflicts(client):
    r = client.post("/api/v1/validate", json=CLOSURE_BODY)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total_closure_conflicts"] == 0  # nobody scheduled inside a window

    rw = data["runways"][0]
    # closures echoed back, sorted and normalised to UTC
    assert rw["closures"] == [
        {"start": "2026-09-18T00:02:30Z", "end": "2026-09-18T00:03:30Z"},
        {"start": "2026-09-18T00:06:00Z", "end": "2026-09-18T00:07:00Z"},
    ]
    assert rw["closure_conflict_count"] == 0
    assert rw["closure_conflicts"] == []


def test_validate_reports_flights_scheduled_inside_windows(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:30Z", "H"),   # inside window 1
                    _flight("CA2", "2026-09-18T00:01:00Z", "M"),   # end is exclusive -> ok
                    _flight("CA3", "2026-09-18T00:05:59Z", "L"),   # inside window 2
                ],
                "closures": [
                    {"start": "2026-09-18T00:00:00Z", "end": "2026-09-18T00:01:00Z"},
                    {"start": "2026-09-18T00:05:00Z", "end": "2026-09-18T00:06:00Z"},
                ],
            }
        ]
    }
    data = client.post("/api/v1/validate", json=body).json()
    assert data["total_closure_conflicts"] == 2
    rw = data["runways"][0]
    assert rw["closure_conflict_count"] == 2
    c1, c2 = rw["closure_conflicts"]
    assert c1["flight_no"] == "CA1"
    assert c1["position"] == 1
    assert c1["window_index"] == 1
    assert c1["window_start"] == "2026-09-18T00:00:00Z"
    assert c1["window_end"] == "2026-09-18T00:01:00Z"
    assert c2["flight_no"] == "CA3"
    assert c2["position"] == 3
    assert c2["window_index"] == 2


def test_trial_avoids_closures_and_cascades(client):
    r = client.post("/api/v1/trial", json=CLOSURE_BODY)
    assert r.status_code == 200, r.text
    data = r.json()
    rw = data["runways"][0]
    assert len(rw["closures"]) == 2
    items = {i["flight_no"]: i for i in rw["items"]}

    # CA101 untouched
    assert items["CA101"]["delay_seconds"] == 0
    assert items["CA101"]["closure_hit"] is False
    assert items["CA101"]["closure_window"] is None

    # CA102: wake J->M pushes 00:01:00 -> 00:03:00, inside [00:02:30, 00:03:30)
    # -> iterated to the window end 00:03:30
    assert items["CA102"]["adjusted_off"] == "2026-09-18T00:03:30Z"
    assert items["CA102"]["delay_seconds"] == 150
    assert items["CA102"]["triggered_by"] == "CA101"
    assert items["CA102"]["closure_hit"] is True
    assert items["CA102"]["closure_window"] == {
        "start": "2026-09-18T00:02:30Z",
        "end": "2026-09-18T00:03:30Z",
    }

    # cascade: CA103 00:04:00 -> 00:03:30 + 60s = 00:04:30
    assert items["CA103"]["adjusted_off"] == "2026-09-18T00:04:30Z"
    assert items["CA103"]["delay_seconds"] == 30
    assert items["CA103"]["triggered_by"] == "CA102"
    assert items["CA103"]["closure_hit"] is False

    # cascade continues: CA104 00:04:30 -> 00:04:30 + 60s = 00:05:30
    assert items["CA104"]["adjusted_off"] == "2026-09-18T00:05:30Z"
    assert items["CA104"]["delay_seconds"] == 60
    assert items["CA104"]["triggered_by"] == "CA103"


def test_trial_first_flight_pushed_out_of_closure(client):
    body = {
        "runways": [
            {
                "runway": "09",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:30Z", "M"),  # inside window
                    _flight("CA2", "2026-09-18T00:02:00Z", "M"),
                ],
                "closures": [
                    {"start": "2026-09-18T00:00:00Z", "end": "2026-09-18T00:01:00Z"}
                ],
            }
        ]
    }
    data = client.post("/api/v1/trial", json=body).json()
    items = data["runways"][0]["items"]
    assert items[0]["adjusted_off"] == "2026-09-18T00:01:00Z"
    assert items[0]["delay_seconds"] == 30
    assert items[0]["triggered_by"] is None
    assert items[0]["closure_hit"] is True
    # follower still fits: 00:02:00 >= 00:01:00 + 60s
    assert items[1]["delay_seconds"] == 0


def test_closure_window_end_must_be_after_start(client):
    base = {
        "runway": "36L",
        "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "H")],
    }
    for start, end in [
        ("2026-09-18T00:01:00Z", "2026-09-18T00:01:00Z"),  # equal
        ("2026-09-18T00:02:00Z", "2026-09-18T00:01:00Z"),  # reversed
    ]:
        body = {"runways": [{**base, "closures": [{"start": start, "end": end}]}]}
        r = client.post("/api/v1/validate", json=body)
        assert r.status_code == 422
        assert "结束" in r.text


def test_overlapping_closure_windows_rejected(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "H")],
                "closures": [
                    {"start": "2026-09-18T00:00:00Z", "end": "2026-09-18T00:02:00Z"},
                    {"start": "2026-09-18T00:01:00Z", "end": "2026-09-18T00:03:00Z"},
                ],
            }
        ]
    }
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 422
    assert "重叠" in r.text


def test_touching_closure_windows_allowed(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [_flight("CA1", "2026-09-18T00:05:00Z", "H")],
                "closures": [
                    {"start": "2026-09-18T00:00:00Z", "end": "2026-09-18T00:01:00Z"},
                    {"start": "2026-09-18T00:01:00Z", "end": "2026-09-18T00:02:00Z"},
                ],
            }
        ]
    }
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 200, r.text


def test_closure_window_naive_datetime_rejected(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [_flight("CA1", "2026-09-18T00:00:00Z", "H")],
                "closures": [
                    {"start": "2026-09-18T00:00:00", "end": "2026-09-18T00:01:00Z"}
                ],
            }
        ]
    }
    r = client.post("/api/v1/validate", json=body)
    assert r.status_code == 422
    assert "时区" in r.text


def test_batch_roundtrip_with_closures(client):
    v = client.post("/api/v1/validate", json=CLOSURE_BODY).json()
    got = client.get(f"/api/v1/batches/{v['batch_id']}").json()
    assert got["total_closure_conflicts"] == 0
    assert got["runways"][0]["closures"] == v["runways"][0]["closures"]
    assert got["runways"][0]["closure_conflicts"] == []

    t = client.post("/api/v1/trial", json=CLOSURE_BODY).json()
    got_t = client.get(f"/api/v1/batches/{t['batch_id']}").json()
    assert got_t["runways"][0]["closures"] == t["runways"][0]["closures"]
    # trial items round-trip with closure details intact
    assert got_t["runways"][0]["items"] == t["runways"][0]["items"]
    item2 = got_t["runways"][0]["items"][1]
    assert item2["closure_hit"] is True
    assert item2["closure_window"]["end"] == "2026-09-18T00:03:30Z"


# ---------- rolling capacity constraint ----------


CAPACITY_BODY = {
    "runways": [
        {
            "runway": "36L",
            "flights": [
                _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                _flight("CA2", "2026-09-18T00:00:30Z", "M"),
                _flight("CA3", "2026-09-18T00:00:59Z", "M"),  # 3rd in [00:00,01:00)
                _flight("CA4", "2026-09-18T00:02:00Z", "M"),
            ],
            "capacity": {"window_seconds": 60, "max_departures": 2},
        },
        {
            "runway": "18R",
            "flights": [
                _flight("MU1", "2026-09-18T00:00:00Z", "M"),
                _flight("MU2", "2026-09-18T00:00:10Z", "M"),
            ],
        },
    ],
    "matrix": [{"leader": "M", "follower": "M", "seconds": 0}],
}


def test_validate_lists_capacity_violations(client):
    r = client.post("/api/v1/validate", json=CAPACITY_BODY)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total_capacity_violations"] == 1
    rw36l = next(rw for rw in data["runways"] if rw["runway"] == "36L")
    assert rw36l["capacity"] == {"window_seconds": 60, "max_departures": 2}
    assert rw36l["capacity_violation_count"] == 1
    cv = rw36l["capacity_violations"][0]
    assert cv["flight_no"] == "CA3"
    assert cv["position"] == 3
    assert cv["window_seconds"] == 60
    assert cv["max_departures"] == 2
    assert cv["departures_in_window"] == 3
    assert cv["anchor_flight_no"] == "CA1"
    assert cv["anchor_position"] == 1
    assert cv["window_start"] == "2026-09-18T00:00:00Z"
    assert cv["window_end"] == "2026-09-18T00:01:00Z"
    assert cv["earliest_compliant_off"] == "2026-09-18T00:01:00Z"

    # runway without a capacity config gets no capacity fields at all
    rw18r = next(rw for rw in data["runways"] if rw["runway"] == "18R")
    assert "capacity" not in rw18r
    assert "capacity_violations" not in rw18r


def test_capacity_fields_absent_when_unspecified(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    _flight("CA2", "2026-09-18T00:00:10Z", "M"),
                ],
            }
        ]
    }
    v = client.post("/api/v1/validate", json=body).json()
    assert "total_capacity_violations" not in v
    rw = v["runways"][0]
    assert "capacity" not in rw and "capacity_violation_count" not in rw

    t = client.post("/api/v1/trial", json=body).json()
    assert "capacity" not in t["runways"][0]
    item = t["runways"][0]["items"][0]
    assert "capacity_hit" not in item and "capacity_anchor" not in item


def test_capacity_config_rejects_non_positive_and_non_integer(client):
    base_flights = [_flight("CA1", "2026-09-18T00:00:00Z", "M")]
    for cap in [
        {"window_seconds": 0, "max_departures": 2},
        {"window_seconds": 60, "max_departures": 0},
        {"window_seconds": -5, "max_departures": 2},
        {"window_seconds": 60.0, "max_departures": 2},
        {"window_seconds": "60", "max_departures": 2},
    ]:
        body = {"runways": [{"runway": "36L", "flights": base_flights, "capacity": cap}]}
        r = client.post("/api/v1/validate", json=body)
        assert r.status_code == 422, cap

    # unknown fields stay rejected
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": base_flights,
                "capacity": {"window_seconds": 60, "max_departures": 2, "extra": 1},
            }
        ]
    }
    assert client.post("/api/v1/validate", json=body).status_code == 422


def test_trial_capacity_defers_flights_and_marks_trigger(client):
    r = client.post("/api/v1/trial", json=CAPACITY_BODY)
    assert r.status_code == 200, r.text
    rw = next(x for x in r.json()["runways"] if x["runway"] == "36L")
    assert rw["capacity"] == {"window_seconds": 60, "max_departures": 2}
    items = {i["flight_no"]: i for i in rw["items"]}

    assert items["CA1"]["adjusted_off"] == "2026-09-18T00:00:00Z"
    assert items["CA1"]["capacity_hit"] is False
    assert items["CA1"]["capacity_anchor"] is None
    assert items["CA1"]["capacity_window"] is None
    # CA3 pushed from 00:00:59 to 00:01:00 against the window anchored at CA1
    assert items["CA3"]["adjusted_off"] == "2026-09-18T00:01:00Z"
    assert items["CA3"]["delay_seconds"] == 1
    assert items["CA3"]["capacity_hit"] is True
    assert items["CA3"]["capacity_anchor"] == "CA1"
    assert items["CA3"]["capacity_window_seconds"] == 60
    assert items["CA3"]["capacity_max_departures"] == 2
    assert items["CA3"]["capacity_window"] == {
        "start": "2026-09-18T00:00:00Z",
        "end": "2026-09-18T00:01:00Z",
    }
    # CA4 at 00:02:00 is clear of every 60 s window -> untouched
    assert items["CA4"]["delay_seconds"] == 0
    assert items["CA4"]["capacity_hit"] is False


def test_trial_capacity_then_closure_iteration(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    _flight("CA2", "2026-09-18T00:00:30Z", "M"),
                    _flight("CA3", "2026-09-18T00:00:59Z", "M"),
                ],
                "closures": [
                    {"start": "2026-09-18T00:01:00Z", "end": "2026-09-18T00:01:40Z"}
                ],
                "capacity": {"window_seconds": 60, "max_departures": 2},
            }
        ],
        "matrix": [{"leader": "M", "follower": "M", "seconds": 0}],
    }
    data = client.post("/api/v1/trial", json=body).json()
    item3 = data["runways"][0]["items"][2]
    # capacity push to 01:00 lands exactly on the closure start -> 01:40
    assert item3["adjusted_off"] == "2026-09-18T00:01:40Z"
    assert item3["capacity_hit"] is True
    assert item3["closure_hit"] is True
    assert item3["capacity_window"]["end"] == "2026-09-18T00:01:00Z"
    assert item3["closure_window"]["start"] == "2026-09-18T00:01:00Z"


def test_batch_roundtrip_preserves_capacity(client):
    v = client.post("/api/v1/validate", json=CAPACITY_BODY).json()
    got_v = client.get(f"/api/v1/batches/{v['batch_id']}").json()
    assert got_v["total_capacity_violations"] == 1
    rw = next(x for x in got_v["runways"] if x["runway"] == "36L")
    assert rw["capacity"] == {"window_seconds": 60, "max_departures": 2}
    assert rw["capacity_violations"] == next(
        x for x in v["runways"] if x["runway"] == "36L"
    )["capacity_violations"]
    # runway without capacity keeps the lean shape on replay too
    rw18r = next(x for x in got_v["runways"] if x["runway"] == "18R")
    assert "capacity" not in rw18r

    t = client.post("/api/v1/trial", json=CAPACITY_BODY).json()
    got_t = client.get(f"/api/v1/batches/{t['batch_id']}").json()
    t36l = next(x for x in got_t["runways"] if x["runway"] == "36L")
    assert t36l["capacity"] == {"window_seconds": 60, "max_departures": 2}
    assert t36l["items"] == next(
        x for x in t["runways"] if x["runway"] == "36L"
    )["items"]


# ---------- immovable (locked) flights ----------


def _lflight(no: str, off: str, wake: str, locked: bool) -> dict:
    return {"flight_no": no, "scheduled_off": off, "wake": wake, "locked": locked}


def test_validate_without_locked_keeps_legacy_shape(client):
    # no locked field anywhere -> no lock keys in the response
    data = client.post("/api/v1/validate", json=VALIDATE_BODY).json()
    assert "total_locked" not in data
    assert "locked_flights" not in data["runways"][0]


def test_trial_without_locked_keeps_legacy_shape(client):
    data = client.post("/api/v1/trial", json=VALIDATE_BODY).json()
    assert "total_locked" not in data
    rw = data["runways"][0]
    assert "locked_flights" not in rw and "blocked" not in rw
    assert "locked" not in rw["items"][0]


def test_validate_echoes_locked_flights_but_checks_original_plan(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _lflight("CA2", "2026-09-18T00:01:00Z", "M", True),
                    _lflight("CA3", "2026-09-18T00:05:00Z", "M", False),
                ],
            }
        ]
    }
    data = client.post("/api/v1/validate", json=body).json()
    assert data["total_locked"] == 1
    rw = data["runways"][0]
    # locking changes nothing about validation: the 60 s gap still violates
    assert rw["violation_count"] == 1
    assert rw["locked_flights"] == [
        {
            "position": 2,
            "flight_no": "CA2",
            "scheduled_off": "2026-09-18T00:01:00Z",
            "wake": "M",
        }
    ]


def test_trial_locked_flight_keeps_scheduled_when_feasible(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _lflight("CA2", "2026-09-18T00:03:00Z", "M", True),
                ],
            }
        ]
    }
    data = client.post("/api/v1/trial", json=body).json()
    assert data["total_locked"] == 1
    rw = data["runways"][0]
    assert rw["blocked"] is None
    items = {i["flight_no"]: i for i in rw["items"]}
    assert items["CA2"]["locked"] is True
    assert items["CA2"]["adjusted_off"] == "2026-09-18T00:03:00Z"
    assert items["CA2"]["delay_seconds"] == 0
    assert items["CA1"]["locked"] is False
    assert [f["flight_no"] for f in rw["locked_flights"]] == ["CA2"]


def test_trial_locked_flight_blocked_by_wake_stops_runway(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _lflight("CA2", "2026-09-18T00:01:00Z", "M", True),
                    _flight("CA3", "2026-09-18T00:10:00Z", "M"),
                ],
            }
        ]
    }
    data = client.post("/api/v1/trial", json=body).json()
    rw = data["runways"][0]
    # feasible prefix: only CA1 gets a suggestion
    assert [i["flight_no"] for i in rw["items"]] == ["CA1"]
    assert rw["total"] == 3  # all flights still counted
    blocked = rw["blocked"]
    assert blocked["flight_no"] == "CA2"
    assert blocked["position"] == 2
    assert blocked["constraint"] == "wake"
    assert blocked["required_earliest_off"] == "2026-09-18T00:03:00Z"
    assert blocked["triggered_by"] == "CA1"
    assert blocked["required_gap_seconds"] == 180
    assert blocked["closure_hit"] is False
    assert blocked["closure_window"] is None
    # no capacity configured on this runway -> capacity detail omitted
    assert "capacity_hit" not in blocked


def test_trial_locked_flight_blocked_by_closure(client):
    body = {
        "runways": [
            {
                "runway": "09",
                "flights": [
                    _lflight("CA1", "2026-09-18T00:00:30Z", "M", True),
                ],
                "closures": [
                    {"start": "2026-09-18T00:00:00Z", "end": "2026-09-18T00:01:00Z"}
                ],
            }
        ]
    }
    data = client.post("/api/v1/trial", json=body).json()
    rw = data["runways"][0]
    assert rw["items"] == []
    blocked = rw["blocked"]
    assert blocked["flight_no"] == "CA1"
    assert blocked["position"] == 1
    assert blocked["constraint"] == "closure"
    assert blocked["required_earliest_off"] == "2026-09-18T00:01:00Z"
    assert blocked["triggered_by"] is None
    assert blocked["closure_hit"] is True
    assert blocked["closure_window"] == {
        "start": "2026-09-18T00:00:00Z",
        "end": "2026-09-18T00:01:00Z",
    }


def test_trial_locked_flight_blocked_by_capacity(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "M"),
                    _flight("CA2", "2026-09-18T00:00:30Z", "M"),
                    _lflight("CA3", "2026-09-18T00:00:59Z", "M", True),
                ],
                "capacity": {"window_seconds": 60, "max_departures": 2},
            }
        ],
        "matrix": [{"leader": "M", "follower": "M", "seconds": 0}],
    }
    data = client.post("/api/v1/trial", json=body).json()
    rw = data["runways"][0]
    assert [i["flight_no"] for i in rw["items"]] == ["CA1", "CA2"]
    blocked = rw["blocked"]
    assert blocked["flight_no"] == "CA3"
    assert blocked["constraint"] == "capacity"
    assert blocked["required_earliest_off"] == "2026-09-18T00:01:00Z"
    assert blocked["capacity_hit"] is True
    assert blocked["capacity_anchor"] == "CA1"
    assert blocked["capacity_window_seconds"] == 60
    assert blocked["capacity_max_departures"] == 2
    assert blocked["capacity_window"] == {
        "start": "2026-09-18T00:00:00Z",
        "end": "2026-09-18T00:01:00Z",
    }


def test_trial_blocked_runway_persists_prefix_and_blocker(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _lflight("CA2", "2026-09-18T00:01:00Z", "M", True),
                    _flight("CA3", "2026-09-18T00:10:00Z", "M"),
                ],
            }
        ]
    }
    t = client.post("/api/v1/trial", json=body).json()
    got = client.get(f"/api/v1/batches/{t['batch_id']}").json()
    assert got["total_locked"] == 1
    rw = got["runways"][0]
    assert rw["total"] == 3
    assert [i["flight_no"] for i in rw["items"]] == ["CA1"]
    assert rw["locked_flights"][0]["flight_no"] == "CA2"
    assert rw["blocked"]["flight_no"] == "CA2"
    assert rw["blocked"]["constraint"] == "wake"
    assert rw["blocked"]["required_earliest_off"] == "2026-09-18T00:03:00Z"


def test_validate_batch_roundtrip_includes_locked(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _lflight("CA1", "2026-09-18T00:00:00Z", "H", True),
                    _flight("CA2", "2026-09-18T00:05:00Z", "M"),
                ],
            }
        ]
    }
    v = client.post("/api/v1/validate", json=body).json()
    got = client.get(f"/api/v1/batches/{v['batch_id']}").json()
    assert got["total_locked"] == 1
    assert got["runways"][0]["locked_flights"][0]["flight_no"] == "CA1"


def test_explicit_locked_false_still_echoes_lock_shape(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _lflight("CA1", "2026-09-18T00:00:00Z", "M", False),
                    _flight("CA2", "2026-09-18T00:01:00Z", "M"),
                ],
            }
        ]
    }
    v = client.post("/api/v1/validate", json=body).json()
    # field was explicitly provided -> lock shape present, but nobody locked
    assert v["total_locked"] == 0
    assert v["runways"][0]["locked_flights"] == []

    t = client.post("/api/v1/trial", json=body).json()
    assert t["total_locked"] == 0
    rw = t["runways"][0]
    assert rw["blocked"] is None
    assert all(i["locked"] is False for i in rw["items"])


def test_locked_must_be_boolean(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    {"flight_no": "CA1", "scheduled_off": "2026-09-18T00:00:00Z",
                     "wake": "M", "locked": "yes"}
                ],
            }
        ]
    }
    assert client.post("/api/v1/trial", json=body).status_code == 422


# ---------- best adjacent swap ----------


SWAP_BODY = {
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


def test_best_adjacent_swap_improves_and_persists(client):
    r = client.post("/api/v1/trial/best-adjacent-swap", json=SWAP_BODY)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["mode"] == "best_adjacent_swap"
    assert data["total_runways"] == 1
    assert data["total_flights"] == 2
    assert data["total_baseline_delay_seconds"] == 130
    assert data["total_delay_seconds"] == 110
    assert data["total_saved_seconds"] == 20

    rw = data["runways"][0]
    assert rw["baseline_order"] == ["CA1", "CA2"]
    assert rw["order"] == ["CA2", "CA1"]
    assert rw["total"] == 2
    assert rw["baseline"] == {
        "delayed_count": 1,
        "total_delay_seconds": 130,
        "max_delay_seconds": 130,
    }
    assert rw["chosen_swap"] == {
        "position": 1,
        "first_flight_no": "CA1",
        "second_flight_no": "CA2",
    }
    assert rw["saved_seconds"] == 20
    assert rw["delayed_count"] == 1
    assert rw["total_delay_seconds"] == 110
    assert rw["max_delay_seconds"] == 110

    items = {i["flight_no"]: i for i in rw["items"]}
    # scheduled_off is never rewritten; positions follow the optimised order
    assert [i["flight_no"] for i in rw["items"]] == ["CA2", "CA1"]
    assert rw["items"][0]["position"] == 1
    assert items["CA2"]["scheduled_off"] == "2026-09-18T00:00:50Z"
    assert items["CA2"]["adjusted_off"] == "2026-09-18T00:00:50Z"
    assert items["CA2"]["delay_seconds"] == 0
    assert items["CA1"]["scheduled_off"] == "2026-09-18T00:00:00Z"
    assert items["CA1"]["adjusted_off"] == "2026-09-18T00:01:50Z"
    assert items["CA1"]["delay_seconds"] == 110
    assert items["CA1"]["triggered_by"] == "CA2"

    # persisted and re-fetchable through the existing batch endpoint
    got = client.get(f"/api/v1/batches/{data['batch_id']}")
    assert got.status_code == 200
    fetched = got.json()
    assert fetched["mode"] == "best_adjacent_swap"
    assert fetched["total_baseline_delay_seconds"] == 130
    assert fetched["total_delay_seconds"] == 110
    assert fetched["total_saved_seconds"] == 20
    got_rw = fetched["runways"][0]
    assert got_rw["baseline_order"] == rw["baseline_order"]
    assert got_rw["order"] == rw["order"]
    assert got_rw["baseline"] == rw["baseline"]
    assert got_rw["chosen_swap"] == rw["chosen_swap"]
    assert got_rw["saved_seconds"] == rw["saved_seconds"]
    assert got_rw["items"] == rw["items"]


def test_best_adjacent_swap_no_improvement_returns_null_swap(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _flight("CA2", "2026-09-18T00:01:00Z", "M"),
                ],
            }
        ]
    }
    data = client.post("/api/v1/trial/best-adjacent-swap", json=body).json()
    rw = data["runways"][0]
    # swap ties the baseline on (total, max) -> "no swap" wins
    assert rw["chosen_swap"] is None
    assert rw["saved_seconds"] == 0
    assert rw["order"] == rw["baseline_order"] == ["CA1", "CA2"]
    assert rw["baseline"]["total_delay_seconds"] == 120
    assert rw["total_delay_seconds"] == 120
    assert data["total_saved_seconds"] == 0


def test_best_adjacent_swap_rejects_locked_true(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _lflight("CA1", "2026-09-18T00:00:00Z", "J", True),
                    _flight("CA2", "2026-09-18T00:00:50Z", "M"),
                ],
            },
            {
                "runway": "18R",
                "flights": [_flight("MU1", "2026-09-18T00:00:00Z", "M")],
            },
        ]
    }
    r = client.post("/api/v1/trial/best-adjacent-swap", json=body)
    assert r.status_code == 422
    assert "locked" in r.text


def test_best_adjacent_swap_allows_explicit_locked_false(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _lflight("CA1", "2026-09-18T00:00:00Z", "J", False),
                    _lflight("CA2", "2026-09-18T00:00:50Z", "M", False),
                ],
            }
        ]
    }
    r = client.post("/api/v1/trial/best-adjacent-swap", json=body)
    assert r.status_code == 200, r.text
    rw = r.json()["runways"][0]
    assert rw["chosen_swap"]["position"] == 1
    # this endpoint never echoes lock detail
    assert "locked_flights" not in rw
    assert "locked" not in rw["items"][0]
    assert "total_locked" not in r.json()


def test_best_adjacent_swap_runways_independent_and_capacity_echoed(client):
    body = {
        "runways": [
            {
                "runway": "36L",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _flight("CA2", "2026-09-18T00:00:50Z", "M"),
                ],
                "capacity": {"window_seconds": 600, "max_departures": 5},
            },
            {
                "runway": "18R",
                "flights": [
                    _flight("MU1", "2026-09-18T00:00:00Z", "J"),
                    _flight("MU2", "2026-09-18T00:01:00Z", "M"),
                ],
            },
        ]
    }
    data = client.post("/api/v1/trial/best-adjacent-swap", json=body).json()
    rw36 = next(r for r in data["runways"] if r["runway"] == "36L")
    rw18 = next(r for r in data["runways"] if r["runway"] == "18R")
    # each runway solved independently: 36L improves, 18R keeps its order
    assert rw36["chosen_swap"] is not None
    assert rw36["capacity"] == {"window_seconds": 600, "max_departures": 5}
    assert "capacity_hit" in rw36["items"][0]
    assert rw18["chosen_swap"] is None
    assert "capacity" not in rw18
    assert "capacity_hit" not in rw18["items"][0]
    assert data["total_saved_seconds"] == rw36["saved_seconds"]

    got = client.get(f"/api/v1/batches/{data['batch_id']}").json()
    g36 = next(r for r in got["runways"] if r["runway"] == "36L")
    g18 = next(r for r in got["runways"] if r["runway"] == "18R")
    assert g36["capacity"] == {"window_seconds": 600, "max_departures": 5}
    assert g36["items"] == rw36["items"]
    assert "capacity" not in g18
    assert g18["chosen_swap"] is None


def test_best_adjacent_swap_optimized_items_carry_closure_detail(client):
    body = {
        "runways": [
            {
                "runway": "09",
                "flights": [
                    _flight("CA1", "2026-09-18T00:00:00Z", "J"),
                    _flight("CA2", "2026-09-18T00:00:50Z", "M"),
                ],
                "closures": [
                    {"start": "2026-09-18T00:01:30Z", "end": "2026-09-18T00:02:00Z"}
                ],
            }
        ]
    }
    data = client.post("/api/v1/trial/best-adjacent-swap", json=body).json()
    rw = data["runways"][0]
    assert rw["closures"] == [
        {"start": "2026-09-18T00:01:30Z", "end": "2026-09-18T00:02:00Z"}
    ]
    # swapped: CA1 wake-pushed to 00:01:50, inside the closure -> 00:02:00
    assert rw["chosen_swap"] == {
        "position": 1,
        "first_flight_no": "CA1",
        "second_flight_no": "CA2",
    }
    assert rw["saved_seconds"] == 10
    items = {i["flight_no"]: i for i in rw["items"]}
    assert items["CA2"]["delay_seconds"] == 0
    assert items["CA1"]["adjusted_off"] == "2026-09-18T00:02:00Z"
    assert items["CA1"]["delay_seconds"] == 120
    assert items["CA1"]["closure_hit"] is True
    assert items["CA1"]["closure_window"] == {
        "start": "2026-09-18T00:01:30Z",
        "end": "2026-09-18T00:02:00Z",
    }


def test_init_db_migrates_legacy_batches_mode_check(tmp_path):
    # databases created before best_adjacent_swap have a narrower CHECK on
    # batches.mode; init_db must rebuild the table, keep existing rows and
    # leave child-table foreign keys pointing at batches
    import sqlite3

    from app import database

    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE batches (batch_id TEXT PRIMARY KEY,"
        " mode TEXT NOT NULL CHECK (mode IN ('validate', 'trial')),"
        " created_at TEXT NOT NULL, matrix_json TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO batches VALUES ('b1', 'trial', '2026-09-19T00:00:00+00:00', '{}')"
    )
    conn.commit()
    conn.close()

    database.init_db(str(db_file))

    check = sqlite3.connect(str(db_file))
    check.execute("PRAGMA foreign_keys=ON")
    assert check.execute("SELECT batch_id, mode FROM batches").fetchall() == [
        ("b1", "trial")
    ]
    # the widened CHECK now accepts the new mode
    check.execute(
        "INSERT INTO batches VALUES ('b2', 'best_adjacent_swap',"
        " '2026-09-19T00:00:00+00:00', '{}')"
    )
    # child tables still reference batches: inserts into them must work
    check.execute(
        "INSERT INTO flights (batch_id, runway, position, flight_no,"
        " scheduled_off, wake, locked)"
        " VALUES ('b2', '36L', 1, 'CA1', '2026-09-19T00:00:00+00:00', 'J', NULL)"
    )
    check.execute(
        "INSERT INTO swap_results (batch_id, runway, baseline_order_json,"
        " baseline_delayed_count, baseline_total_delay_seconds,"
        " baseline_max_delay_seconds, saved_seconds)"
        " VALUES ('b2', '36L', '[\"CA1\"]', 0, 0, 0, 0)"
    )
    check.commit()
    # foreign keys are genuinely enforced against the rebuilt table
    try:
        check.execute(
            "INSERT INTO flights (batch_id, runway, position, flight_no,"
            " scheduled_off, wake, locked)"
            " VALUES ('nope', '36L', 1, 'CA9', '2026-09-19T00:00:00+00:00', 'J', NULL)"
        )
        check.commit()
        raise AssertionError("expected FK violation for unknown batch_id")
    except sqlite3.IntegrityError:
        pass
    check.close()
