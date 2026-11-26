import csv
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from coldlane.export import COLUMNS
from coldlane.registry import NAMES, REGISTRY
from coldlane.twb import REQUIRED_WORKSHEETS, validate_twb


@pytest.fixture
def workbook(tmp_path, repo) -> Path:
    """A private copy of the committed workbook and its extracts."""
    dest = tmp_path / "tableau"
    shutil.copytree(repo / "tableau", dest)
    return dest / "coldlane.twb"


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_committed_workbook_is_valid(repo):
    assert validate_twb(repo / "tableau" / "coldlane.twb") == []


def test_committed_extract_headers_match_the_export_columns(repo):
    for table, columns in COLUMNS.items():
        with (repo / "tableau" / "data" / f"{table}.csv").open() as fh:
            assert tuple(next(csv.reader(fh))) == columns


def test_workbook_has_one_textscan_datasource_per_csv_with_a_relative_directory(repo):
    root = ET.parse(repo / "tableau" / "coldlane.twb").getroot()
    conns = [c for c in root.iter("connection") if c.get("class") == "textscan"]
    assert sorted(c.get("filename") for c in conns) == sorted(f"{t}.csv" for t in COLUMNS)
    assert {c.get("directory") for c in conns} == {"data"}


def test_workbook_has_the_four_worksheets_and_one_dashboard(repo):
    root = ET.parse(repo / "tableau" / "coldlane.twb").getroot()
    assert [w.get("name") for w in root.findall("./worksheets/worksheet")] == list(
        REQUIRED_WORKSHEETS
    )
    assert len(root.findall("./dashboards/dashboard")) == 1


def test_calculated_field_captions_equal_the_registry(repo):
    root = ET.parse(repo / "tableau" / "coldlane.twb").getroot()
    captions = sorted(
        c.get("caption")
        for ds in root.findall("./datasources/datasource")
        for c in ds.findall("column")
        if c.find("calculation") is not None
    )
    assert captions == sorted(NAMES)
    assert set(NAMES) == {
        "Excursion Minutes",
        "Excursion Rate",
        "Indeterminate Rate",
        "Reject Rate",
        "Budget Used %",
        "Avg MKT",
        "Cumulative Excursion %",
    }


def test_registry_formulas_only_use_columns_of_their_own_extract():
    import re

    for metric in REGISTRY:
        refs = set(re.findall(r"\[([^\]]+)\]", metric.formula))
        assert refs and refs <= set(COLUMNS[metric.datasource]), metric.name


def test_renaming_a_csv_column_fails_and_names_the_calculated_field(workbook):
    path = workbook.parent / "data" / "leg_excursions.csv"
    text = path.read_text()
    path.write_text(text.replace("excursion_minutes", "excursion_mins", 1))
    errors = validate_twb(workbook)
    assert errors
    assert any("'Excursion Minutes'" in e and "[excursion_minutes]" in e for e in errors)


def test_renaming_a_column_used_by_a_rate_names_that_field(workbook):
    path = workbook.parent / "data" / "shipment_summary.csv"
    path.write_text(path.read_text().replace("is_reject", "rejected", 1))
    errors = validate_twb(workbook)
    assert any("'Reject Rate'" in e and "[is_reject]" in e for e in errors)
    assert not any("'Excursion Rate'" in e for e in errors)


def test_renaming_a_dimension_column_breaks_the_worksheets_that_use_it(workbook):
    path = workbook.parent / "data" / "lot_budget.csv"
    path.write_text(path.read_text().replace("lot_id", "lot", 1))
    errors = validate_twb(workbook)
    assert any("Lot Budget Burn" in e and "[lot_id]" in e for e in errors)


def test_a_caption_that_differs_from_the_registry_fails(workbook):
    edit(workbook, "caption='Reject Rate'", "caption='Rejection Rate'")
    errors = validate_twb(workbook)
    assert any("'Rejection Rate' is not in the metric registry" in e for e in errors)
    assert any("'Reject Rate' has no calculated field" in e for e in errors)


def test_a_formula_that_differs_from_the_registry_fails(workbook):
    edit(workbook, "formula='AVG([mkt_c])'", "formula='MAX([mkt_c])'")
    assert any("'Avg MKT'" in e and "registry" in e for e in validate_twb(workbook))


def test_a_missing_metric_fails(workbook):
    text = workbook.read_text()
    start = text.index("      <column caption='Budget Used %'")
    end = text.index("</column>", start) + len("</column>\n")
    workbook.write_text(text[:start] + text[end:])
    assert any("'Budget Used %' has no calculated field" in e for e in validate_twb(workbook))


