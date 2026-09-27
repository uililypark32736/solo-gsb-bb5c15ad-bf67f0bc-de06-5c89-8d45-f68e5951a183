"""Tests for runway slot reservation books (create / reserve / query / cancel)."""

from __future__ import annotations

import sqlite3
import threading

import pytest


def _book_body(**overrides):
    body = {
        "runway": {
            "runway": "36L",
            "flights": [
                {"flight_no": "CA1", "scheduled_off": "2026-09-18T00:00:00Z", "wake": "J"},
                {"flight_no": "CA2", "scheduled_off": "2026-09-18T00:05:00Z", "wake": "M"},
            ],
        }
    }
    body.update(overrides)
    return body


def _resv_body(flight_no="CA9", wake="M", earliest="2026-09-18T00:00:30Z",
               latest="2026-09-18T00:10:00Z", **overrides):
    body = {
        "flight_no": flight_no,
        "wake": wake,
        "earliest_off": earliest,
        "latest_off": latest,
    }
    body.update(overrides)
    return body


# ---------- book creation ----------


def test_create_book_freezes_feasible_plan(client):
    r = client.post("/api/v1/reservation-books", json=_book_body())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["runway"] == "36L"
    assert data["total_fixed"] == 2
    assert data["total_reservations"] == 0
    assert data["reservations"] == []
    assert [f["flight_no"] for f in data["fixed_flights"]] == ["CA1", "CA2"]
    assert data["order"] == ["CA1", "CA2"]
    assert data["capacity"] is None
    assert data["closures"] == []
    assert data["matrix"]["J"]["M"] == 180
    for f in data["fixed_flights"]:
        assert set(f) == {"position", "flight_no", "scheduled_off", "wake"}


def test_create_book_with_capacity_and_closures(client):
    body = _book_body()
    body["runway"]["capacity"] = {"window_seconds": 600, "max_departures": 2}
    body["runway"]["closures"] = [
        {"start": "2026-09-18T00:10:00Z", "end": "2026-09-18T00:20:00Z"}
    ]
    r = client.post("/api/v1/reservation-books", json=body)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["capacity"] == {"window_seconds": 600, "max_departures": 2}
    assert data["closures"] == [
        {"start": "2026-09-18T00:10:00Z", "end": "2026-09-18T00:20:00Z"}
    ]


def test_create_book_infeasible_wake_violation_returns_422(client):
    body = _book_body()
    body["runway"]["flights"][1]["scheduled_off"] = "2026-09-18T00:00:30Z"
    r = client.post("/api/v1/reservation-books", json=body)
    assert r.status_code == 422
    assert "尾流间隔违规" in r.json()["detail"]


def test_create_book_infeasible_closure_conflict_returns_422(client):
    body = _book_body()
    body["runway"]["closures"] = [
        {"start": "2026-09-18T00:04:00Z", "end": "2026-09-18T00:06:00Z"}
    ]
    r = client.post("/api/v1/reservation-books", json=body)
    assert r.status_code == 422
    assert "关闭窗口冲突" in r.json()["detail"]


def test_create_book_infeasible_capacity_returns_422(client):
    body = _book_body()
    body["runway"]["flights"].append(
        {"flight_no": "CA3", "scheduled_off": "2026-09-18T00:06:00Z", "wake": "M"}
    )
    body["runway"]["capacity"] = {"window_seconds": 600, "max_departures": 2}
    body["matrix"] = [{"leader": "J", "follower": "M", "seconds": 0}]
    r = client.post("/api/v1/reservation-books", json=body)
    assert r.status_code == 422
    assert "滚动容量超限" in r.json()["detail"]


def test_create_book_validation_errors(client):
    # duplicate flight number in frozen plan
    body = _book_body()
    body["runway"]["flights"][1]["flight_no"] = "CA1"
    assert client.post("/api/v1/reservation-books", json=body).status_code == 422
    # naive datetime
    body = _book_body()
    body["runway"]["flights"][0]["scheduled_off"] = "2026-09-18T00:00:00"
    assert client.post("/api/v1/reservation-books", json=body).status_code == 422
    # subsecond precision
    body = _book_body()
    body["runway"]["flights"][0]["scheduled_off"] = "2026-09-18T00:00:00.5Z"
    assert client.post("/api/v1/reservation-books", json=body).status_code == 422
    # extra field
    body = _book_body()
    body["runway"]["bogus"] = 1
    assert client.post("/api/v1/reservation-books", json=body).status_code == 422
    # overlapping closures
    body = _book_body()
    body["runway"]["closures"] = [
        {"start": "2026-09-18T00:10:00Z", "end": "2026-09-18T00:20:00Z"},
        {"start": "2026-09-18T00:15:00Z", "end": "2026-09-18T00:25:00Z"},
    ]
    assert client.post("/api/v1/reservation-books", json=body).status_code == 422


