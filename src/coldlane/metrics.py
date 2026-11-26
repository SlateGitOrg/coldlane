"""Mean kinetic temperature and time-weighted helpers (pure numpy)."""

from __future__ import annotations

import numpy as np

from coldlane import config as cfg

KELVIN = 273.15


def _weights(temps: np.ndarray, weights: np.ndarray | None) -> np.ndarray:
    if weights is None:
        return np.ones_like(temps)
    w = np.asarray(weights, dtype=float)
    if w.shape != temps.shape:
        raise ValueError("weights must have the same length as temps_c")
    if (w < 0).any():
        raise ValueError("weights must be non-negative")
    # A series whose weights are all zero (a single reading, say) falls back to equal weights.
    return w if w.sum() > 0 else np.ones_like(temps)


def time_weighted_mean(
    temps_c: np.ndarray | list[float], weights: np.ndarray | None = None
) -> float:
    """Arithmetic mean of ``temps_c`` under ``weights`` (equal weights when omitted)."""
    t = np.asarray(temps_c, dtype=float)
    if t.size == 0:
        raise ValueError("empty temperature series")
    w = _weights(t, weights)
    return float(np.sum(w * t) / np.sum(w))


def mean_kinetic_temperature(
    temps_c: np.ndarray | list[float],
    weights: np.ndarray | None = None,
    delta_h: float = cfg.DELTA_H_J_PER_MOL,
    gas_constant: float = cfg.GAS_CONSTANT,
) -> float:
    """Mean kinetic temperature in Celsius.

    MKT = (dH / R) / -ln( sum(w_i * exp(-dH / (R * T_i))) / sum(w_i) ), with T_i in Kelvin.
    The exponentials are summed in log space so a hot series cannot underflow.
    """
    t_c = np.asarray(temps_c, dtype=float)
    if t_c.size == 0:
        raise ValueError("empty temperature series")
    w = _weights(t_c, weights)
    k = delta_h / gas_constant
    exponent = -k / (t_c + KELVIN)
    top = exponent.max()
    positive = w > 0
    log_mean = top + np.log(np.sum(w[positive] * np.exp(exponent[positive] - top)) / np.sum(w))
    return float(k / -log_mean - KELVIN)
