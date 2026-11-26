import math

import pytest
from helpers import run, series, ship, summary

from coldlane import config as cfg

HOT = 9.0
COLD = 1.0


def leg_row(con, leg, shipment_id="S1"):
    cur = con.execute(
        "SELECT * FROM leg_excursions WHERE shipment_id = ? AND leg = ?", [shipment_id, leg]
    )
    names = [d[0] for d in cur.description]
    row = cur.fetchone()
    return None if row is None else dict(zip(names, row, strict=True))


def _warm(n):
    """n readings above 8 C that vary, so the logger does not look stuck."""
    return [HOT + 0.2 * (i % 2) for i in range(n)]


def test_time_weighting_fixture_30_excursion_minutes_and_30_missing(tmp_path):
    readings = [(0, HOT), (10, HOT), (20, HOT), (60, 5.0), (70, 5.0)]
    s = summary(run(tmp_path, [ship(readings=readings)]))
    assert s["minutes_above_8"] == 30.0
    assert s["excursion_minutes"] == 30.0
    assert s["missing_minutes"] == 30.0
    assert s["coverage_pct"] == pytest.approx(100 * 40 / 70, abs=0.01)


def test_missing_time_is_never_counted_as_in_range_or_out_of_range(tmp_path):
    readings = [(0, 5.0), (10, 5.0), (100, 5.0)]
    con = run(tmp_path, [ship(readings=readings)])
    s = summary(con)
    assert s["missing_minutes"] == 80.0 and s["excursion_minutes"] == 0.0
    assert leg_row(con, "origin_dock")["missing_minutes"] == 80.0


def test_range_limits_are_inclusive(tmp_path):
    s = summary(run(tmp_path, [ship(readings=series([2.0, 8.0, 2.0, 8.0, 5.0]))]))
    assert s["excursion_minutes"] == 0.0
    assert s["shipment_class"] == cfg.COMPLIANT


def test_below_2_minutes_are_counted_separately(tmp_path):
    s = summary(run(tmp_path, [ship(readings=series([5.0, COLD, COLD, 5.0, 5.0]))]))
    assert s["minutes_below_2"] == 20.0
    assert s["minutes_above_8"] == 0.0
    assert s["excursion_minutes"] == 20.0
    assert s["shipment_class"] == cfg.EXCURSION


def test_max_min_and_mkt_are_reported(tmp_path):
    s = summary(run(tmp_path, [ship(readings=series([4.0, 6.0, 5.0, 5.0]))]))
    assert (s["max_temp_c"], s["min_temp_c"]) == (6.0, 4.0)
    # weights 10, 10, 10, 0: the last reading covers no time, so MKT is over 4, 6 and 5 C
    k = 83_144.0 / 8.3144621
    rate = sum(math.exp(-k / (t + 273.15)) for t in (4.0, 6.0, 5.0)) / 3
    assert s["mkt_c"] == pytest.approx(k / -math.log(rate) - 273.15, abs=0.006)
    assert s["mkt_c"] >= 5.0


def test_excursion_minutes_are_pinned_to_the_leg_they_happened_in(tmp_path):
    legs = [
        ("origin_dock", 0),
        ("linehaul", 60),
        ("hub_dwell", 120),
        ("tarmac", 180),
        ("flight", 240),
    ]
    readings = series([5.0] * 6, 0) + series([HOT] * 6, 180) + series([5.0] * 6, 240)
    con = run(tmp_path, [ship(readings=readings, milestones=legs, delivered=400)])
    tarmac = leg_row(con, "tarmac")
    assert tarmac["excursion_minutes"] == 60.0 and tarmac["above_8_minutes"] == 60.0
    assert tarmac["site"] == "HUB-B"
    for leg in ("origin_dock", "linehaul", "hub_dwell", "flight"):
        row = leg_row(con, leg)
        assert row is None or row["excursion_minutes"] == 0.0


def test_leg_minutes_reconcile_with_the_shipment_total(tmp_path):
    legs = [("origin_dock", 0), ("linehaul", 50), ("last_mile", 100)]
    readings = series([5.0, HOT, HOT, 5.0, COLD, 5.0, HOT, HOT, 5.0, 5.0, 5.0])
    con = run(tmp_path, [ship(readings=readings, milestones=legs, delivered=200)])
    total = con.execute("SELECT sum(excursion_minutes) FROM leg_excursions").fetchone()[0]
    assert total == summary(con)["excursion_minutes"] == 50.0


def test_lot_fixture_product_c_with_40_and_50_minutes_uses_25_percent(tmp_path):
    a = ship("S1", series([HOT] * 4 + [5.0] * 4), product="C", lot="LOT-C")
    b = ship("S2", series([5.0] * 2 + [HOT] * 5 + [5.0] * 2), product="C", lot="LOT-C")
    con = run(tmp_path, [a, b])
    lot = con.execute("SELECT * FROM lot_budget WHERE lot_id = 'LOT-C'").fetchone()
    cols = [d[0] for d in con.description]
    lot = dict(zip(cols, lot, strict=True))
    assert lot["allowance_minutes"] == 360
    assert lot["minutes_used"] == 90.0
    assert lot["budget_used_pct"] == 25.0
    assert lot["shipments"] == 2
    assert lot["budget_status"] == "within"


