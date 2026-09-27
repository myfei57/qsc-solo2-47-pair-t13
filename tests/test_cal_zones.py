"""Calender stack: zoned nip closure, thickness gating and reel verdicts."""

from support import ok, refused


def prepare_speed(console) -> None:
    ok(console, "POST", "/api/press/dewater", {"inlet_moisture": 60, "width": 6})
    ok(console, "POST", "/api/press/persist", {"speed": 900})
    ok(console, "POST", "/api/dry/speed", {"speed": 900})
    ok(console, "POST", "/api/cal/speed", {"speed": 900, "target": 900})


def close_and_accept(console, zone: str, pressure: float, thickness: float):
    if not any(z["zone"] == zone for z in ok(console, "GET", "/api/cal/zones")["zones"] if z["closed"]):
        ok(console, "POST", "/api/cal/zone/close", {"zone": zone, "pressure": pressure})
    return ok(
        console,
        "POST",
        "/api/cal/zone/measure",
        {"zone": zone, "thickness": thickness},
    )


def run_all_zones(console) -> None:
    close_and_accept(console, "nip1", 60, 0.120)
    close_and_accept(console, "nip2", 70, 0.100)
    close_and_accept(console, "nip3", 80, 0.080)


def test_zones_close_in_a_fixed_order(console):
    prepare_speed(console)
    body = ok(console, "GET", "/api/cal/procedure")
    assert body["stages"] == ["nip1", "nip2", "nip3"]
    assert body["next"] == "nip1"

    response = refused(
        console, "POST", "/api/cal/zone/close", {"zone": "nip2", "pressure": 70}
    )
    assert response.status == 409
    assert "nip1" in response.body["error"]

    close_and_accept(console, "nip1", 60, 0.120)
    response = refused(
        console, "POST", "/api/cal/zone/close", {"zone": "nip3", "pressure": 80}
    )
    assert response.status == 409
    assert "nip2" in response.body["error"]

    close_and_accept(console, "nip2", 70, 0.100)
    close_and_accept(console, "nip3", 80, 0.080)
    assert ok(console, "GET", "/api/cal/procedure")["next"] == "complete"


def test_each_zone_has_its_own_thickness_target(console):
    prepare_speed(console)
    ok(console, "POST", "/api/cal/zone/close", {"zone": "nip1", "pressure": 60})
    zones = {z["zone"]: z for z in ok(console, "GET", "/api/cal/zones")["zones"]}
    assert (zones["nip1"]["target_low"], zones["nip1"]["target_high"]) == (0.110, 0.130)
    assert (zones["nip2"]["target_low"], zones["nip2"]["target_high"]) == (0.090, 0.108)
    assert (zones["nip3"]["target_low"], zones["nip3"]["target_high"]) == (0.072, 0.088)
    assert zones["nip1"]["verdict"] == "unmeasured"


def test_a_zone_out_of_band_blocks_the_next_zone(console):
    prepare_speed(console)
    close_and_accept(console, "nip1", 60, 0.120)
    ok(console, "POST", "/api/cal/zone/close", {"zone": "nip2", "pressure": 70})

    thin = refused(
        console,
        "POST",
        "/api/cal/zone/measure",
        {"zone": "nip2", "thickness": 0.080},
    )
    assert thin.status == 409
    assert "thin" in thin.body["error"]

    # nip2 never qualified, so nip3 must stay shut even with a good pressure.
    jumped = refused(
        console, "POST", "/api/cal/zone/close", {"zone": "nip3", "pressure": 80}
    )
    assert jumped.status == 409
    assert "nip2" in jumped.body["error"]

    zones = {z["zone"]: z for z in ok(console, "GET", "/api/cal/zones")["zones"]}
    assert zones["nip2"]["verdict"] == "thin"
    assert zones["nip2"]["accepted"] is False
    assert zones["nip3"]["closed"] is False

    # measure again, this time inside nip2's band: the block releases.
    close_and_accept(console, "nip2", 70, 0.100)
    assert ok(console, "GET", "/api/cal/procedure")["next"] == "nip3"


