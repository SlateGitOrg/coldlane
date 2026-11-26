"""Constants shared by the generator, the ingest step and the analysis.

Everything that a reviewer might want to challenge (thresholds, the activation energy,
the stability allowances) lives here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- logger and time-weighting -------------------------------------------------------------
READING_INTERVAL_MIN = 10  # nominal logger period; a reading covers at most this many minutes
GAP_MIN = 15  # a spacing above this many minutes between readings is a gap
RISK_GAP_MIN = 30  # a gap of at least this many minutes over a risky leg is indeterminate
FLATLINE_WINDOW = 36  # consecutive readings
FLATLINE_STD_C = 0.05

# --- product temperature range and Arrhenius constants ------------------------------------
RANGE_LOW_C = 2.0
RANGE_HIGH_C = 8.0
REJECT_LOW_C = 0.0  # any reading <= this is a reject
REJECT_HIGH_C = 25.0  # any reading > this is a reject
DELTA_H_J_PER_MOL = 83_144.0  # 83.144 kJ/mol, the value used in pharmaceutical storage guidance
GAS_CONSTANT = 8.3144621  # J/(mol K)

# Minutes of cumulative time out of range a lot may spend before its stability budget is gone.
ALLOWANCE_MIN: dict[str, int] = {"A": 72 * 60, "B": 24 * 60, "C": 6 * 60}

# --- legs ----------------------------------------------------------------------------------
ORIGIN_DOCK = "origin_dock"
LINEHAUL = "linehaul"
HUB_DWELL = "hub_dwell"
TARMAC = "tarmac"
FLIGHT = "flight"
LAST_MILE = "last_mile"
DESTINATION_DOCK = "destination_dock"
DELIVERED = "delivered"  # terminal milestone, not a leg
UNASSIGNED = "unassigned"

LEGS = (ORIGIN_DOCK, LINEHAUL, HUB_DWELL, TARMAC, FLIGHT, LAST_MILE, DESTINATION_DOCK)
RISK_LEGS = (HUB_DWELL, TARMAC)

ORIGIN_HUB = "HUB-A"
AIR_HUB = "HUB-B"


def leg_site(leg: str, destination: str) -> str:
    """Where a leg happens, for the hub Pareto. Moving legs are their own site."""
    if leg == ORIGIN_DOCK:
        return ORIGIN_HUB
    if leg in (HUB_DWELL, TARMAC):
        return AIR_HUB
    if leg == LINEHAUL:
        return "Linehaul"
    if leg == FLIGHT:
        return "Flight"
    return destination


# --- classification ------------------------------------------------------------------------
REJECT = "reject"
INDETERMINATE = "indeterminate"
EXCURSION = "excursion_within_budget"
COMPLIANT = "compliant"
CLASSES = (REJECT, INDETERMINATE, EXCURSION, COMPLIANT)


@dataclass(frozen=True)
class Lane:
    lane_id: str
    mode: str
    destination: str
    linehaul_hours: tuple[float, float]
    weight: float


LANES = (
    Lane("AIR-SEA", "air", "SEA", (2.0, 4.0), 0.18),
    Lane("AIR-MIA", "air", "MIA", (2.0, 4.0), 0.18),
    Lane("GND-CHI", "ground", "CHI", (10.0, 20.0), 0.16),
    Lane("GND-ATL", "ground", "ATL", (16.0, 30.0), 0.16),
    Lane("GND-DAL", "ground", "DAL", (24.0, 40.0), 0.16),
    Lane("GND-DEN", "ground", "DEN", (30.0, 50.0), 0.16),
)
