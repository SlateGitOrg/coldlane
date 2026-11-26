"""Load logger CSVs and milestones into DuckDB and derive the per-reading facts.

Tables created in the target database:

``shipment``, ``milestone``   the raw shipment and milestone files
``reading_raw``               every uploaded row, duplicates included
``reading``                   de-duplicated readings with leg, interval, covered and missing
                              minutes
``leg_span``                  start and end of every leg of every shipment
``gap``                       every spacing between readings above GAP_MIN, with the risky
                              legs it overlaps
``flatline``                  shipments with FLATLINE_WINDOW consecutive near-identical readings
``ingest_stats``              one row of counts
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb

from coldlane import config as cfg


@dataclass(frozen=True)
class IngestStats:
    shipments: int
    rows_uploaded: int
    readings: int
    duplicates_removed: int
    conflicting_duplicates: int
    gaps: int
    risk_gaps: int
    flatlined_shipments: int


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def load(con: duckdb.DuckDBPyConnection, data_dir: Path) -> IngestStats:
    """Ingest ``data_dir`` (shipments.csv, milestones.csv, loggers/*.csv) into ``con``."""
    data_dir = Path(data_dir)
    shipments_csv = data_dir / "shipments.csv"
    milestones_csv = data_dir / "milestones.csv"
    loggers_glob = data_dir / "loggers" / "*.csv"
    for path in (shipments_csv, milestones_csv):
        if not path.exists():
            raise FileNotFoundError(f"{path} not found; run `coldlane generate` first")
    if not list((data_dir / "loggers").glob("*.csv")):
        raise FileNotFoundError(f"no logger files in {data_dir / 'loggers'}")

    con.execute(
        """
        CREATE OR REPLACE TABLE shipment AS
        SELECT * FROM read_csv(?, header = true, columns = {
            'shipment_id': 'VARCHAR', 'lot_id': 'VARCHAR', 'product': 'VARCHAR',
            'lane_id': 'VARCHAR', 'mode': 'VARCHAR', 'origin': 'VARCHAR',
            'destination': 'VARCHAR', 'ship_ts': 'TIMESTAMP', 'logger_id': 'VARCHAR'})
        """,
        [str(shipments_csv)],
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE milestone AS
        SELECT * FROM read_csv(?, header = true, columns = {
            'shipment_id': 'VARCHAR', 'milestone': 'VARCHAR', 'ts': 'TIMESTAMP'})
        """,
        [str(milestones_csv)],
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE reading_raw AS
        SELECT * FROM read_csv(?, header = true, columns = {
            'shipment_id': 'VARCHAR', 'logger_id': 'VARCHAR',
            'ts': 'TIMESTAMP', 'temp_c': 'DOUBLE'})
        """,
        [str(loggers_glob)],
    )

    # A re-uploaded reading repeats the same (shipment, timestamp). Keep one row per key; if the
    # copies disagree, keep the warmer value, which is the conservative choice for a 2-8 C product.
    con.execute(
        """
        CREATE OR REPLACE TABLE reading_dedup AS
        SELECT shipment_id, any_value(logger_id) AS logger_id, ts,
               max(temp_c) AS temp_c, count(DISTINCT temp_c) AS variants
        FROM reading_raw GROUP BY shipment_id, ts
        """
    )

    cap = cfg.READING_INTERVAL_MIN
    con.execute(
        f"""
        CREATE OR REPLACE TABLE reading AS
        WITH timed AS (
            SELECT shipment_id, logger_id, ts, temp_c,
                   lead(ts) OVER (PARTITION BY shipment_id ORDER BY ts) AS next_ts
            FROM reading_dedup
        ),
        legged AS (
            SELECT t.shipment_id, t.logger_id, t.ts, t.temp_c, t.next_ts,
                   coalesce(nullif(m.milestone, '{cfg.DELIVERED}'), '{cfg.UNASSIGNED}') AS leg
            FROM timed t
            ASOF LEFT JOIN milestone m ON t.shipment_id = m.shipment_id AND t.ts >= m.ts
        )
        SELECT shipment_id, logger_id, ts, temp_c, next_ts, leg,
               date_diff('second', ts, next_ts) / 60.0 AS interval_min,
               least(coalesce(date_diff('second', ts, next_ts) / 60.0, 0), {cap}) AS covered_min,
               greatest(coalesce(date_diff('second', ts, next_ts) / 60.0, 0) - {cap}, 0)
                   AS missing_min
        FROM legged
        """
    )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE leg_span AS
        SELECT shipment_id, milestone AS leg, ts AS leg_start, leg_end
        FROM (
            SELECT shipment_id, milestone, ts,
                   lead(ts) OVER (PARTITION BY shipment_id ORDER BY ts) AS leg_end
            FROM milestone
        )
        WHERE milestone <> '{cfg.DELIVERED}'
        """
    )

    con.execute(
        f"""
        CREATE OR REPLACE TABLE gap AS
        SELECT g.shipment_id, g.gap_start, g.gap_end, g.gap_minutes,
               string_agg(s.leg, '+' ORDER BY s.leg_start) AS risk_leg,
               g.gap_minutes >= {cfg.RISK_GAP_MIN} AND count(s.leg) > 0 AS risk_gap
        FROM (
            SELECT shipment_id, ts AS gap_start, next_ts AS gap_end, interval_min AS gap_minutes
            FROM reading WHERE interval_min > {cfg.GAP_MIN}
        ) g
        LEFT JOIN leg_span s
          ON s.shipment_id = g.shipment_id
         AND s.leg IN ({_sql_list(cfg.RISK_LEGS)})
         AND g.gap_start < s.leg_end AND g.gap_end > s.leg_start
        GROUP BY g.shipment_id, g.gap_start, g.gap_end, g.gap_minutes
        """
    )

    n = cfg.FLATLINE_WINDOW
    con.execute(
        f"""
        CREATE OR REPLACE TABLE flatline AS
        SELECT shipment_id, count(*) AS flat_windows, min(ts) AS first_flat_end
        FROM (
            SELECT shipment_id, ts,
                   stddev_samp(temp_c) OVER w AS sd, count(*) OVER w AS n
            FROM reading
            WINDOW w AS (PARTITION BY shipment_id ORDER BY ts
                         ROWS BETWEEN {n - 1} PRECEDING AND CURRENT ROW)
        )
        WHERE n = {n} AND sd < {cfg.FLATLINE_STD_C}
        GROUP BY shipment_id
        """
    )

    stats = IngestStats(
        shipments=_scalar(con, "SELECT count(*) FROM shipment"),
        rows_uploaded=_scalar(con, "SELECT count(*) FROM reading_raw"),
        readings=_scalar(con, "SELECT count(*) FROM reading"),
        duplicates_removed=_scalar(
            con, "SELECT (SELECT count(*) FROM reading_raw) - count(*) FROM reading"
        ),
        conflicting_duplicates=_scalar(
            con, "SELECT count(*) FROM reading_dedup WHERE variants > 1"
        ),
        gaps=_scalar(con, "SELECT count(*) FROM gap"),
        risk_gaps=_scalar(con, "SELECT count(*) FROM gap WHERE risk_gap"),
        flatlined_shipments=_scalar(con, "SELECT count(*) FROM flatline"),
    )
    con.execute(
        "CREATE OR REPLACE TABLE ingest_stats AS SELECT ? AS shipments, ? AS rows_uploaded, "
        "? AS readings, ? AS duplicates_removed, ? AS conflicting_duplicates, ? AS gaps, "
        "? AS risk_gaps, ? AS flatlined_shipments",
        [
            stats.shipments,
            stats.rows_uploaded,
            stats.readings,
            stats.duplicates_removed,
            stats.conflicting_duplicates,
            stats.gaps,
            stats.risk_gaps,
            stats.flatlined_shipments,
        ],
    )
    return stats


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def ingest(data_dir: Path, db_path: Path) -> IngestStats:
    """Create (or replace the tables of) the database at ``db_path`` from ``data_dir``."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        return load(con, data_dir)
    finally:
        con.close()