def test_a_thick_zone_is_blocked_until_re_measured(console):
    prepare_speed(console)
    close_and_accept(console, "nip1", 60, 0.120)
    ok(console, "POST", "/api/cal/zone/close", {"zone": "nip2", "pressure": 70})
    thick = refused(
        console,
        "POST",
        "/api/cal/zone/measure",
        {"zone": "nip2", "thickness": 0.120},
    )
    assert thick.status == 409
    assert "thick" in thick.body["error"]
    body = close_and_accept(console, "nip2", 70, 0.095)
    assert body["verdict"] == "in_band"
    assert body["accepted"] is True


def test_failed_attempts_are_recorded_for_later_review(console):
    prepare_speed(console)
    ok(console, "POST", "/api/cal/zone/close", {"zone": "nip1", "pressure": 60})
    refused(
        console, "POST", "/api/cal/zone/measure", {"zone": "nip1", "thickness": 0.150}
    )
    refused(
        console, "POST", "/api/cal/zone/measure", {"zone": "nip1", "thickness": 0.090}
    )
    close_and_accept(console, "nip1", 60, 0.120)
    zone = ok(console, "GET", "/api/cal/zones?zone=nip1")
    assert zone["attempts"] == 3
    assert zone["verdict"] == "in_band"


def test_zone_close_needs_a_durable_settled_speed(console):
    refused(
        console, "POST", "/api/cal/zone/close", {"zone": "nip1", "pressure": 60}
    ).status == 409
    ok(console, "POST", "/api/press/dewater", {"inlet_moisture": 60, "width": 6})
    ok(console, "POST", "/api/press/persist", {"speed": 900})
    ok(console, "POST", "/api/dry/speed", {"speed": 900})
    ok(console, "POST", "/api/cal/speed", {"speed": 900, "target": 800})
    response = refused(
        console, "POST", "/api/cal/zone/close", {"zone": "nip1", "pressure": 60}
    )
    assert response.status == 409
    assert "not settled" in response.body["error"]


def test_measure_only_applies_to_a_closed_zone(console):
    prepare_speed(console)
    response = refused(
        console,
        "POST",
        "/api/cal/zone/measure",
        {"zone": "nip1", "thickness": 0.120},
    )
    assert response.status == 409


def test_an_accepted_zone_cannot_be_measured_again(console):
    prepare_speed(console)
    close_and_accept(console, "nip1", 60, 0.120)
    response = refused(
        console,
        "POST",
        "/api/cal/zone/measure",
        {"zone": "nip1", "thickness": 0.121},
    )
    assert response.status == 409


def test_an_unknown_zone_is_unprocessable(console):
    prepare_speed(console)
    response = refused(
        console, "POST", "/api/cal/zone/close", {"zone": "nip9", "pressure": 60}
    )
    assert response.status == 422


def test_zones_can_be_opened_back_from_the_top(console):
    prepare_speed(console)
    run_all_zones(console)
    # cannot open nip1 while later nips are still loaded
    response = refused(console, "POST", "/api/cal/zone/open", {"zone": "nip1"})
    assert response.status == 409
    ok(console, "POST", "/api/cal/zone/open", {"zone": "nip3"})
    ok(console, "POST", "/api/cal/zone/open", {"zone": "nip2"})
    body = ok(console, "POST", "/api/cal/zone/open", {"zone": "nip1"})
    assert body["nip_closed"] is False
    assert ok(console, "GET", "/api/cal/procedure")["next"] == "nip1"
    zones = {z["zone"]: z for z in ok(console, "GET", "/api/cal/zones")["zones"]}
    assert all(not z["closed"] for z in zones.values())
    # closing nip1 keeps its original speed/load gating; the reel gate reopens
    # only once nip1 is loaded again.
    close_and_accept(console, "nip1", 60, 0.120)
    close_and_accept(console, "nip2", 70, 0.100)
    close_and_accept(console, "nip3", 80, 0.080)


def test_open_zone_without_a_name_opens_the_top_closed_one(console):
    prepare_speed(console)
    close_and_accept(console, "nip1", 60, 0.120)
    ok(console, "POST", "/api/cal/zone/close", {"zone": "nip2", "pressure": 70})
    body = ok(console, "POST", "/api/cal/zone/open", {})
    assert body["zone"] == "nip2"
    zones = {z["zone"]: z for z in ok(console, "GET", "/api/cal/zones")["zones"]}
    assert zones["nip1"]["closed"] is True
    assert zones["nip2"]["closed"] is False


