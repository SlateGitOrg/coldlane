"""Seeded synthetic cold-chain data: shipments, milestones and 10-minute logger CSVs.

The in-box temperature follows the ambient temperature of the current leg through a
first-order lag. Heating towards a warmer ambient uses a 6 h time constant. Legs that are
actively refrigerated (cold rooms, reefer trucks, the active container in the aircraft hold)
pull the payload back down with a shorter 0.75 h constant, because the refrigerant packs are
re-chilled there. That constant is a modelling choice, not a measurement: with a single 6 h
constant a 3 h tarmac dwell would leave the box above 8 C for the whole of the next leg. The
concentration of excursion minutes on the tarmac in the analysis follows from this choice and
from the injected tarmac dwell; it is a check that the pipeline recovers the planted pattern.

Injected patterns (see Manifest for the exact shipment ids):

* every June-August air shipment sits 2-4 h on the HUB-B tarmac at 28-34 C
* 9 shipments carry loggers from batch LG-7 that are stuck at 5.0 C
* 40 shipments have a 50-90 minute logger gap; 12 of them sit on the tarmac or in the hub
  dwell, the other 28 lie wholly inside a linehaul or flight leg
* 8% of deliveries have a single reading above 8 C on the destination dock (<= 10 minutes)
* 5 winter ground shipments dip below 0 C during linehaul
* 2% of the readings are uploaded twice
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from coldlane import config as cfg

YEAR = 2025
N_SHIPMENTS = 1200
N_LOTS = 300
N_FREEZING = 5
N_GAP_RISK = 12
N_GAP_OTHER = 28
N_FLATLINE = 9
DOCK_SPIKE_RATE = 0.08
DUPLICATE_RATE = 0.02
SUMMER_MONTHS = (6, 7, 8)
WINTER_MONTHS = (12, 1, 2)

TAU_WARM_H = 6.0
TAU_CHILL_H = 0.75
START_C = 4.5
NOISE_SD_C = 0.15
PRODUCT_WEIGHTS = {"A": 0.40, "B": 0.35, "C": 0.25}
STUCK_C = 5.0


@dataclass(frozen=True)
class LegPlan:
    name: str
    minutes: int
    ambient_c: float
    chilled: bool


@dataclass
class Manifest:
    """Ground truth about what was injected; the tests assert the analysis recovers it."""

    seed: int
    shipments: int
    lots: int
    readings_written: int
    readings_unique: int
    duplicates_injected: int
    flatline_ids: list[str] = field(default_factory=list)
    gap_risk_ids: list[str] = field(default_factory=list)
    gap_other_ids: list[str] = field(default_factory=list)
    freezing_ids: list[str] = field(default_factory=list)
    dock_spike_ids: list[str] = field(default_factory=list)
    summer_tarmac_ids: list[str] = field(default_factory=list)


@dataclass
class _Shipment:
    shipment_id: str
    lot_id: str
    product: str
    lane: cfg.Lane
    ship_ts: datetime
    logger_id: str = ""
    legs: list[LegPlan] = field(default_factory=list)


def _season(ts: datetime) -> float:
    """-1 in mid-January, +1 in mid-July."""
    doy = ts.timetuple().tm_yday
    return -math.cos(2 * math.pi * (doy - 15) / 365)


def _build_legs(rng: np.random.Generator, shp: _Shipment) -> list[LegPlan]:
    s = _season(shp.ship_ts)
    lane = shp.lane
    summer = shp.ship_ts.month in SUMMER_MONTHS

    def mins(lo: float, hi: float) -> int:
        return int(round(rng.uniform(lo, hi) * 60))

    legs = [
        LegPlan(cfg.ORIGIN_DOCK, mins(1.0, 2.0), 4.5, True),
        LegPlan(
            cfg.LINEHAUL,
            mins(*lane.linehaul_hours),
            float(rng.uniform(4.0, 6.0) + 0.5 * max(s, 0.0)),
            True,
        ),
    ]
    if lane.mode == "air":
        legs.append(LegPlan(cfg.HUB_DWELL, mins(4.0, 14.0), 4.5, True))
        if summer:
            legs.append(LegPlan(cfg.TARMAC, mins(2.0, 4.0), float(rng.uniform(28, 34)), False))
        else:
            legs.append(LegPlan(cfg.TARMAC, mins(0.3, 1.0), 10.0 + 8.0 * s, False))
        legs.append(LegPlan(cfg.FLIGHT, mins(2.0, 5.0), 3.0, True))
    legs.append(LegPlan(cfg.LAST_MILE, mins(1.0, 2.5), 6.0 + 6.0 * s, False))
    legs.append(LegPlan(cfg.DESTINATION_DOCK, mins(1.0, 2.0), 8.0 + 2.0 * s, False))
    return legs


def _simulate(
    rng: np.random.Generator,
    legs: list[LegPlan],
    freeze: tuple[int, int, float] | None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (minute offsets, temperatures, leg index) on the 10-minute logger grid."""
    step = cfg.READING_INTERVAL_MIN
    starts = np.cumsum([0] + [leg.minutes for leg in legs])
    total = int(starts[-1])
    minutes = np.arange(0, total, step)
    leg_idx = np.searchsorted(starts, minutes, side="right") - 1
    ambient = np.array([legs[i].ambient_c for i in leg_idx], dtype=float)
    chilled = np.array([legs[i].chilled for i in leg_idx], dtype=bool)
    if freeze is not None:
        lo, hi, amb = freeze
        ambient[(minutes >= lo) & (minutes < hi)] = amb
    k_warm = 1 - math.exp(-(step / 60) / TAU_WARM_H)
    k_chill = 1 - math.exp(-(step / 60) / TAU_CHILL_H)
    temps = np.empty(len(minutes))
    t = START_C + float(rng.normal(0, 0.4))
    for i in range(len(minutes)):
        temps[i] = t
        a = ambient[i]
        k = k_chill if (a < t and chilled[i]) else k_warm
        t += (a - t) * k
    temps = np.round(temps + rng.normal(0, NOISE_SD_C, len(temps)), 1)
    return minutes, temps, leg_idx


