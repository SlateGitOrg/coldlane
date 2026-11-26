"""The full seeded dataset: each injected pattern must be recovered by the analysis."""

import pytest

from coldlane import config as cfg
from coldlane.analyse import class_counts, lane_leg_cells, site_leg_cells
from coldlane.registry import REGISTRY, reference_values


def ids(con, sql):
    return {r[0] for r in con.execute(sql).fetchall()}


def test_dataset_size_and_shape(dataset):
    con = dataset.con
    assert con.execute("SELECT count(*) FROM shipment").fetchone() == (1200,)
    assert con.execute("SELECT count(DISTINCT lot_id) FROM shipment").fetchone() == (300,)
    assert con.execute("SELECT count(DISTINCT lane_id) FROM shipment").fetchone() == (6,)
    assert dict(
        con.execute("SELECT mode, count(DISTINCT lane_id) FROM shipment GROUP BY 1").fetchall()
    ) == {
        "air": 2,
        "ground": 4,
    }
    assert con.execute("SELECT count(DISTINCT product) FROM shipment").fetchone() == (3,)
    assert con.execute("SELECT count(DISTINCT month(ship_ts)) FROM shipment").fetchone() == (12,)
    assert 150_000 < dataset.stats.readings < 250_000
    assert con.execute("SELECT count(DISTINCT origin) FROM shipment").fetchone() == (1,)


def test_each_lot_has_a_single_product(dataset):
    assert dataset.con.execute(
        "SELECT count(*) FROM "
        "(SELECT lot_id FROM shipment GROUP BY 1 HAVING count(DISTINCT product) > 1)"
    ).fetchone() == (0,)


def test_deduplication_removes_exactly_the_injected_duplicates(dataset):
    m = dataset.manifest
    assert dataset.stats.duplicates_removed == m["duplicates_injected"]
    assert dataset.stats.readings == m["readings_unique"]
    assert dataset.stats.rows_uploaded == m["readings_written"]
    assert dataset.stats.conflicting_duplicates == 0
    assert m["duplicates_injected"] / m["readings_unique"] == pytest.approx(0.02, abs=0.0005)


def test_no_duplicate_readings_remain(dataset):
    assert dataset.con.execute(
        "SELECT count(*) FROM (SELECT 1 FROM reading GROUP BY shipment_id, ts HAVING count(*) > 1)"
    ).fetchone() == (0,)


def test_flatline_detector_returns_exactly_the_nine_lg7_shipments_all_indeterminate(dataset):
    con = dataset.con
    flat = ids(con, "SELECT shipment_id FROM flatline")
    assert flat == set(dataset.manifest["flatline_ids"])
    assert len(flat) == 9
    lg7 = ids(con, "SELECT shipment_id FROM shipment WHERE logger_id LIKE 'LG-7-%'")
    assert lg7 == flat
    rows = con.execute(
        "SELECT DISTINCT shipment_class, indeterminate_reason FROM shipment_summary "
        "WHERE flatlined = 1"
    ).fetchall()
    assert rows == [(cfg.INDETERMINATE, "flatline")]
    assert con.execute(
        "SELECT count(*) FROM reading WHERE shipment_id IN (SELECT shipment_id FROM flatline) "
        "AND temp_c <> 5.0"
    ).fetchone() == (0,)


def test_exactly_12_gap_over_risk_leg_shipments_are_indeterminate_because_of_gaps(dataset):
    con = dataset.con
    risky = ids(con, "SELECT shipment_id FROM gap WHERE risk_gap")
    assert risky == set(dataset.manifest["gap_risk_ids"]) and len(risky) == 12
    rows = con.execute(
        "SELECT DISTINCT shipment_class, indeterminate_reason FROM shipment_summary "
        "WHERE risk_gap = 1"
    ).fetchall()
    assert rows == [(cfg.INDETERMINATE, "gap_over_risk_leg")]
    legs = {
        r[0] for r in con.execute("SELECT DISTINCT risk_leg FROM gap WHERE risk_gap").fetchall()
    }
    assert legs == {"tarmac", "hub_dwell"}


def test_the_other_28_gap_shipments_keep_their_computed_class(dataset):
    con = dataset.con
    all_gap = ids(con, "SELECT shipment_id FROM gap")
    others = all_gap - set(dataset.manifest["gap_risk_ids"])
    assert (
        others == set(dataset.manifest["gap_other_ids"])
        and len(others) == 28
        and len(all_gap) == 40
    )
    classes = {
        r[0]
        for r in con.execute(
            "SELECT shipment_class FROM shipment_summary WHERE shipment_id IN "
            f"({', '.join(repr(i) for i in others)})"
        ).fetchall()
    }
    assert cfg.INDETERMINATE not in classes
    # and nothing else is indeterminate: 12 gap + 9 stuck-logger shipments
    assert dict(class_counts(con))[cfg.INDETERMINATE] == 21


