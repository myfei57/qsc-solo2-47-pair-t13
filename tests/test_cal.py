"""Calender nip train: ordered closing, per-nip thickness targets and roll records."""

from support import build, ok, refused


def prepare_speed(console) -> None:
    ok(console, "POST", "/api/press/dewater", {"inlet_moisture": 60, "width": 6})
    ok(console, "POST", "/api/press/persist", {"speed": 900})
    ok(console, "POST", "/api/dry/speed", {"speed": 900})
    ok(console, "POST", "/api/cal/speed", {"speed": 900, "target": 900})


def close_nip(console, nip: str, thickness: float, pressure: float = 60):
    return ok(
        console,
        "POST",
        "/api/cal/nip",
        {"nip": nip, "pressure": pressure, "thickness": thickness},
    )


def close_stack(console) -> None:
    close_nip(console, "nip1", 0.094)
    close_nip(console, "nip2", 0.083)
    close_nip(console, "nip3", 0.073)


def test_nips_close_in_order_and_report_each_verdict(console, services):
    prepare_speed(console)
    assert close_nip(console, "nip1", 0.094)["verdict"] == "within"
    assert services.cal.nip_closed() is False
    close_nip(console, "nip2", 0.083)
    close_nip(console, "nip3", 0.073)
    assert services.cal.nip_closed() is True
    state = ok(console, "GET", "/api/cal/state")
    assert state["nip_closed"] is True
    assert state["next_nip"] == "complete"
    assert [nip["name"] for nip in state["nips"]] == ["nip1", "nip2", "nip3"]
    assert all(nip["closed"] for nip in state["nips"])
    assert {nip["verdict"] for nip in state["nips"]} == {"within"}


def test_a_nip_cannot_close_before_its_predecessor(console):
    prepare_speed(console)
    response = refused(
        console,
        "POST",
        "/api/cal/nip",
        {"nip": "nip2", "pressure": 60, "thickness": 0.083},
    )
    assert response.status == 409
    assert "cannot close nip2 before nip1" in response.body["error"]


def test_unknown_nip_is_rejected(console):
    prepare_speed(console)
    response = refused(
        console,
        "POST",
        "/api/cal/nip",
        {"nip": "nip9", "pressure": 60, "thickness": 0.09},
    )
    assert response.status == 422


def test_closing_an_already_closed_nip_is_rejected(console):
    prepare_speed(console)
    close_nip(console, "nip1", 0.094)
    response = refused(
        console,
        "POST",
        "/api/cal/nip",
        {"nip": "nip1", "pressure": 60, "thickness": 0.094},
    )
    assert response.status == 409
    assert "already closed" in response.body["error"]


def test_a_nip_off_its_thickness_target_blocks_the_train(console, services):
    prepare_speed(console)
    response = refused(
        console,
        "POST",
        "/api/cal/nip",
        {"nip": "nip1", "pressure": 60, "thickness": 0.12},
    )
    assert response.status == 422
    assert "outside" in response.body["error"]
    assert services.cal.nip_closed() is False
    # the failed nip stays open, so the train cannot move past it
    response = refused(
        console,
        "POST",
        "/api/cal/nip",
        {"nip": "nip2", "pressure": 60, "thickness": 0.083},
    )
    assert response.status == 409
    # the miss was judged and remembered before the train was blocked
    assert services.cal.nip_history("nip1").current() == "above"
    # an adjusted thickness closes the nip and the train moves on
    close_nip(console, "nip1", 0.094)
    assert services.cal.nip_history("nip1").current() == "within"


def test_every_judgement_is_kept_per_nip(console, services):
    prepare_speed(console)
    refused(
        console,
        "POST",
        "/api/cal/nip",
        {"nip": "nip1", "pressure": 60, "thickness": 0.05},
    )
    close_nip(console, "nip1", 0.094)
    points = services.cal.nip_history("nip1").points()
    assert [point.verdict for point in points] == ["below", "within"]
    # the per-nip history is also on the shared verdict history endpoint
    body = ok(console, "GET", "/api/verdict/history?subject=cal.nip.nip2")
    assert body["subject"] == "cal.nip.nip2"
    assert body["current"] == "unknown"


