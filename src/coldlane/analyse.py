"""Per-shipment metrics, leg-attributed excursion minutes and lot stability budgets.

Reads the tables written by :mod:`coldlane.ingest` and writes ``shipment_summary``,
``leg_excursions``, ``lot_budget`` and ``product`` into the same database.

Time out of range is the sum of ``covered_min`` over readings outside 2-8 C. Each reading
covers the interval to the next one, capped at 10 minutes, and the minutes beyond the cap are
``missing_min``: unknown time, never in range and never out of range. A reading's minutes belong
to the leg the reading was taken in.
"""

from __future__ import annotations

import duckdb
import numpy as np

from coldlane import config as cfg
from coldlane.classify import ShipmentFacts, classify
from coldlane.metrics import mean_kinetic_temperature

_SUMMARY_COLUMNS = (
    "shipment_id VARCHAR, lot_id VARCHAR, product VARCHAR, lane_id VARCHAR, mode VARCHAR, "
    "destination VARCHAR, ship_date DATE, ship_month DATE, readings INTEGER, "
    "minutes_above_8 DOUBLE, minutes_below_2 DOUBLE, excursion_minutes DOUBLE, "
    "max_temp_c DOUBLE, min_temp_c DOUBLE, mkt_c DOUBLE, coverage_pct DOUBLE, "
    "missing_minutes DOUBLE, max_gap_minutes DOUBLE, risk_gap INTEGER, flatlined INTEGER, "
    "shipment_class VARCHAR, reject_reason VARCHAR, indeterminate_reason VARCHAR, "
    "has_excursion INTEGER, is_reject INTEGER, is_indeterminate INTEGER"
)
_LOW, _HIGH = cfg.RANGE_LOW_C, cfg.RANGE_HIGH_C
_LEG_LIST = ", ".join(f"'{leg}'" for leg in cfg.LEGS)


def analyse(con: duckdb.DuckDBPyConnection) -> None:
    """Build the three analysis tables from the ingested readings."""
    _load_products(con)
    _shipment_summary(con)
    _leg_excursions(con)
    _lot_budget(con)


