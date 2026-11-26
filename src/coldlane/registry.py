"""The metric registry: one definition per metric, shared by the Python reference, the exported
CSVs and the Tableau workbook.

``formula`` is the Tableau calculated-field formula. ``sql`` computes the same number from the
analysis tables and is what the tests and the report treat as the reference value.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb


@dataclass(frozen=True)
class Metric:
    name: str
    datasource: str  # CSV extract (without .csv) the Tableau calculation lives in
    formula: str
    sql: str
    datatype: str
    description: str


REGISTRY: tuple[Metric, ...] = (
    Metric(
        "Excursion Minutes",
        "leg_excursions",
        "SUM([excursion_minutes])",
        "SELECT sum(excursion_minutes) FROM leg_excursions",
        "real",
        "Minutes above 8 C or below 2 C, integrated over reading intervals and pinned to a leg.",
    ),
    Metric(
        "Excursion Rate",
        "shipment_summary",
        "SUM([has_excursion]) / COUNTD([shipment_id])",
        "SELECT avg(has_excursion) FROM shipment_summary",
        "real",
        "Share of shipments with any computed excursion minute.",
    ),
    Metric(
        "Indeterminate Rate",
        "shipment_summary",
        "SUM([is_indeterminate]) / COUNTD([shipment_id])",
        "SELECT avg(is_indeterminate) FROM shipment_summary",
        "real",
        "Share of shipments that cannot be called compliant: risky gaps or a stuck logger.",
    ),
    Metric(
        "Reject Rate",
        "shipment_summary",
        "SUM([is_reject]) / COUNTD([shipment_id])",
        "SELECT avg(is_reject) FROM shipment_summary",
        "real",
        "Share of shipments rejected for freezing, overheating or exceeding the allowance.",
    ),
    Metric(
        "Budget Used %",
        "lot_budget",
        "SUM([minutes_used]) / SUM([allowance_minutes])",
        "SELECT sum(minutes_used) / sum(allowance_minutes) FROM lot_budget",
        "real",
        "Minutes out of range across a lot's shipments over the product's stability allowance.",
    ),
    Metric(
        "Avg MKT",
        "shipment_summary",
        "AVG([mkt_c])",
        "SELECT avg(mkt_c) FROM shipment_summary",
        "real",
        "Average of the per-shipment mean kinetic temperature, in Celsius.",
    ),
    Metric(
        "Cumulative Excursion %",
        "leg_excursions",
        "RUNNING_SUM(SUM([excursion_minutes])) / TOTAL(SUM([excursion_minutes]))",
        "SELECT max(m) / sum(m) FROM (SELECT sum(excursion_minutes) AS m "
        "FROM leg_excursions GROUP BY site)",
        "real",
        "Pareto line: running total of excursion minutes over sites, largest first, as a share "
        "of all minutes. Its first point is the largest site's share, which is the reference "
        "value.",
    ),
)

NAMES = tuple(m.name for m in REGISTRY)


def reference_values(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    """Evaluate every registry metric over the whole dataset."""
    out: dict[str, float] = {}
    for metric in REGISTRY:
        row = con.execute(metric.sql).fetchone()
        assert row is not None and row[0] is not None
        out[metric.name] = float(row[0])
    return out
