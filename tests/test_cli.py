import json
import shutil

import pytest
from typer.testing import CliRunner

from coldlane.cli import app

runner = CliRunner()


@pytest.fixture
def sample_db(tmp_path, repo):
    db = tmp_path / "s.duckdb"
    result = runner.invoke(app, ["ingest", "--data", str(repo / "sample"), "--db", str(db)])
    assert result.exit_code == 0, result.output
    return db


def test_help_lists_every_command():
    out = runner.invoke(app, ["--help"]).output
    for name in ("generate", "ingest", "analyse", "export", "validate-twb", "report"):
        assert name in out


def test_ingest_prints_stats_as_json(tmp_path, repo):
    result = runner.invoke(
        app, ["ingest", "--data", str(repo / "sample"), "--db", str(tmp_path / "s.duckdb")]
    )
    stats = json.loads(result.output)
    assert stats["shipments"] == 3
    assert stats["duplicates_removed"] == 6
    assert stats["risk_gaps"] == 1


def test_analyse_prints_the_class_counts(sample_db):
    result = runner.invoke(app, ["analyse", "--db", str(sample_db)])
    assert result.exit_code == 0
    lines = {ln.split()[0]: int(ln.split()[1]) for ln in result.output.strip().splitlines()}
    assert lines == {
        "reject": 0,
        "indeterminate": 1,
        "excursion_within_budget": 1,
        "compliant": 1,
    }


def test_export_writes_three_csvs(sample_db, tmp_path):
    runner.invoke(app, ["analyse", "--db", str(sample_db)])
    out = tmp_path / "extract"
    result = runner.invoke(app, ["export", "--db", str(sample_db), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == [
        "leg_excursions.csv",
        "lot_budget.csv",
        "shipment_summary.csv",
    ]
    assert len((out / "shipment_summary.csv").read_text().splitlines()) == 4


def test_report_prints_the_three_tables(sample_db):
    runner.invoke(app, ["analyse", "--db", str(sample_db)])
    out = runner.invoke(app, ["report", "--db", str(sample_db)]).output
    assert "### Classification counts" in out
    assert "### Top 5 lane x leg cells by excursion minutes" in out
    assert "### 5 lots closest to their stability budget" in out
    assert "| AIR-SEA | tarmac |" in out


def test_commands_fail_cleanly_without_a_database(tmp_path):
    result = runner.invoke(app, ["analyse", "--db", str(tmp_path / "missing.duckdb")])
    assert result.exit_code == 2
    assert "coldlane ingest" in result.output


def test_validate_twb_passes_on_the_committed_workbook(repo):
    result = runner.invoke(app, ["validate-twb", str(repo / "tableau" / "coldlane.twb")])
    assert result.exit_code == 0
    assert result.output.strip().endswith(": ok")


def test_validate_twb_exits_1_and_names_the_field_when_a_column_is_renamed(tmp_path, repo):
    shutil.copytree(repo / "tableau", tmp_path / "tableau")
    csv_path = tmp_path / "tableau" / "data" / "lot_budget.csv"
    csv_path.write_text(csv_path.read_text().replace("minutes_used", "used", 1))
    result = runner.invoke(app, ["validate-twb", str(tmp_path / "tableau" / "coldlane.twb")])
    assert result.exit_code == 1
    assert "Budget Used %" in result.output


def test_generate_writes_the_dataset(tmp_path):
    result = runner.invoke(app, ["generate", "--out", str(tmp_path / "d"), "--seed", "5"])
    assert result.exit_code == 0, result.output
    assert "1200 shipments in 300 lots" in result.output
    assert len(list((tmp_path / "d" / "loggers").glob("*.csv"))) == 1200


def test_report_before_analyse_points_at_analyse(sample_db):
    result = runner.invoke(app, ["report", "--db", str(sample_db)])
    assert result.exit_code == 2
    assert "run `coldlane analyse` first" in result.output
    assert "Exception" not in result.output


def test_analyse_on_a_database_without_ingested_tables_points_at_ingest(tmp_path):
    import duckdb

    empty = tmp_path / "empty.duckdb"
    duckdb.connect(str(empty)).close()
    result = runner.invoke(app, ["analyse", "--db", str(empty)])
    assert result.exit_code == 2
    assert "run `coldlane ingest` first" in result.output
