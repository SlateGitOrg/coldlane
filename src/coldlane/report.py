"""Markdown summary: classification counts, hottest lane x leg cells, lots nearest their budget."""

from __future__ import annotations

import duckdb

from coldlane.analyse import class_counts, closest_lots, lane_leg_cells
from coldlane.registry import reference_values


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def render_report(con: duckdb.DuckDBPyConnection) -> str:
    counts = class_counts(con)
    total = sum(n for _, n in counts) or 1
    classes = _table(
        ["class", "shipments", "share"],
        [[c, str(n), f"{100 * n / total:.1f}%"] for c, n in counts],
    )

    cells = lane_leg_cells(con, limit=5)
    grand = sum(m for _, _, m in lane_leg_cells(con)) or 1.0
    top_cells = _table(
        ["lane", "leg", "excursion minutes", "share of all"],
        [[lane, leg, f"{m:.0f}", f"{100 * m / grand:.1f}%"] for lane, leg, m in cells],
    )

    lots = _table(
        ["lot", "product", "allowance min", "minutes used", "budget used", "status"],
        [
            [lot, prod, str(allow), f"{used:.0f}", f"{pct:.1f}%", status]
            for lot, prod, allow, used, pct, status in closest_lots(con, 5)
        ],
    )

    metrics = reference_values(con)
    registry = _table(
        ["metric", "value"],
        [
            ["Excursion Minutes", f"{metrics['Excursion Minutes']:.0f}"],
            ["Excursion Rate", f"{100 * metrics['Excursion Rate']:.1f}%"],
            ["Indeterminate Rate", f"{100 * metrics['Indeterminate Rate']:.1f}%"],
            ["Reject Rate", f"{100 * metrics['Reject Rate']:.1f}%"],
            ["Budget Used %", f"{100 * metrics['Budget Used %']:.1f}%"],
            ["Avg MKT", f"{metrics['Avg MKT']:.2f} C"],
            [
                "Cumulative Excursion % (first Pareto point)",
                f"{100 * metrics['Cumulative Excursion %']:.1f}%",
            ],
        ],
    )
    return "\n\n".join(
        [
            "### Classification counts",
            classes,
            "### Top 5 lane x leg cells by excursion minutes",
            top_cells,
            "### 5 lots closest to their stability budget",
            lots,
            "### Registry metrics",
            registry,
        ]
    )
