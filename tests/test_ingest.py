import duckdb
import pytest
from helpers import run, series, ship, write_dataset

from coldlane.ingest import ingest, load

AIR_LEGS = [
    ("origin_dock", 0),
    ("linehaul", 60),
    ("hub_dwell", 200),
    ("tarmac", 400),
    ("flight", 600),
]


def legs_of(con, shipment_id="S1"):
    rows = con.execute(
        "SELECT cast(date_diff('minute', timestamp '2025-06-01', ts) AS INTEGER), leg "
        "FROM reading WHERE shipment_id = ? ORDER BY ts",
        [shipment_id],
    ).fetchall()
    return dict(rows)


def test_exact_duplicate_rows_are_removed(tmp_path):
    readings = series([5.0, 5.1, 5.2, 5.3])
    s = ship(readings=readings + [readings[1], readings[1], readings[3]])
    con = duckdb.connect()
    write_dataset(tmp_path, [s])
    stats = load(con, tmp_path)
    assert stats.rows_uploaded == 7
    assert stats.readings == 4
    assert stats.duplicates_removed == 3
    assert stats.conflicting_duplicates == 0


def test_conflicting_duplicate_keeps_the_warmer_reading_and_is_counted(tmp_path):
    s = ship(readings=[(0, 5.0), (10, 5.0), (10, 9.0), (20, 5.0)])
    con = duckdb.connect()
    write_dataset(tmp_path, [s])
    stats = load(con, tmp_path)
    assert stats.conflicting_duplicates == 1
    assert con.execute(
        "SELECT temp_c FROM reading WHERE ts = timestamp '2025-06-01 00:10:00'"
    ).fetchone() == (9.0,)


def test_asof_join_assigns_each_reading_to_the_latest_milestone(tmp_path):
    s = ship(
        readings=series([5.0] * 6, start=20),  # minutes 20..70
        milestones=[("origin_dock", 0), ("linehaul", 40), ("hub_dwell", 65)],
    )
    con = run(tmp_path, [s])
    assert legs_of(con) == {
        20: "origin_dock",
        30: "origin_dock",
        40: "linehaul",  # a reading exactly on a milestone belongs to the new leg
        50: "linehaul",
        60: "linehaul",
        70: "hub_dwell",
    }


def test_reading_before_the_first_milestone_is_unassigned(tmp_path):
    s = ship(readings=series([5.0, 5.0, 5.0], start=-10), milestones=[("origin_dock", 0)])
    con = run(tmp_path, [s])
    assert legs_of(con) == {-10: "unassigned", 0: "origin_dock", 10: "origin_dock"}


def test_leg_span_closes_each_leg_at_the_next_milestone(tmp_path):
    con = run(tmp_path, [ship(readings=series([5.0] * 3), milestones=AIR_LEGS, delivered=900)])
    spans = con.execute(
        "SELECT leg, date_diff('minute', leg_start, leg_end) FROM leg_span ORDER BY leg_start"
    ).fetchall()
    assert spans == [
        ("origin_dock", 60),
        ("linehaul", 140),
        ("hub_dwell", 200),
        ("tarmac", 200),
        ("flight", 300),
    ]


def test_intervals_are_capped_at_ten_minutes_and_the_rest_is_missing(tmp_path):
    con = run(tmp_path, [ship(readings=[(0, 5.0), (10, 5.0), (60, 5.0), (65, 5.0)])])
    rows = con.execute(
        "SELECT interval_min, covered_min, missing_min FROM reading ORDER BY ts"
    ).fetchall()
    assert rows == [(10.0, 10.0, 0.0), (50.0, 10.0, 40.0), (5.0, 5.0, 0.0), (None, 0.0, 0.0)]


def test_gap_marking_uses_a_15_minute_threshold(tmp_path):
    s = ship(readings=[(0, 5.0), (10, 5.0), (25, 5.0), (41, 5.0), (51, 5.0)])
    con = run(tmp_path, [s])
    assert con.execute("SELECT gap_minutes FROM gap ORDER BY gap_start").fetchall() == [(16.0,)]


