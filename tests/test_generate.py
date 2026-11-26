import json

import pytest

from coldlane.generate import generate


def test_generator_is_deterministic_for_a_seed(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate(a, seed=9, shipments=300, lots=75)
    generate(b, seed=9, shipments=300, lots=75)
    for name in ("shipments.csv", "milestones.csv", "manifest.json", "loggers/SHP-0100.csv"):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_different_seeds_give_different_data(tmp_path):
    generate(tmp_path / "a", seed=1, shipments=300, lots=75)
    generate(tmp_path / "b", seed=2, shipments=300, lots=75)
    assert (tmp_path / "a" / "loggers" / "SHP-0100.csv").read_text() != (
        tmp_path / "b" / "loggers" / "SHP-0100.csv"
    ).read_text()


def test_injected_sets_are_disjoint_and_sized(dataset):
    m = dataset.manifest
    sets = {
        "gap_risk": set(m["gap_risk_ids"]),
        "gap_other": set(m["gap_other_ids"]),
        "flatline": set(m["flatline_ids"]),
        "freezing": set(m["freezing_ids"]),
        "spikes": set(m["dock_spike_ids"]),
    }
    assert {k: len(v) for k, v in sets.items()} == {
        "gap_risk": 12,
        "gap_other": 28,
        "flatline": 9,
        "freezing": 5,
        "spikes": 96,
    }
    names = list(sets)
    for i, x in enumerate(names):
        for y in names[i + 1 :]:
            assert not sets[x] & sets[y], (x, y)


def test_manifest_is_written_next_to_the_data(dataset):
    on_disk = json.loads((dataset.data_dir / "manifest.json").read_text())
    assert on_disk["shipments"] == 1200 and on_disk["lots"] == 300


def test_logger_files_are_ten_minute_series_with_a_header(dataset):
    lines = (dataset.data_dir / "loggers" / "SHP-0001.csv").read_text().splitlines()
    assert lines[0] == "shipment_id,logger_id,ts,temp_c"
    assert lines[1].startswith("SHP-0001,LG-")


def test_generator_refuses_a_dataset_too_small_for_the_patterns(tmp_path):
    with pytest.raises(ValueError, match="at least 300"):
        generate(tmp_path, shipments=100)
