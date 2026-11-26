"""Write the three CSV extracts the Tableau workbook reads."""

from __future__ import annotations

from pathlib import Path

import duckdb

from coldlane import config as cfg

COLUMNS: dict[str, tuple[str, ...]] = {
    "shipment_summary": (
        "shipment_id", "lot_id", "product", "lane_id", "mode", "destination", "ship_date",
        "ship_month", "readings", "minutes_above_8", "minutes_below_2", "excursion_minutes",
        "max_temp_c", "min_temp_c", "mkt_c", "coverage_pct", "missing_minutes",
        "max_gap_minutes", "risk_gap", "flatlined", "shipment_class", "reject_reason",
        "indeterminate_reason", "has_excursion", "is_reject", "is_indeterminate",
    ),
    "leg_excursions": (
        "shipment_id", "lane_id", "mode", "product", "ship_month", "leg", "leg_seq", "site",
        "readings",
        "covered_minutes", "above_8_minutes", "below_2_minutes", "excursion_minutes",
        "missing_minutes",
    ),
    "lot_budget": (
        "lot_id", "product", "allowance_minutes", "shipments", "minutes_used",
        "unknown_minutes", "budget_used_pct", "budget_status",
    ),
}  # fmt: skip

_ORDER = {"shipment_summary": "shipment_id", "lot_budget": "lot_id"}
_LEG_ORDER = "list_position([" + ", ".join(f"'{leg}'" for leg in cfg.LEGS) + "], leg)"


def export(con: duckdb.DuckDBPyConnection, out_dir: Path) -> dict[str, int]:
    """Write ``shipment_summary.csv``, ``leg_excursions.csv`` and ``lot_budget.csv``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for table, columns in COLUMNS.items():
        target = str(out_dir / f"{table}.csv").replace("'", "''")
        cols = ", ".join(columns)
        order = f"shipment_id, {_LEG_ORDER}" if table == "leg_excursions" else _ORDER[table]
        query = f"SELECT {cols} FROM {table} ORDER BY {order}"
        con.execute(f"COPY ({query}) TO '{target}' (HEADER, DELIMITER ',')")
        row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert row is not None
        counts[table] = int(row[0])
    return counts