def _load_products(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE OR REPLACE TABLE product (product VARCHAR, allowance_minutes INTEGER)")
    con.executemany("INSERT INTO product VALUES (?, ?)", list(cfg.ALLOWANCE_MIN.items()))


def _shipment_summary(con: duckdb.DuckDBPyConnection) -> None:
    base = con.execute(
        f"""
        WITH per_reading AS (
            SELECT shipment_id, count(*) AS readings,
                   coalesce(sum(covered_min) FILTER (WHERE temp_c > {_HIGH}), 0) AS above_8,
                   coalesce(sum(covered_min)
                            FILTER (WHERE temp_c > {_HIGH} AND temp_c <= {cfg.REJECT_HIGH_C}), 0)
                       AS warm,
                   coalesce(sum(covered_min) FILTER (WHERE temp_c < {_LOW}), 0) AS below_2,
                   max(temp_c) AS max_t, min(temp_c) AS min_t,
                   sum(covered_min) AS covered, sum(missing_min) AS missing
            FROM reading GROUP BY shipment_id
        ),
        per_gap AS (
            SELECT shipment_id, max(gap_minutes) AS max_gap, bool_or(risk_gap) AS risk_gap
            FROM gap GROUP BY shipment_id
        )
        SELECT s.shipment_id, s.lot_id, s.product, s.lane_id, s.mode, s.destination,
               s.ship_ts::DATE, date_trunc('month', s.ship_ts)::DATE,
               coalesce(p.readings, 0), coalesce(p.above_8, 0), coalesce(p.warm, 0),
               coalesce(p.below_2, 0), p.max_t, p.min_t,
               coalesce(p.covered, 0), coalesce(p.missing, 0),
               coalesce(g.max_gap, 0), coalesce(g.risk_gap, false),
               f.shipment_id IS NOT NULL
        FROM shipment s
        LEFT JOIN per_reading p USING (shipment_id)
        LEFT JOIN per_gap g USING (shipment_id)
        LEFT JOIN flatline f USING (shipment_id)
        ORDER BY s.shipment_id
        """
    ).fetchall()

    mkt = _mkt_by_shipment(con)
    rows = []
    for (
        sid, lot, product, lane, mode, dest, ship_date, ship_month, n,
        above_8, warm, below_2, max_t, min_t, covered, missing, max_gap, risk_gap, flat,
    ) in base:  # fmt: skip
        verdict = classify(
            ShipmentFacts(
                product=product,
                readings=n,
                min_temp_c=min_t,
                max_temp_c=max_t,
                minutes_above_8=above_8,
                minutes_warm=warm,
                minutes_below_2=below_2,
                risk_gap=bool(risk_gap),
                flatlined=bool(flat),
            )
        )
        window = covered + missing
        coverage = round(100.0 * covered / window, 2) if window > 0 else 0.0
        excursion = above_8 + below_2
        rows.append(
            (
                sid, lot, product, lane, mode, dest, ship_date, ship_month, n,
                round(above_8, 2), round(below_2, 2), round(excursion, 2), max_t, min_t,
                None if sid not in mkt else round(mkt[sid], 2), coverage, round(missing, 2),
                round(max_gap, 2), int(risk_gap), int(flat),
                verdict.shipment_class, verdict.reject_reason or None,
                verdict.indeterminate_reason or None,
                int(excursion > 0), int(verdict.shipment_class == cfg.REJECT),
                int(verdict.shipment_class == cfg.INDETERMINATE),
            )
        )  # fmt: skip
    con.execute("DROP TABLE IF EXISTS shipment_summary")
    con.execute(f"CREATE TABLE shipment_summary ({_SUMMARY_COLUMNS})")
    con.executemany(f"INSERT INTO shipment_summary VALUES ({', '.join('?' * 26)})", rows)


def _mkt_by_shipment(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    data = con.execute(
        "SELECT shipment_id, temp_c, covered_min FROM reading ORDER BY shipment_id, ts"
    ).fetchnumpy()
    ids = np.asarray(data["shipment_id"], dtype=str)
    if ids.size == 0:
        return {}
    temps = np.asarray(data["temp_c"], dtype=float)
    weights = np.asarray(data["covered_min"], dtype=float)
    starts = np.flatnonzero(np.r_[True, ids[1:] != ids[:-1]])
    bounds = np.r_[starts, ids.size]
    return {
        str(ids[a]): mean_kinetic_temperature(temps[a:b], weights[a:b])
        for a, b in zip(bounds[:-1], bounds[1:], strict=True)
    }


def _leg_excursions(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        CREATE OR REPLACE TABLE leg_excursions AS
        SELECT r.shipment_id, s.lane_id, s.mode, s.product,
               date_trunc('month', s.ship_ts)::DATE AS ship_month, r.leg,
               CASE r.leg
                   WHEN '{cfg.ORIGIN_DOCK}' THEN '{cfg.ORIGIN_HUB}'
                   WHEN '{cfg.HUB_DWELL}' THEN '{cfg.AIR_HUB}'
                   WHEN '{cfg.TARMAC}' THEN '{cfg.AIR_HUB}'
                   WHEN '{cfg.LINEHAUL}' THEN 'Linehaul'
                   WHEN '{cfg.FLIGHT}' THEN 'Flight'
                   ELSE s.destination
               END AS site,
               list_position([{_LEG_LIST}], r.leg)::INTEGER AS leg_seq,
               count(*)::INTEGER AS readings,
               round(sum(r.covered_min), 2) AS covered_minutes,
               round(coalesce(sum(r.covered_min) FILTER (WHERE r.temp_c > {_HIGH}), 0), 2)
                   AS above_8_minutes,
               round(coalesce(sum(r.covered_min) FILTER (WHERE r.temp_c < {_LOW}), 0), 2)
                   AS below_2_minutes,
               round(coalesce(sum(r.covered_min)
                              FILTER (WHERE r.temp_c > {_HIGH} OR r.temp_c < {_LOW}), 0), 2)
                   AS excursion_minutes,
               round(sum(r.missing_min), 2) AS missing_minutes
        FROM reading r JOIN shipment s USING (shipment_id)
        GROUP BY r.shipment_id, s.lane_id, s.mode, s.product, s.destination, s.ship_ts, r.leg
        ORDER BY r.shipment_id, min(r.ts)
        """
    )


def _lot_budget(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE OR REPLACE TABLE lot_budget AS
        SELECT s.lot_id, s.product, p.allowance_minutes,
               count(*)::INTEGER AS shipments,
               round(sum(s.excursion_minutes), 2) AS minutes_used,
               round(sum(s.missing_minutes), 2) AS unknown_minutes,
               round(100.0 * sum(s.excursion_minutes) / p.allowance_minutes, 2)
                   AS budget_used_pct,
               CASE WHEN sum(s.excursion_minutes) > p.allowance_minutes
                    THEN 'over' ELSE 'within' END AS budget_status
        FROM shipment_summary s JOIN product p USING (product)
        GROUP BY s.lot_id, s.product, p.allowance_minutes
        ORDER BY s.lot_id
        """
    )


def class_counts(con: duckdb.DuckDBPyConnection) -> list[tuple[str, int]]:
    """Shipments per class, in the fixed class order (zero counts included)."""
    got = dict(
        con.execute(
            "SELECT shipment_class, count(*) FROM shipment_summary GROUP BY shipment_class"
        ).fetchall()
    )
    return [(c, int(got.get(c, 0))) for c in cfg.CLASSES]


def lane_leg_cells(
    con: duckdb.DuckDBPyConnection, months: tuple[int, ...] | None = None, limit: int | None = None
) -> list[tuple[str, str, float]]:
    """(lane, leg, excursion minutes) cells, largest first; optionally only some calendar months."""
    where = ""
    params: list[int] = []
    if months:
        where = f"WHERE month(ship_month) IN ({', '.join('?' * len(months))})"
        params = list(months)
    sql = f"""
        SELECT lane_id, leg, sum(excursion_minutes) AS minutes
        FROM leg_excursions {where}
        GROUP BY lane_id, leg HAVING sum(excursion_minutes) > 0
        ORDER BY minutes DESC, lane_id, leg
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [(a, b, float(c)) for a, b, c in con.execute(sql, params).fetchall()]


def site_leg_cells(
    con: duckdb.DuckDBPyConnection, months: tuple[int, ...] | None = None, limit: int | None = None
) -> list[tuple[str, str, float]]:
    """(site, leg, excursion minutes) cells, largest first.

    A site groups lanes, as the Hub Pareto does: HUB-B tarmac is one cell for both air lanes.
    """
    where = ""
    params: list[int] = []
    if months:
        where = f"WHERE month(ship_month) IN ({', '.join('?' * len(months))})"
        params = list(months)
    sql = f"""
        SELECT site, leg, sum(excursion_minutes) AS minutes
        FROM leg_excursions {where}
        GROUP BY site, leg HAVING sum(excursion_minutes) > 0
        ORDER BY minutes DESC, site, leg
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [(a, b, float(c)) for a, b, c in con.execute(sql, params).fetchall()]


def closest_lots(con: duckdb.DuckDBPyConnection, limit: int = 5) -> list[tuple]:
    """Lots with the highest share of their stability budget used."""
    return con.execute(
        """
        SELECT lot_id, product, allowance_minutes, minutes_used, budget_used_pct, budget_status
        FROM lot_budget ORDER BY budget_used_pct DESC, lot_id LIMIT ?
        """,
        [limit],
    ).fetchall()
