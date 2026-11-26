import pytest

from coldlane import config as cfg
from coldlane.classify import ShipmentFacts, classify


def facts(**kw):
    base = dict(
        product="A",
        readings=100,
        min_temp_c=4.0,
        max_temp_c=6.0,
        minutes_above_8=0.0,
        minutes_warm=0.0,
        minutes_below_2=0.0,
        risk_gap=False,
        flatlined=False,
    )
    base.update(kw)
    return ShipmentFacts(**base)


def test_clean_shipment_is_compliant():
    assert classify(facts()).shipment_class == cfg.COMPLIANT


def test_any_excursion_minute_is_an_excursion_within_budget():
    assert classify(facts(minutes_above_8=10.0, minutes_warm=10.0)).shipment_class == cfg.EXCURSION
    assert classify(facts(minutes_below_2=10.0)).shipment_class == cfg.EXCURSION


@pytest.mark.parametrize("low", [0.0, -3.5])
def test_a_reading_at_or_below_zero_rejects(low):
    v = classify(facts(min_temp_c=low))
    assert v.shipment_class == cfg.REJECT and v.reject_reason == "freeze"


def test_a_reading_above_25_rejects_and_exactly_25_does_not():
    assert classify(facts(max_temp_c=25.1)).reject_reason == "overheat"
    assert classify(facts(max_temp_c=25.0)).shipment_class == cfg.COMPLIANT


def test_cumulative_warm_time_over_the_allowance_rejects():
    allowance = cfg.ALLOWANCE_MIN["C"]
    at_limit = classify(facts(product="C", minutes_above_8=allowance, minutes_warm=allowance))
    over = classify(facts(product="C", minutes_above_8=allowance + 10, minutes_warm=allowance + 10))
    assert at_limit.shipment_class == cfg.EXCURSION
    assert over.shipment_class == cfg.REJECT and over.reject_reason == "allowance"


def test_the_same_warm_time_is_acceptable_for_a_product_with_a_larger_allowance():
    minutes = cfg.ALLOWANCE_MIN["C"] + 100
    assert (
        classify(facts(product="A", minutes_above_8=minutes, minutes_warm=minutes)).shipment_class
        == cfg.EXCURSION
    )
    assert (
        classify(facts(product="C", minutes_above_8=minutes, minutes_warm=minutes)).shipment_class
        == cfg.REJECT
    )


def test_allowances_are_72_24_and_6_hours():
    assert cfg.ALLOWANCE_MIN == {"A": 4320, "B": 1440, "C": 360}


def test_risk_gap_makes_a_clean_shipment_indeterminate_not_compliant():
    v = classify(facts(risk_gap=True))
    assert v.shipment_class == cfg.INDETERMINATE and v.indeterminate_reason == "gap_over_risk_leg"


def test_flatlined_logger_is_indeterminate():
    v = classify(facts(flatlined=True))
    assert v.shipment_class == cfg.INDETERMINATE and v.indeterminate_reason == "flatline"


def test_indeterminate_wins_over_an_excursion():
    v = classify(facts(risk_gap=True, flatlined=True, minutes_above_8=40.0, minutes_warm=40.0))
    assert v.shipment_class == cfg.INDETERMINATE
    assert v.indeterminate_reason == "gap_over_risk_leg+flatline"


def test_reject_wins_over_indeterminate():
    v = classify(facts(min_temp_c=-1.0, risk_gap=True, flatlined=True))
    assert v.shipment_class == cfg.REJECT and v.indeterminate_reason == ""


def test_shipment_without_readings_is_indeterminate():
    v = classify(facts(readings=0, min_temp_c=None, max_temp_c=None))
    assert v.shipment_class == cfg.INDETERMINATE and v.indeterminate_reason == "no_data"