def test_injected_gaps_are_50_to_90_minutes(dataset):
    lo, hi = dataset.con.execute("SELECT min(gap_minutes), max(gap_minutes) FROM gap").fetchone()
    assert lo >= 50.0 and hi <= 90.0


def test_the_five_freezing_shipments_are_rejects_and_nothing_else_is(dataset):
    con = dataset.con
    rejects = ids(con, "SELECT shipment_id FROM shipment_summary WHERE shipment_class = 'reject'")
    assert rejects == set(dataset.manifest["freezing_ids"]) and len(rejects) == 5
    assert con.execute(
        "SELECT DISTINCT reject_reason FROM shipment_summary WHERE is_reject = 1"
    ).fetchall() == [("freeze",)]
    winter = con.execute(
        "SELECT count(*) FROM shipment_summary WHERE is_reject = 1 AND mode = 'ground' "
        "AND month(ship_date) IN (12, 1, 2) AND min_temp_c <= 0"
    ).fetchone()
    assert winter == (5,)


def test_dock_spike_shipments_are_excursions_never_rejects(dataset):
    con = dataset.con
    spikes = dataset.manifest["dock_spike_ids"]
    assert len(spikes) == 96  # 8% of 1200 deliveries
    in_list = ", ".join(repr(i) for i in spikes)
    classes = con.execute(
        f"SELECT shipment_class, count(*) FROM shipment_summary WHERE shipment_id IN ({in_list}) "
        "GROUP BY 1"
    ).fetchall()
    assert classes == [(cfg.EXCURSION, 96)]
    # a spike shipment whose only excursion is the spike has exactly 10 minutes
    only = con.execute(
        f"SELECT count(*) FROM shipment_summary WHERE shipment_id IN ({in_list}) "
        "AND excursion_minutes = 10"
    ).fetchone()[0]
    assert only == 87  # seed 2025: 9 spike shipments also have a neighbouring or other excursion
    assert con.execute(
        "SELECT count(*) FROM shipment_summary WHERE is_reject = 1 AND minutes_above_8 <= 10 "
        "AND minutes_below_2 = 0"
    ).fetchone() == (0,)


def test_dock_spike_minutes_sit_on_the_destination_dock_leg(dataset):
    spikes = dataset.manifest["dock_spike_ids"]
    in_list = ", ".join(repr(i) for i in spikes)
    n = dataset.con.execute(
        "SELECT count(*) FROM leg_excursions WHERE leg = 'destination_dock' "
        f"AND above_8_minutes >= 10 AND shipment_id IN ({in_list})"
    ).fetchone()[0]
    assert n == len(spikes) == 96


def test_top_lane_leg_cell_is_an_air_lane_tarmac(dataset):
    # at lane grain the two air lanes each have a tarmac cell, so neither is the whole story
    lane, leg, minutes = lane_leg_cells(dataset.con, limit=1)[0]
    assert leg == "tarmac" and lane.startswith("AIR-")
    assert minutes > 5_000


def test_hub_b_air_tarmac_is_the_top_site_cell_with_at_least_60_percent_of_summer_minutes(dataset):
    # at site grain (as the Hub Pareto groups it) HUB-B tarmac is one cell for both air lanes
    cells = site_leg_cells(dataset.con, months=(6, 7, 8))
    total = sum(m for _, _, m in cells)
    site, leg, minutes = cells[0]
    assert (site, leg) == ("HUB-B", "tarmac")
    assert minutes / total >= 0.60
    top_site = dataset.con.execute(
        "SELECT site FROM leg_excursions GROUP BY site ORDER BY sum(excursion_minutes) DESC LIMIT 1"
    ).fetchone()
    assert top_site == ("HUB-B",)
    # the same minutes, split by lane, are the two air-lane tarmac cells
    by_lane = [
        m for lane, leg, m in lane_leg_cells(dataset.con, months=(6, 7, 8)) if leg == "tarmac"
    ]
    assert len(by_lane) == 2 and sum(by_lane) == pytest.approx(minutes)


