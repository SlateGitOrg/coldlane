"""Shipment classification: reject, indeterminate, excursion within budget, compliant.

The order matters. A reading outside the physical limits is evidence and rejects the
shipment whatever else is wrong with the logger. A missing or stuck reading is not evidence of
control, so those shipments are indeterminate and never compliant. Only then do the computed
excursion minutes decide between an excursion and a clean shipment.
"""

from __future__ import annotations

from dataclasses import dataclass

from coldlane import config as cfg


@dataclass(frozen=True)
class ShipmentFacts:
    product: str
    readings: int
    min_temp_c: float | None
    max_temp_c: float | None
    minutes_above_8: float
    minutes_warm: float  # minutes with 8 C < reading <= 25 C
    minutes_below_2: float
    risk_gap: bool  # a gap of at least 30 minutes overlapping a tarmac or hub-dwell leg
    flatlined: bool


@dataclass(frozen=True)
class Verdict:
    shipment_class: str
    reject_reason: str
    indeterminate_reason: str


def classify(facts: ShipmentFacts) -> Verdict:
    allowance = cfg.ALLOWANCE_MIN[facts.product]
    reasons: list[str] = []
    if facts.min_temp_c is not None and facts.min_temp_c <= cfg.REJECT_LOW_C:
        reasons.append("freeze")
    if facts.max_temp_c is not None and facts.max_temp_c > cfg.REJECT_HIGH_C:
        reasons.append("overheat")
    if facts.minutes_warm > allowance:
        reasons.append("allowance")
    if reasons:
        return Verdict(cfg.REJECT, "+".join(reasons), "")

    unknown: list[str] = []
    if facts.readings == 0:
        unknown.append("no_data")
    if facts.risk_gap:
        unknown.append("gap_over_risk_leg")
    if facts.flatlined:
        unknown.append("flatline")
    if unknown:
        return Verdict(cfg.INDETERMINATE, "", "+".join(unknown))

    if facts.minutes_above_8 > 0 or facts.minutes_below_2 > 0:
        return Verdict(cfg.EXCURSION, "", "")
    return Verdict(cfg.COMPLIANT, "", "")
