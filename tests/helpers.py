"""Build tiny logger datasets by hand so a test controls every reading."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from coldlane.analyse import analyse
from coldlane.ingest import load

BASE = datetime(2025, 6, 1)
DEFAULT_MILESTONES = [("origin_dock", 0), ("linehaul", 30)]


def _ts(minute: float) -> str:
    return (BASE + timedelta(minutes=minute)).strftime("%Y-%m-%d %H:%M:%S")


def ship(
    shipment_id: str = "S1",
    readings: list[tuple[float, float]] | None = None,
    product: str = "A",
    lot: str = "LOT-1",
    lane: str = "AIR-SEA",
    milestones: list[tuple[str, int]] | None = None,
    delivered: int | None = None,
) -> dict[str, Any]:
    readings = readings or []
    last = max((m for m, _ in readings), default=0)
    return {
        "id": shipment_id,
        "product": product,
        "lot": lot,
        "lane": lane,
        "readings": readings,
        "milestones": milestones or DEFAULT_MILESTONES,
        "delivered": delivered if delivered is not None else int(last) + 10,
    }


def series(temps: list[float], start: int = 0, step: int = 10) -> list[tuple[float, float]]:
    return [(start + i * step, t) for i, t in enumerate(temps)]


def write_dataset(root: Path, ships: list[dict[str, Any]]) -> Path:
    (root / "loggers").mkdir(parents=True, exist_ok=True)
    rows = ["shipment_id,lot_id,product,lane_id,mode,origin,destination,ship_ts,logger_id"]
    miles = ["shipment_id,milestone,ts"]
    for s in ships:
        mode = "air" if s["lane"].startswith("AIR") else "ground"
        rows.append(
            f"{s['id']},{s['lot']},{s['product']},{s['lane']},{mode},HUB-A,SEA,{_ts(0)},LG-1-0001"
        )
        for leg, minute in s["milestones"]:
            miles.append(f"{s['id']},{leg},{_ts(minute)}")
        miles.append(f"{s['id']},delivered,{_ts(s['delivered'])}")
        lines = ["shipment_id,logger_id,ts,temp_c"]
        lines += [f"{s['id']},LG-1-0001,{_ts(m)},{t}" for m, t in s["readings"]]
        (root / "loggers" / f"{s['id']}.csv").write_text("\n".join(lines) + "\n")
    (root / "shipments.csv").write_text("\n".join(rows) + "\n")
    (root / "milestones.csv").write_text("\n".join(miles) + "\n")
    return root


def run(root: Path, ships: list[dict[str, Any]]) -> duckdb.DuckDBPyConnection:
    """Write ``ships`` under ``root``, ingest and analyse them in an in-memory database."""
    write_dataset(root, ships)
    con = duckdb.connect()
    load(con, root)
    analyse(con)
    return con


def summary(con: duckdb.DuckDBPyConnection, shipment_id: str = "S1") -> dict[str, Any]:
    cur = con.execute("SELECT * FROM shipment_summary WHERE shipment_id = ?", [shipment_id])
    names = [d[0] for d in cur.description]
    row = cur.fetchone()
    assert row is not None
    return dict(zip(names, row, strict=True))