def test_gap_of_30_minutes_over_tarmac_is_a_risk_gap(tmp_path):
    readings = [(400, 5.0), (410, 5.0), (420, 5.0), (450, 5.0), (460, 5.0)]
    con = run(tmp_path, [ship(readings=readings, milestones=AIR_LEGS, delivered=900)])
    assert con.execute("SELECT gap_minutes, risk_leg, risk_gap FROM gap").fetchall() == [
        (30.0, "tarmac", True)
    ]


def test_gap_of_29_minutes_over_tarmac_is_not_a_risk_gap(tmp_path):
    readings = [(400, 5.0), (410, 5.0), (439, 5.0), (449, 5.0)]
    con = run(tmp_path, [ship(readings=readings, milestones=AIR_LEGS, delivered=900)])
    assert con.execute("SELECT gap_minutes, risk_gap FROM gap").fetchall() == [(29.0, False)]


def test_gap_wholly_inside_linehaul_is_not_a_risk_gap(tmp_path):
    readings = [(70, 5.0), (80, 5.0), (140, 5.0), (150, 5.0)]
    con = run(tmp_path, [ship(readings=readings, milestones=AIR_LEGS, delivered=900)])
    assert con.execute("SELECT gap_minutes, risk_leg, risk_gap FROM gap").fetchall() == [
        (60.0, None, False)
    ]


def test_gap_that_ends_inside_hub_dwell_overlaps_it(tmp_path):
    readings = [(150, 5.0), (160, 5.0), (230, 5.0), (240, 5.0)]  # 160 -> 230 crosses minute 200
    con = run(tmp_path, [ship(readings=readings, milestones=AIR_LEGS, delivered=900)])
    assert con.execute("SELECT risk_leg, risk_gap FROM gap").fetchall() == [("hub_dwell", True)]


def test_gap_spanning_both_risk_legs_is_reported_once(tmp_path):
    readings = [(300, 5.0), (310, 5.0), (700, 5.0), (710, 5.0)]
    con = run(tmp_path, [ship(readings=readings, milestones=AIR_LEGS, delivered=900)])
    assert con.execute("SELECT risk_leg, risk_gap FROM gap").fetchall() == [
        ("hub_dwell+tarmac", True)
    ]


def test_36_identical_readings_are_a_flatline(tmp_path):
    con = run(tmp_path, [ship(readings=series([5.0] * 36))])
    assert con.execute("SELECT shipment_id FROM flatline").fetchall() == [("S1",)]


def test_35_identical_readings_are_not_a_flatline(tmp_path):
    con = run(tmp_path, [ship(readings=series([5.0] * 35))])
    assert con.execute("SELECT count(*) FROM flatline").fetchone() == (0,)


def test_a_run_of_identical_readings_broken_by_a_real_change_is_not_flat(tmp_path):
    con = run(tmp_path, [ship(readings=series([5.0] * 35 + [6.0] + [5.0] * 35))])
    assert con.execute("SELECT count(*) FROM flatline").fetchone() == (0,)


def test_normal_logger_noise_is_not_a_flatline(tmp_path):
    temps = [5.0 + 0.2 * (i % 3) for i in range(120)]
    con = run(tmp_path, [ship(readings=series(temps))])
    assert con.execute("SELECT count(*) FROM flatline").fetchone() == (0,)


def test_flatline_window_must_be_consecutive_readings(tmp_path):
    # 40 readings that vary slightly early on, then a 36-reading flat tail
    temps = [4.0 + 0.5 * (i % 2) for i in range(40)] + [5.0] * 36
    con = run(tmp_path, [ship(readings=series(temps))])
    flat = con.execute("SELECT flat_windows FROM flatline").fetchone()
    assert flat == (1,)


def test_missing_input_files_raise_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="coldlane generate"):
        load(duckdb.connect(), tmp_path)


def test_ingest_writes_a_database_file(tmp_path):
    write_dataset(tmp_path / "d", [ship(readings=series([5.0, 5.0]))])
    stats = ingest(tmp_path / "d", tmp_path / "out" / "x.duckdb")
    assert stats.readings == 2
    con = duckdb.connect(str(tmp_path / "out" / "x.duckdb"), read_only=True)
    assert con.execute("SELECT count(*) FROM reading").fetchone() == (2,)