def test_opening_a_nip_reopens_the_tail_of_the_train(console, services):
    prepare_speed(console)
    close_stack(console)
    body = ok(console, "POST", "/api/cal/nip", {"nip": "nip2", "closed": False})
    assert body["opened"] == ["nip2", "nip3"]
    assert services.cal.nip_closed() is False
    state = ok(console, "GET", "/api/cal/state")
    assert state["nips"][0]["closed"] is True
    assert state["nips"][1]["closed"] is False
    assert state["next_nip"] == "nip2"
    # the reel pre-gate stays shut while the train is open
    response = refused(
        console,
        "POST",
        "/api/reel/spool",
        {"spool_id": "S-1"},
    )
    assert response.status == 409
    # the tail has to be closed in order again
    close_nip(console, "nip2", 0.083)
    close_nip(console, "nip3", 0.073)
    assert services.cal.nip_closed() is True


def test_nip_needs_a_durable_speed(console):
    response = refused(
        console,
        "POST",
        "/api/cal/nip",
        {"nip": "nip1", "pressure": 60, "thickness": 0.094},
    )
    assert response.status == 409
    assert "dry/speed" in response.body["error"]


def test_nip_waits_for_a_settled_speed(console):
    ok(console, "POST", "/api/press/dewater", {"inlet_moisture": 60, "width": 6})
    ok(console, "POST", "/api/press/persist", {"speed": 900})
    ok(console, "POST", "/api/dry/speed", {"speed": 900})
    ok(console, "POST", "/api/cal/speed", {"speed": 900, "target": 800})
    response = refused(
        console,
        "POST",
        "/api/cal/nip",
        {"nip": "nip1", "pressure": 60, "thickness": 0.094},
    )
    assert response.status == 409
    assert "not settled" in response.body["error"]


def test_roll_record_freezes_each_nip_verdict(console, services):
    prepare_speed(console)
    close_stack(console)
    ok(console, "POST", "/api/reel/change", {"reason": "full spool"})
    ok(console, "POST", "/api/reel/spool", {"spool_id": "S-1"})
    record = ok(console, "GET", "/api/cal/roll?spool_id=S-1")
    assert record["spool_id"] == "S-1"
    assert set(record["nips"]) == {"nip1", "nip2", "nip3"}
    nip2 = record["nips"]["nip2"]
    assert nip2["verdict"] == "within"
    assert nip2["thickness"] == 0.083
    assert nip2["closed"] is True
    assert ok(console, "GET", "/api/cal/roll")["rolls"] == ["S-1"]


def test_unknown_roll_is_not_found(console):
    response = refused(console, "GET", "/api/cal/roll?spool_id=S-9")
    assert response.status == 404


def test_roll_record_and_stack_survive_a_restart(config):
    first = build(config)
    prepare_speed(first.console)
    close_stack(first.console)
    ok(first.console, "POST", "/api/reel/change", {"reason": "full spool"})
    ok(first.console, "POST", "/api/reel/spool", {"spool_id": "S-1"})

    second = build(config)
    assert second.cal.nip_closed() is True
    assert second.cal.next_nip() == "complete"
    nips = second.cal.view().to_dict()["nips"]
    assert [nip["thickness"] for nip in nips] == [0.094, 0.083, 0.073]
    record = second.cal.roll_verdicts("S-1")
    assert record["nips"]["nip3"]["verdict"] == "within"


def test_cal_model_reports_the_arithmetic(console):
    prepare_speed(console)
    close_nip(console, "nip1", 0.094)
    body = ok(console, "GET", "/api/cal/model?width=6&dwell=0.05")
    assert body["nip_load"] == 360.0
    assert body["nip_impulse"] == 3.0
    assert body["speed_difference"] == 0.0
    assert body["settled"] is True
    assert body["nip_verdict"] == "in_band"
    assert body["verdict"] == "open"
    assert body["load"] == 360.0