def test_infeasible_book_creation_persists_nothing(client):
    import app.database as database
    conn = sqlite3.connect(database.settings.db_path)
    before = conn.execute("SELECT COUNT(*) FROM reservation_books").fetchone()[0]
    conn.close()
    body = _book_body()
    body["runway"]["flights"][1]["scheduled_off"] = "2026-09-18T00:00:30Z"
    assert client.post("/api/v1/reservation-books", json=body).status_code == 422
    conn = sqlite3.connect(database.settings.db_path)
    after = conn.execute("SELECT COUNT(*) FROM reservation_books").fetchone()[0]
    conn.close()
    assert after == before


# ---------- reservations ----------


def test_reserve_returns_id_and_utc_slot(client):
    bid = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    r = client.post(
        f"/api/v1/reservation-books/{bid}/reservations", json=_resv_body()
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert len(data["reservation_id"]) == 32  # uuid4 hex
    assert data["book_id"] == bid
    assert data["flight_no"] == "CA9"
    assert data["assigned_off"] == "2026-09-18T00:03:00Z"  # J->M = 180s
    assert data["insert_position"] == 2
    assert data["order"] == ["CA1", "CA9", "CA2"]
    assert data["earliest_off"] == "2026-09-18T00:00:30Z"
    assert data["latest_off"] == "2026-09-18T00:10:00Z"


def test_reserve_unknown_book_returns_404(client):
    r = client.post(
        "/api/v1/reservation-books/deadbeef/reservations", json=_resv_body()
    )
    assert r.status_code == 404


def test_reserve_duplicate_flight_no_returns_409(client):
    bid = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    # against a fixed flight
    r = client.post(
        f"/api/v1/reservation-books/{bid}/reservations", json=_resv_body(flight_no="CA1")
    )
    assert r.status_code == 409
    assert "CA1" in r.json()["detail"]
    # against an earlier active reservation
    r = client.post(
        f"/api/v1/reservation-books/{bid}/reservations", json=_resv_body(flight_no="CA9")
    )
    assert r.status_code == 201
    r = client.post(
        f"/api/v1/reservation-books/{bid}/reservations", json=_resv_body(flight_no="CA9")
    )
    assert r.status_code == 409


def test_reserve_no_slot_returns_409(client):
    bid = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    # between CA1@00:00 (J) and CA2@00:05 (M): earliest feasible 00:03
    r = client.post(
        f"/api/v1/reservation-books/{bid}/reservations",
        json=_resv_body(latest="2026-09-18T00:02:59Z"),
    )
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "无可行空档" in detail
    assert "2026-09-18T00:03:00Z" in detail


def test_failed_reservation_leaves_no_record(client):
    bid = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    r = client.post(
        f"/api/v1/reservation-books/{bid}/reservations",
        json=_resv_body(latest="2026-09-18T00:02:59Z"),
    )
    assert r.status_code == 409
    book = client.get(f"/api/v1/reservation-books/{bid}").json()
    assert book["total_reservations"] == 0
    import app.database as database
    conn = sqlite3.connect(database.settings.db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM reservations WHERE book_id = ?", (bid,)
    ).fetchone()[0]
    conn.close()
    assert n == 0


def test_later_reservations_recompute_against_committed_ones(client):
    # zero M->M gaps via custom matrix; only J->M 180s matters.
    body = _book_body()
    body["matrix"] = [{"leader": "J", "follower": "M", "seconds": 180}]
    bid = client.post("/api/v1/reservation-books", json=body).json()["book_id"]
    first = client.post(
        f"/api/v1/reservation-books/{bid}/reservations",
        json=_resv_body(flight_no="R1", earliest="2026-09-18T00:00:00Z",
                        latest="2026-09-18T00:30:00Z"),
    ).json()
    assert first["assigned_off"] == "2026-09-18T00:03:00Z"
    # second reservation window starts before 00:03: earliest feasible slot
    # recomputed against R1 is 00:03 as well (tie of instant -> sorts after R1)
    second = client.post(
        f"/api/v1/reservation-books/{bid}/reservations",
        json=_resv_body(flight_no="R2", earliest="2026-09-18T00:00:00Z",
                        latest="2026-09-18T00:30:00Z"),
    ).json()
    assert second["assigned_off"] == "2026-09-18T00:03:00Z"
    assert second["order"] == ["CA1", "R1", "R2", "CA2"]
    book = client.get(f"/api/v1/reservation-books/{bid}").json()
    positions = {x["flight_no"]: x["position"] for x in book["reservations"]}
    assert positions == {"R1": 2, "R2": 3}
    assert book["order"] == ["CA1", "R1", "R2", "CA2"]


def test_reservation_validation_errors(client):
    bid = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    url = f"/api/v1/reservation-books/{bid}/reservations"
    # naive datetime
    r = client.post(url, json=_resv_body(earliest="2026-09-18T00:00:30"))
    assert r.status_code == 422 and "时区" in r.text
    # earliest after latest
    r = client.post(
        url, json=_resv_body(earliest="2026-09-18T00:11:00Z",
                             latest="2026-09-18T00:10:00Z")
    )
    assert r.status_code == 422 and "最早离场时刻不能晚于最晚" in r.text
    # subsecond precision
    r = client.post(url, json=_resv_body(earliest="2026-09-18T00:00:30.5Z"))
    assert r.status_code == 422 and "整秒" in r.text
    # invalid wake
    assert client.post(url, json=_resv_body(wake="X")).status_code == 422
    # empty flight number
    assert client.post(url, json=_resv_body(flight_no=" ")).status_code == 422
    # extra field
    assert client.post(url, json=_resv_body(locked=True)).status_code == 422


def test_reserve_honours_frozen_closure_and_capacity(client):
    body = _book_body()
    body["runway"]["capacity"] = {"window_seconds": 600, "max_departures": 2}
    body["runway"]["closures"] = [
        {"start": "2026-09-18T00:10:00Z", "end": "2026-09-18T00:20:00Z"}
    ]
    body["matrix"] = [{"leader": "J", "follower": "M", "seconds": 0}]
    bid = client.post("/api/v1/reservation-books", json=body).json()["book_id"]
    # 2 per 600s: third departure no earlier than CA1@00:00 + 600 = 00:10,
    # which falls in the closure -> 00:20
    r = client.post(
        f"/api/v1/reservation-books/{bid}/reservations",
        json=_resv_body(earliest="2026-09-18T00:00:00Z",
                        latest="2026-09-18T00:30:00Z"),
    )
    assert r.status_code == 201
    assert r.json()["assigned_off"] == "2026-09-18T00:20:00Z"
    assert r.json()["order"] == ["CA1", "CA2", "CA9"]


# ---------- query ----------


def test_get_book_unknown_returns_404(client):
    assert client.get("/api/v1/reservation-books/nope").status_code == 404


def test_get_book_returns_current_state(client):
    bid = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    client.post(
        f"/api/v1/reservation-books/{bid}/reservations", json=_resv_body(flight_no="R1")
    )
    client.post(
        f"/api/v1/reservation-books/{bid}/reservations",
        json=_resv_body(flight_no="R2", earliest="2026-09-18T00:03:01Z",
                        latest="2026-09-18T00:10:00Z"),
    )
    book = client.get(f"/api/v1/reservation-books/{bid}").json()
    assert book["total_fixed"] == 2
    assert book["total_reservations"] == 2
    assert book["order"] == ["CA1", "R1", "R2", "CA2"]
    by_no = {x["flight_no"]: x for x in book["reservations"]}
    assert by_no["R1"]["position"] == 2
    assert by_no["R2"]["position"] == 3
    assert by_no["R1"]["assigned_off"].endswith("Z")
    # reservations are listed in final departure order, not creation order issues
    assert [x["flight_no"] for x in book["reservations"]] == ["R1", "R2"]


# ---------- cancel ----------


def test_cancel_releases_slot_immediately(client):
    bid = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    rid = client.post(
        f"/api/v1/reservation-books/{bid}/reservations", json=_resv_body(flight_no="R1")
    ).json()["reservation_id"]
    code = client.delete(
        f"/api/v1/reservation-books/{bid}/reservations/{rid}"
    ).status_code
    assert code == 204
    book = client.get(f"/api/v1/reservation-books/{bid}").json()
    assert book["total_reservations"] == 0
    assert book["order"] == ["CA1", "CA2"]
    # flight number and the 00:03 slot are usable again
    r = client.post(
        f"/api/v1/reservation-books/{bid}/reservations", json=_resv_body(flight_no="R1")
    )
    assert r.status_code == 201
    assert r.json()["assigned_off"] == "2026-09-18T00:03:00Z"


def test_cancel_is_idempotent_204(client):
    bid = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    rid = client.post(
        f"/api/v1/reservation-books/{bid}/reservations", json=_resv_body()
    ).json()["reservation_id"]
    assert client.delete(
        f"/api/v1/reservation-books/{bid}/reservations/{rid}"
    ).status_code == 204
    assert client.delete(
        f"/api/v1/reservation-books/{bid}/reservations/{rid}"
    ).status_code == 204


def test_cancel_unknown_book_and_reservation_404(client):
    bid = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    assert client.delete(
        f"/api/v1/reservation-books/{bid}/reservations/nope"
    ).status_code == 404
    # a reservation of another book is also unknown here
    other = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    rid = client.post(
        f"/api/v1/reservation-books/{other}/reservations", json=_resv_body()
    ).json()["reservation_id"]
    assert client.delete(
        f"/api/v1/reservation-books/{bid}/reservations/{rid}"
    ).status_code == 404
    assert client.delete(
        f"/api/v1/reservation-books/nope/reservations/{rid}"
    ).status_code == 404


def test_fixed_plan_cannot_be_canceled(client):
    # fixed flights are not reservations: their identifiers never resolve,
    # so attempting to cancel one is a 404 on the reservation id
    bid = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    assert client.delete(
        f"/api/v1/reservation-books/{bid}/reservations/CA1"
    ).status_code == 404
    book = client.get(f"/api/v1/reservation-books/{bid}").json()
    assert book["total_fixed"] == 2
    assert book["order"] == ["CA1", "CA2"]


# ---------- concurrency (same process, serialized transactions) ----------


def test_concurrent_reservations_serialized(client):
    body = _book_body()
    body["matrix"] = [
        {"leader": "J", "follower": "M", "seconds": 180},
        {"leader": "M", "follower": "M", "seconds": 60},
    ]
    bid = client.post("/api/v1/reservation-books", json=body).json()["book_id"]

    results: list = []
    barrier = threading.Barrier(10)

    def reserve(i):
        barrier.wait()
        r = client.post(
            f"/api/v1/reservation-books/{bid}/reservations",
            json=_resv_body(
                flight_no=f"CC{i:02d}",
                earliest="2026-09-18T00:00:00Z",
                latest="2026-09-18T00:08:00Z",
            ),
        )
        results.append(r)

    threads = [threading.Thread(target=reserve, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    created = [r for r in results if r.status_code == 201]
    conflicts = [r for r in results if r.status_code == 409]
    # frozen CA1@00:00 (J), CA2@00:05 (M): reservation slots within
    # [00:00,00:08] are 00:03,00:04 (between, M->CA2 needs 60s) then
    # 00:06,00:07,00:08 (after CA2 at 60s packing) — 5 in total
    assert len(created) == 5, [(r.status_code, r.text[:60]) for r in results]
    assert len(conflicts) == 5
    slots = sorted(r.json()["assigned_off"] for r in created)
    assert slots == [
        "2026-09-18T00:03:00Z",
        "2026-09-18T00:04:00Z",
        "2026-09-18T00:06:00Z",
        "2026-09-18T00:07:00Z",
        "2026-09-18T00:08:00Z",
    ]
    book = client.get(f"/api/v1/reservation-books/{bid}").json()
    assert book["total_reservations"] == 5
    assert len(book["order"]) == 7


def test_concurrent_same_flight_no_single_winner(client):
    bid = client.post("/api/v1/reservation-books", json=_book_body()).json()["book_id"]
    outcomes: list[int] = []
    barrier = threading.Barrier(2)

    def reserve():
        barrier.wait()
        r = client.post(
            f"/api/v1/reservation-books/{bid}/reservations",
            json=_resv_body(flight_no="DUP"),
        )
        outcomes.append(r.status_code)

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == [201, 409]
    assert client.get(
        f"/api/v1/reservation-books/{bid}"
    ).json()["total_reservations"] == 1