def test_lot_over_its_allowance_is_flagged_over(tmp_path):
    a = ship("S1", series(_warm(20) + [5.0] * 2), product="C", lot="LOT-C")  # 200 min
    b = ship("S2", series(_warm(20) + [5.0] * 2), product="C", lot="LOT-C")  # 200 min
    con = run(tmp_path, [a, b])
    assert con.execute(
        "SELECT minutes_used, budget_used_pct, budget_status FROM lot_budget"
    ).fetchone() == (
        400.0,
        pytest.approx(111.11),
        "over",
    )


def test_the_same_minutes_use_less_budget_for_a_larger_allowance(tmp_path):
    a = ship("S1", series([HOT] * 6 + [5.0] * 2), product="A", lot="LOT-A")
    b = ship("S2", series([HOT] * 6 + [5.0] * 2), product="B", lot="LOT-B")
    con = run(tmp_path, [a, b])
    pct = dict(con.execute("SELECT lot_id, budget_used_pct FROM lot_budget").fetchall())
    assert pct == {"LOT-A": pytest.approx(1.39, abs=0.01), "LOT-B": pytest.approx(4.17, abs=0.01)}


def test_unknown_minutes_are_reported_per_lot(tmp_path):
    s = ship("S1", [(0, 5.0), (10, 5.0), (70, 5.0), (80, 5.0)], lot="LOT-G")
    con = run(tmp_path, [s])
    assert con.execute("SELECT unknown_minutes, minutes_used FROM lot_budget").fetchone() == (
        50.0,
        0.0,
    )


@pytest.mark.parametrize(
    ("temps", "reason"),
    [([5.0, 5.2, 0.0, 5.0], "freeze"), ([5.0, 5.2, 25.5, 5.0], "overheat")],
)
def test_reject_by_a_single_extreme_reading(tmp_path, temps, reason):
    s = summary(run(tmp_path, [ship(readings=series(temps))]))
    assert s["shipment_class"] == cfg.REJECT and s["reject_reason"] == reason
    assert s["is_reject"] == 1


def test_reject_when_warm_time_exceeds_the_product_allowance(tmp_path):
    over = ship("S1", series(_warm(37) + [5.0] * 2), product="C")  # 370 min > 360
    at = ship("S2", series(_warm(36) + [5.0] * 2), product="C")  # 360 min
    con = run(tmp_path, [over, at])
    assert summary(con, "S1")["shipment_class"] == cfg.REJECT
    assert summary(con, "S1")["reject_reason"] == "allowance"
    assert summary(con, "S2")["shipment_class"] == cfg.EXCURSION


def test_a_ten_minute_dock_spike_is_an_excursion_not_a_reject(tmp_path):
    legs = [("origin_dock", 0), ("destination_dock", 50)]
    readings = series([5.0] * 4) + [(50, 5.0), (60, 9.4), (70, 5.0), (80, 5.0), (90, 5.0)]
    con = run(tmp_path, [ship(readings=readings, milestones=legs, delivered=120)])
    s = summary(con)
    assert s["excursion_minutes"] == 10.0
    assert s["shipment_class"] == cfg.EXCURSION
    assert leg_row(con, "destination_dock")["excursion_minutes"] == 10.0


def test_gap_over_tarmac_makes_a_clean_looking_shipment_indeterminate(tmp_path):
    legs = [("origin_dock", 0), ("hub_dwell", 50), ("tarmac", 100), ("flight", 300)]
    readings = [(0, 5.0), (10, 5.0), (90, 5.0), (100, 5.0), (110, 5.0), (170, 5.0), (180, 5.0)]
    con = run(tmp_path, [ship(readings=readings, milestones=legs, delivered=400)])
    s = summary(con)
    assert s["excursion_minutes"] == 0.0  # what the readings alone say
    assert s["shipment_class"] == cfg.INDETERMINATE
    assert s["indeterminate_reason"] == "gap_over_risk_leg"
    assert s["is_indeterminate"] == 1 and s["has_excursion"] == 0


def test_gap_over_a_safe_leg_keeps_the_computed_class(tmp_path):
    legs = [("origin_dock", 0), ("linehaul", 30), ("hub_dwell", 400)]
    readings = [(30, 5.0), (40, 5.0), (120, 5.0), (130, 5.0)]
    s = summary(run(tmp_path, [ship(readings=readings, milestones=legs, delivered=500)]))
    assert s["max_gap_minutes"] == 80.0
    assert s["shipment_class"] == cfg.COMPLIANT


def test_flatlined_shipment_is_indeterminate(tmp_path):
    s = summary(run(tmp_path, [ship(readings=series([5.0] * 40))]))
    assert s["flatlined"] == 1
    assert s["shipment_class"] == cfg.INDETERMINATE and s["indeterminate_reason"] == "flatline"


def test_lane_leg_cells_and_class_counts(tmp_path):
    from coldlane.analyse import class_counts, lane_leg_cells

    legs = [("origin_dock", 0), ("tarmac", 50)]
    hot = ship("S1", series([5.0] * 5 + [HOT] * 4 + [5.0] * 2), milestones=legs, delivered=200)
    ok = ship("S2", series([5.0] * 6), milestones=legs, lane="GND-CHI")
    con = run(tmp_path, [hot, ok])
    assert lane_leg_cells(con) == [("AIR-SEA", "tarmac", 40.0)]
    assert dict(class_counts(con)) == {
        "reject": 0,
        "indeterminate": 0,
        "excursion_within_budget": 1,
        "compliant": 1,
    }