def _drop_window(
    rng: np.random.Generator, leg_idx: np.ndarray, target_leg: int
) -> tuple[int, int] | None:
    """Pick 4-8 consecutive readings to remove from one leg.

    The readings kept on both sides of the window lie in the same leg, so the resulting
    logger gap (50-90 minutes) lies wholly inside it.
    """
    members = np.flatnonzero(leg_idx == target_leg)
    k = int(rng.integers(4, 9))
    lo, hi = int(members[0]) + 1, int(members[-1]) - k
    if hi < lo:
        return None
    start = int(rng.integers(lo, hi + 1))
    return start, start + k


def _pick(
    rng: np.random.Generator, pool: list[int], n: int, taken: set[int], tag: str
) -> list[int]:
    avail = [i for i in pool if i not in taken]
    if len(avail) < n:
        raise ValueError(f"not enough shipments to inject {n} of {tag}")
    chosen = [int(i) for i in rng.choice(avail, size=n, replace=False)]
    taken.update(chosen)
    return chosen


def _fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def generate(
    out_dir: Path,
    seed: int = 2025,
    shipments: int = N_SHIPMENTS,
    lots: int = N_LOTS,
) -> Manifest:
    """Write shipments.csv, milestones.csv, loggers/*.csv and manifest.json under out_dir."""
    if shipments < 300 or lots < 1:
        raise ValueError("generate needs at least 300 shipments to place the injected patterns")
    rng = np.random.default_rng(seed)
    out_dir = Path(out_dir)
    (out_dir / "loggers").mkdir(parents=True, exist_ok=True)

    # --- lots and shipments ------------------------------------------------------------------
    products = list(PRODUCT_WEIGHTS)
    lot_product = rng.choice(products, size=lots, p=list(PRODUCT_WEIGHTS.values()))
    lot_anchor = rng.uniform(0, 335, size=lots)
    base = datetime(YEAR, 1, 1)
    raw: list[tuple[datetime, int]] = []
    for i in range(shipments):
        lot = i % lots
        day = min(int(lot_anchor[lot] + rng.uniform(0, 28)), 364)
        ts = base + timedelta(
            days=day, hours=int(rng.integers(6, 20)), minutes=int(rng.integers(0, 60))
        )
        raw.append((ts, lot))
    raw.sort(key=lambda r: r[0])
    lane_p = [lane.weight for lane in cfg.LANES]
    lane_ix = rng.choice(len(cfg.LANES), size=shipments, p=np.array(lane_p) / sum(lane_p))
    ships = [
        _Shipment(
            shipment_id=f"SHP-{i + 1:04d}",
            lot_id=f"LOT-{lot + 1:03d}",
            product=str(lot_product[lot]),
            lane=cfg.LANES[int(lane_ix[i])],
            ship_ts=ts,
        )
        for i, (ts, lot) in enumerate(raw)
    ]
    for shp in ships:
        shp.legs = _build_legs(rng, shp)

    # --- disjoint injected sets --------------------------------------------------------------
    taken: set[int] = set()
    idx_all = list(range(shipments))
    winter_ground = [
        i
        for i in idx_all
        if ships[i].lane.mode == "ground" and ships[i].ship_ts.month in WINTER_MONTHS
    ]
    freezing = _pick(rng, winter_ground, N_FREEZING, taken, "freezing")
    air = [i for i in idx_all if ships[i].lane.mode == "air"]
    summer_air = [i for i in air if ships[i].ship_ts.month in SUMMER_MONTHS]
    gap_tarmac = _pick(rng, summer_air, N_GAP_RISK // 2, taken, "tarmac gaps")
    gap_hub = _pick(rng, air, N_GAP_RISK // 2, taken, "hub gaps")
    gap_other = _pick(rng, idx_all, N_GAP_OTHER, taken, "other gaps")
    flatline = _pick(rng, idx_all, N_FLATLINE, taken, "stuck loggers")
    n_spike = int(round(DOCK_SPIKE_RATE * shipments))
    spikes = _pick(rng, idx_all, n_spike, taken, "dock spikes")

    freeze_cfg: dict[int, tuple[int, int, float]] = {}
    for i in freezing:
        leg_start = ships[i].legs[0].minutes
        leg_len = ships[i].legs[1].minutes
        length = int(rng.integers(120, 181))
        offset = int(rng.integers(30, leg_len - length - 30))
        freeze_cfg[i] = (
            leg_start + offset,
            leg_start + offset + length,
            float(rng.uniform(-8, -4)),
        )

    # --- simulate, inject, write -------------------------------------------------------------
    manifest = Manifest(
        seed=seed,
        shipments=shipments,
        lots=lots,
        readings_written=0,
        readings_unique=0,
        duplicates_injected=0,
    )
    series: list[tuple[np.ndarray, np.ndarray]] = []
    leg_names = [[leg.name for leg in s.legs] for s in ships]
    for i, shp in enumerate(ships):
        minutes, temps, leg_idx = _simulate(rng, shp.legs, freeze_cfg.get(i))
        names = leg_names[i]
        keep = np.ones(len(minutes), dtype=bool)
        if i in flatline:
            temps = np.full(len(minutes), STUCK_C)
            batch = 7
        else:
            batch = int(rng.choice([1, 2, 3, 4, 5, 6, 8, 9]))
        shp.logger_id = f"LG-{batch}-{int(rng.integers(1, 9999)):04d}"
        if i in spikes:
            dock = np.flatnonzero(leg_idx == names.index(cfg.DESTINATION_DOCK))
            pos = int(dock[len(dock) // 2])
            temps[pos] = round(float(rng.uniform(8.4, 9.9)), 1)
            manifest.dock_spike_ids.append(shp.shipment_id)
        if i in gap_tarmac or i in gap_hub or i in gap_other:
            if i in gap_tarmac:
                target = names.index(cfg.TARMAC)
            elif i in gap_hub:
                target = names.index(cfg.HUB_DWELL)
            else:
                chooser = [cfg.LINEHAUL] + ([cfg.FLIGHT] if cfg.FLIGHT in names else [])
                target = names.index(str(rng.choice(chooser)))
            window = _drop_window(rng, leg_idx, target)
            if window is None:
                raise ValueError(f"{shp.shipment_id}: leg too short for a logger gap")
            keep[window[0] : window[1]] = False
            bucket = manifest.gap_other_ids if i in gap_other else manifest.gap_risk_ids
            bucket.append(shp.shipment_id)
        if i in flatline:
            manifest.flatline_ids.append(shp.shipment_id)
        if i in freezing:
            manifest.freezing_ids.append(shp.shipment_id)
        if shp.ship_ts.month in SUMMER_MONTHS and shp.lane.mode == "air":
            manifest.summer_tarmac_ids.append(shp.shipment_id)
        series.append((minutes[keep], temps[keep]))

    total_rows = sum(len(m) for m, _ in series)
    n_dup = int(round(DUPLICATE_RATE * total_rows))
    share = np.array([len(m) for m, _ in series], dtype=float)
    dup_counts = rng.multinomial(n_dup, share / share.sum())
    dup_counts = np.minimum(dup_counts, share.astype(int))
    deficit = n_dup - int(dup_counts.sum())
    while deficit > 0:  # top up shipments that hit their row cap
        j = int(rng.integers(0, shipments))
        if dup_counts[j] < len(series[j][0]):
            dup_counts[j] += 1
            deficit -= 1

    ship_rows = ["shipment_id,lot_id,product,lane_id,mode,origin,destination,ship_ts,logger_id"]
    mile_rows = ["shipment_id,milestone,ts"]
    for i, shp in enumerate(ships):
        ship_rows.append(
            ",".join(
                [
                    shp.shipment_id,
                    shp.lot_id,
                    shp.product,
                    shp.lane.lane_id,
                    shp.lane.mode,
                    cfg.ORIGIN_HUB,
                    shp.lane.destination,
                    _fmt(shp.ship_ts),
                    shp.logger_id,
                ]
            )
        )
        t = shp.ship_ts
        for leg in shp.legs:
            mile_rows.append(f"{shp.shipment_id},{leg.name},{_fmt(t)}")
            t += timedelta(minutes=leg.minutes)
        mile_rows.append(f"{shp.shipment_id},{cfg.DELIVERED},{_fmt(t)}")

        minutes, temps = series[i]
        lines = ["shipment_id,logger_id,ts,temp_c"]
        for m, v in zip(minutes, temps, strict=True):
            ts = _fmt(shp.ship_ts + timedelta(minutes=int(m)))
            lines.append(f"{shp.shipment_id},{shp.logger_id},{ts},{v:.1f}")
        extra = int(dup_counts[i])
        if extra:
            pick = np.sort(rng.choice(len(minutes), size=extra, replace=False))
            for p in pick:
                ts = _fmt(shp.ship_ts + timedelta(minutes=int(minutes[p])))
                lines.append(f"{shp.shipment_id},{shp.logger_id},{ts},{temps[p]:.1f}")
        (out_dir / "loggers" / f"{shp.shipment_id}.csv").write_text("\n".join(lines) + "\n")

    (out_dir / "shipments.csv").write_text("\n".join(ship_rows) + "\n")
    (out_dir / "milestones.csv").write_text("\n".join(mile_rows) + "\n")
    manifest.readings_unique = total_rows
    manifest.duplicates_injected = int(dup_counts.sum())
    manifest.readings_written = total_rows + manifest.duplicates_injected
    manifest.freezing_ids.sort()
    (out_dir / "manifest.json").write_text(json.dumps(asdict(manifest), indent=2) + "\n")
    return manifest