def test_tarmac_excursions_are_a_summer_air_phenomenon(dataset):
    rows = dataset.con.execute(
        "SELECT month(ship_month) IN (6, 7, 8) AS summer, sum(excursion_minutes) "
        "FROM leg_excursions WHERE leg = 'tarmac' GROUP BY 1"
    ).fetchall()
    by = dict(rows)
    assert by[True] > 20 * by.get(False, 0)
    assert dataset.con.execute(
        "SELECT count(*) FROM leg_excursions WHERE leg IN ('tarmac', 'hub_dwell', 'flight') "
        "AND mode = 'ground'"
    ).fetchone() == (0,)


def test_summer_air_tarmac_shipments_reach_8_c_and_the_hub_dwell_does_not(dataset):
    con = dataset.con
    n = con.execute(
        "SELECT count(*) FROM leg_excursions WHERE leg = 'tarmac' AND month(ship_month) IN (6,7,8) "
        "AND excursion_minutes > 0"
    ).fetchone()[0]
    assert n >= 60
    assert con.execute(
        "SELECT sum(excursion_minutes) FROM leg_excursions WHERE leg = 'hub_dwell'"
    ).fetchone() == (0.0,)


def test_mkt_is_never_below_the_time_weighted_mean(dataset):
    bad = dataset.con.execute(
        """
        SELECT count(*) FROM shipment_summary s JOIN (
            SELECT shipment_id, sum(temp_c * covered_min) / sum(covered_min) AS mean_c
            FROM reading GROUP BY shipment_id HAVING sum(covered_min) > 0
        ) m USING (shipment_id)
        WHERE s.mkt_c < m.mean_c - 0.006
        """
    ).fetchone()
    assert bad == (0,)


def test_coverage_is_100_percent_except_where_readings_are_missing(dataset):
    con = dataset.con
    partial = ids(con, "SELECT shipment_id FROM shipment_summary WHERE coverage_pct < 100")
    assert partial == set(dataset.manifest["gap_risk_ids"]) | set(dataset.manifest["gap_other_ids"])
    assert con.execute("SELECT min(coverage_pct) FROM shipment_summary").fetchone()[0] > 90


def test_covered_plus_missing_minutes_equal_the_logged_span(dataset):
    bad = dataset.con.execute(
        """
        SELECT count(*) FROM (
            SELECT shipment_id, sum(covered_min + missing_min) AS accounted,
                   date_diff('second', min(ts), max(ts)) / 60.0 AS span
            FROM reading GROUP BY shipment_id
        ) WHERE abs(accounted - span) > 1e-6
        """
    ).fetchone()
    assert bad == (0,)


def test_leg_minutes_reconcile_with_shipment_minutes(dataset):
    con = dataset.con
    legs = con.execute("SELECT sum(excursion_minutes) FROM leg_excursions").fetchone()[0]
    ships = con.execute("SELECT sum(excursion_minutes) FROM shipment_summary").fetchone()[0]
    assert legs == pytest.approx(ships)
    lots = con.execute("SELECT sum(minutes_used) FROM lot_budget").fetchone()[0]
    assert lots == pytest.approx(ships)


def test_classes_partition_the_shipments(dataset):
    counts = dict(class_counts(dataset.con))
    assert sum(counts.values()) == 1200
    assert counts[cfg.REJECT] == 5
    assert counts[cfg.COMPLIANT] > 800


def test_some_lots_spend_more_than_their_stability_budget(dataset):
    over = dataset.con.execute(
        "SELECT count(*), min(product) FROM lot_budget WHERE budget_status = 'over'"
    ).fetchone()
    assert over[0] >= 1
    assert dataset.con.execute(
        "SELECT count(*) FROM lot_budget WHERE budget_status = 'over' AND product = 'A'"
    ).fetchone() == (0,)


def test_registry_reference_values_match_independent_counts(dataset):
    con = dataset.con
    ref = reference_values(con)
    assert set(ref) == {m.name for m in REGISTRY}
    counts = dict(class_counts(con))
    assert ref["Reject Rate"] == pytest.approx(counts[cfg.REJECT] / 1200)
    assert ref["Indeterminate Rate"] == pytest.approx(counts[cfg.INDETERMINATE] / 1200)
    with_excursion = con.execute(
        "SELECT count(*) FROM shipment_summary WHERE excursion_minutes > 0"
    ).fetchone()[0]
    assert ref["Excursion Rate"] == pytest.approx(with_excursion / 1200)
    assert 4.0 < ref["Avg MKT"] < 6.0
    used, allowance = con.execute(
        "SELECT sum(minutes_used), sum(allowance_minutes) FROM lot_budget"
    ).fetchone()
    assert ref["Budget Used %"] == pytest.approx(used / allowance)
