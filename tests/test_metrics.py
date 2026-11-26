import math

import numpy as np
import pytest

from coldlane.metrics import mean_kinetic_temperature, time_weighted_mean

DH_OVER_R = 83_144.0 / 8.3144621


def reference_mkt(temps_c, weights=None):
    """Straight transcription of the Arrhenius formula, without the log-space trick."""
    w = weights if weights is not None else [1.0] * len(temps_c)
    total = sum(w)
    mean_rate = (
        sum(wi * math.exp(-DH_OVER_R / (t + 273.15)) for wi, t in zip(w, temps_c, strict=True))
        / total
    )
    return DH_OVER_R / -math.log(mean_rate) - 273.15


def test_constant_series_gives_the_constant():
    assert mean_kinetic_temperature([5.0] * 20) == pytest.approx(5.00, abs=1e-9)


def test_half_at_5_and_half_at_25_is_19_70():
    got = mean_kinetic_temperature([5.0] * 50 + [25.0] * 50)
    assert got == pytest.approx(19.70, abs=0.05)
    assert got > 15.0  # well above the arithmetic mean of 15 C


def test_matches_hand_computed_two_point_value():
    # 0 C and 10 C: exp(-k/273.15) and exp(-k/283.15), k = dH / R
    expected = reference_mkt([0.0, 10.0])
    assert expected == pytest.approx(6.4327, abs=1e-3)
    assert mean_kinetic_temperature([0.0, 10.0]) == pytest.approx(expected, abs=1e-9)


def test_activation_energy_changes_the_result():
    low = mean_kinetic_temperature([2.0, 8.0, 25.0], delta_h=40_000.0)
    high = mean_kinetic_temperature([2.0, 8.0, 25.0])
    assert low < high  # a higher activation energy weights the warm readings more


def test_never_below_the_arithmetic_mean_on_random_series():
    rng = np.random.default_rng(11)
    for _ in range(200):
        n = int(rng.integers(2, 300))
        temps = rng.uniform(-5, 40, n)
        weights = rng.uniform(0.1, 10, n)
        assert mean_kinetic_temperature(temps) >= time_weighted_mean(temps) - 1e-9
        assert mean_kinetic_temperature(temps, weights) >= time_weighted_mean(temps, weights) - 1e-9


def test_weights_are_equivalent_to_repeated_readings():
    repeated = mean_kinetic_temperature([5.0, 25.0, 25.0])
    weighted = mean_kinetic_temperature([5.0, 25.0], weights=np.array([1.0, 2.0]))
    assert weighted == pytest.approx(repeated, abs=1e-9)


def test_zero_weight_readings_are_ignored():
    got = mean_kinetic_temperature([5.0, 5.0, 40.0], weights=np.array([10.0, 10.0, 0.0]))
    assert got == pytest.approx(5.0, abs=1e-9)


def test_all_zero_weights_fall_back_to_equal_weights():
    got = mean_kinetic_temperature([5.0, 25.0], weights=np.array([0.0, 0.0]))
    assert got == pytest.approx(mean_kinetic_temperature([5.0, 25.0]), abs=1e-9)


def test_hot_series_does_not_underflow():
    assert mean_kinetic_temperature([150.0, 150.0]) == pytest.approx(150.0, abs=1e-6)


def test_matches_reference_implementation_on_random_input():
    rng = np.random.default_rng(3)
    temps = rng.uniform(0, 30, 500)
    weights = rng.integers(0, 11, 500).astype(float)
    assert mean_kinetic_temperature(temps, weights) == pytest.approx(
        reference_mkt(temps, list(weights)), abs=1e-9
    )


def test_rejects_bad_input():
    with pytest.raises(ValueError):
        mean_kinetic_temperature([])
    with pytest.raises(ValueError):
        mean_kinetic_temperature([5.0, 6.0], weights=np.array([1.0]))
    with pytest.raises(ValueError):
        mean_kinetic_temperature([5.0, 6.0], weights=np.array([1.0, -1.0]))


def test_time_weighted_mean():
    assert time_weighted_mean([4.0, 10.0], np.array([3.0, 1.0])) == pytest.approx(5.5)