def test_a_spool_change_freezes_each_zone_verdict(console, services):
    prepare_speed(console)
    close_and_accept(console, "nip1", 60, 0.120)
    ok(console, "POST", "/api/cal/zone/close", {"zone": "nip2", "pressure": 70})
    refused(
        console, "POST", "/api/cal/zone/measure", {"zone": "nip2", "thickness": 0.080}
    )

    # turn up the reel: turning up a new spool freezes every zone as it stands
    ok(console, "POST", "/api/reel/change", {"reason": "full spool"})
    ok(console, "POST", "/api/reel/spool", {"spool_id": "S-77"})
    frozen = ok(console, "GET", "/api/cal/reel?spool_id=S-77")
    zones = {z["zone"]: z for z in frozen["zones"]}
    assert frozen["spool_id"] == "S-77"
    assert zones["nip1"]["verdict"] == "in_band"
    assert zones["nip1"]["accepted"] is True
    assert zones["nip2"]["verdict"] == "thin"
    assert zones["nip2"]["accepted"] is False
    assert zones["nip3"]["verdict"] == "unmeasured"


def test_spool_verdict_can_be_queried_long_after_the_fact(console, services):
    prepare_speed(console)
    run_all_zones(console)
    ok(console, "POST", "/api/reel/change", {"reason": "full spool"})
    ok(console, "POST", "/api/reel/spool", {"spool_id": "S-101"})
    ok(console, "POST", "/api/reel/retract", {})
    ok(console, "POST", "/api/reel/bite", {})

    # a later spool changes the live state; S-101's verdict must stay frozen
    ok(console, "POST", "/api/reel/change", {"reason": "full spool"})
    ok(console, "POST", "/api/reel/spool", {"spool_id": "S-102"})
    assert services.cal.active_spool() == "S-102"

    record = services.cal.spool_verdict("S-101")
    verdicts = {z["zone"]: z["verdict"] for z in record["zones"]}
    assert verdicts == {"nip1": "in_band", "nip2": "in_band", "nip3": "in_band"}

    missing = refused(console, "GET", "/api/cal/reel?spool_id=S-404")
    assert missing.status == 404


def test_spool_verdict_survives_a_restart(config):
    from support import build

    first = build(config)
    prepare_speed(first.console)
    close_and_accept(first.console, "nip1", 60, 0.120)
    close_and_accept(first.console, "nip2", 70, 0.100)
    ok(first.console, "POST", "/api/reel/change", {"reason": "full spool"})
    ok(first.console, "POST", "/api/reel/spool", {"spool_id": "S-202"})

    second = build(config)
    record = second.cal.spool_verdict("S-202")
    verdicts = {z["zone"]: z["verdict"] for z in record["zones"]}
    assert verdicts["nip1"] == "in_band"
    assert verdicts["nip2"] == "in_band"
    assert verdicts["nip3"] == "unmeasured"
    assert second.cal.active_spool() == "S-202"
    procedure = second.cal.procedure()
    assert procedure["completed"] == ["nip1", "nip2"]
    zones = {z.spec.name: z for z in second.cal.zones()}
    assert zones["nip1"].accepted is True
    assert zones["nip2"].pressure == 70.0


def test_state_view_reports_the_whole_stack(console):
    prepare_speed(console)
    run_all_zones(console)
    cal = ok(console, "GET", "/api/state")["subsystems"]["cal"]
    assert cal["nip_closed"] is True
    assert [z["zone"] for z in cal["zones"]] == ["nip1", "nip2", "nip3"]
    assert cal["procedure"]["next"] == "complete"


def test_legacy_single_nip_endpoint_drives_the_first_zone(console):
    prepare_speed(console)
    assert ok(console, "POST", "/api/cal/nip", {"closed": True, "pressure": 60})["zone"] == "nip1"
    assert ok(console, "GET", "/api/cal/state")["nip_closed"] is True
    assert ok(console, "POST", "/api/cal/nip", {"closed": False})["nip_closed"] is False
    assert ok(console, "GET", "/api/cal/procedure")["next"] == "nip1"
