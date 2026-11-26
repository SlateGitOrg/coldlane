"""The three-shipment sample committed under sample/."""

import duckdb
import pytest

from coldlane import config as cfg
from coldlane.analyse import analyse
from coldlane.ingest import load


@pytest.fixture(scope="module")
def sample(repo):
    con = duckdb.connect()
    stats = load(con, repo / "sample")
    analyse(con)
    return con, stats


def row(con, sid):
    cur = con.execute("SELECT * FROM shipment_summary WHERE shipment_id = ?", [sid])
    return dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))


def test_sample_ingest_counts(sample):
    _, stats = sample
    assert (stats.shipments, stats.rows_uploaded, stats.readings) == (3, 307, 301)
    assert stats.duplicates_removed == 6 and stats.gaps == 1 and stats.risk_gaps == 1


def test_sample_summer_air_shipment_has_tarmac_excursion(sample):
    con, _ = sample
    s = row(con, "SHP-0636")
    assert s["shipment_class"] == cfg.EXCURSION and s["excursion_minutes"] == 100.0
    legs = dict(
        con.execute(
            "SELECT leg, excursion_minutes FROM leg_excursions "
            "WHERE shipment_id = 'SHP-0636' AND excursion_minutes > 0"
        ).fetchall()
    )
    # the box warms on the tarmac and is still above 8 C early in the flight
    assert legs == {"tarmac": 70.0, "flight": 30.0}


def test_sample_ground_shipment_is_compliant(sample):
    con, _ = sample
    s = row(con, "SHP-1192")
    assert s["shipment_class"] == cfg.COMPLIANT and s["coverage_pct"] == 100.0


def test_sample_gap_over_the_tarmac_is_indeterminate(sample):
    con, _ = sample
    s = row(con, "SHP-0577")
    assert s["shipment_class"] == cfg.INDETERMINATE
    assert s["indeterminate_reason"] == "gap_over_risk_leg"
    assert s["missing_minutes"] > 0 and s["coverage_pct"] < 100