def test_an_absolute_directory_fails(workbook):
    edit(workbook, "directory='data'", "directory='/srv/extracts'")
    errors = validate_twb(workbook)
    assert any("absolute path" in e for e in errors)


def test_a_windows_absolute_path_fails(workbook):
    edit(workbook, "<preferences>", "<preferences><preference name='dir' value='C:\\data' />")
    assert any("absolute path" in e for e in validate_twb(workbook))


def test_a_calculation_on_an_unknown_field_fails(workbook):
    edit(workbook, "formula='SUM([excursion_minutes])'", "formula='SUM([excursion_secs])'")
    errors = validate_twb(workbook)
    assert any("'Excursion Minutes'" in e and "[excursion_secs]" in e for e in errors)


def test_a_worksheet_field_missing_from_the_extract_fails(workbook):
    edit(
        workbook,
        "<column datatype='string' name='[site]'",
        "<column datatype='string' name='[sitex]'",
    )
    assert any("'Hub Pareto'" in e and "[sitex]" in e for e in validate_twb(workbook))


def test_a_shelf_reference_without_a_column_instance_fails(workbook):
    root_text = workbook.read_text()
    marker = "<rows>[federated.0coldlane0legexcursions].[none:lane_id:nk]</rows>"
    assert marker in root_text
    workbook.write_text(root_text.replace(marker, marker.replace("lane_id", "leg_id")))
    assert any("no matching column instance" in e for e in validate_twb(workbook))


def test_a_dashboard_zone_must_be_a_worksheet(workbook):
    edit(workbook, "name='Hub Pareto' w='40000'", "name='Hub Pareto 2' w='40000'")
    assert any("'Hub Pareto 2', which is not a worksheet" in e for e in validate_twb(workbook))


def test_a_missing_worksheet_fails(workbook):
    text = workbook.read_text()
    start = text.index("    <worksheet name='Monthly Trend'>")
    end = text.index("</worksheet>", start) + len("</worksheet>\n")
    workbook.write_text(text[:start] + text[end:])
    assert any("missing worksheet 'Monthly Trend'" in e for e in validate_twb(workbook))


def test_a_missing_csv_fails(workbook):
    (workbook.parent / "data" / "lot_budget.csv").unlink()
    assert any("lot_budget.csv" in e and "does not exist" in e for e in validate_twb(workbook))


def test_malformed_xml_is_reported(tmp_path):
    bad = tmp_path / "bad.twb"
    bad.write_text("<workbook><datasources></workbook>")
    assert validate_twb(bad)[0].startswith("cannot parse")


def test_a_home_directory_path_fails(workbook):
    edit(workbook, "<preferences>", "<preferences><preference name='dir' value='x/home/me/data' />")
    assert any("absolute path" in e for e in validate_twb(workbook))


def test_hub_pareto_has_a_cumulative_line_on_a_second_axis(repo):
    root = ET.parse(repo / "tableau" / "coldlane.twb").getroot()
    sheet = next(w for w in root.findall("./worksheets/worksheet") if w.get("name") == "Hub Pareto")
    assert [m.get("class") for m in sheet.findall("./table/panes/pane/mark")] == ["Bar", "Line"]
    calcs = [i for i in sheet.iter("column-instance") if i.find("table-calc") is not None]
    assert len(calcs) == 1
    assert calcs[0].get("name") in sheet.findtext("./table/rows")


def test_a_pareto_without_its_table_calculation_fails(workbook):
    edit(workbook, "<table-calc ordering-type='Columns' />", "")
    assert any(
        "no table calculation on 'Cumulative Excursion %'" in e for e in validate_twb(workbook)
    )


def test_a_pareto_without_a_second_pane_fails(workbook):
    text = workbook.read_text()
    start = text.index("          <pane y-axis-name=")
    end = text.index("</pane>", start) + len("</pane>\n")
    workbook.write_text(text[:start] + text[end:])
    assert any("needs a second pane" in e for e in validate_twb(workbook))


def test_a_pareto_that_drops_the_line_from_the_rows_shelf_fails(workbook):
    text = workbook.read_text()
    start = text.index(
        "<rows>([federated.0coldlane0legexcursions].[usr:Calculation_2025110100000001"
    )
    end = text.index("</rows>", start)
    row = text[start:end]
    workbook.write_text(text.replace(row, row.split(" + ")[0] + ")"))
    assert any(
        "does not place 'Cumulative Excursion %' on the rows shelf" in e
        for e in validate_twb(workbook)
    )
