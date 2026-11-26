"""The ``coldlane`` command line."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import duckdb
import typer

from coldlane import analyse as analysis
from coldlane import export as exporter
from coldlane import generate as generator
from coldlane import ingest as ingestion
from coldlane import report as reporting
from coldlane import twb as workbook

app = typer.Typer(
    help="Cold-chain logger analysis: MKT, leg-attributed excursion minutes, gaps as unknown.",
    no_args_is_help=True,
    add_completion=False,
)

DataDir = Annotated[Path, typer.Option("--data", help="Directory holding the logger data.")]
DbPath = Annotated[Path, typer.Option("--db", help="DuckDB database file.")]


def _open_existing(db: Path, table: str = "shipment") -> duckdb.DuckDBPyConnection:
    """Open ``db`` and require ``table`` in it, with a pointer to the command that creates it."""
    step = "ingest" if table == "shipment" else "analyse"
    if not db.exists():
        typer.echo(f"{db} not found; run `coldlane {step}` first", err=True)
        raise typer.Exit(2)
    con = duckdb.connect(str(db))
    found = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()
    if not found or not found[0]:
        con.close()
        typer.echo(f"{db} has no `{table}` table; run `coldlane {step}` first", err=True)
        raise typer.Exit(2)
    return con


@app.command()
def generate(
    out: Annotated[Path, typer.Option("--out", help="Directory to write.")] = Path("data"),
    seed: Annotated[int, typer.Option(help="Random seed.")] = 2025,
) -> None:
    """Write the synthetic shipments, milestones and logger CSVs."""
    manifest = generator.generate(out, seed=seed)
    typer.echo(
        f"wrote {manifest.shipments} shipments in {manifest.lots} lots, "
        f"{manifest.readings_written} logger rows ({manifest.duplicates_injected} duplicated) "
        f"to {out}/"
    )


@app.command()
def ingest(data: DataDir = Path("data"), db: DbPath = Path("coldlane.duckdb")) -> None:
    """Load logger CSVs: de-duplicate, assign legs, mark gaps and flatlined sensors."""
    stats = ingestion.ingest(data, db)
    typer.echo(json.dumps(stats.__dict__, indent=2))


@app.command()
def analyse(db: DbPath = Path("coldlane.duckdb")) -> None:
    """Compute shipment metrics, leg excursions, classes and lot budgets."""
    con = _open_existing(db)
    try:
        analysis.analyse(con)
        for name, n in analysis.class_counts(con):
            typer.echo(f"{name:<26}{n:>6}")
    finally:
        con.close()


@app.command()
def export(
    db: DbPath = Path("coldlane.duckdb"),
    out: Annotated[Path, typer.Option("--out", help="Directory for the CSV extracts.")] = Path(
        "tableau/data"
    ),
) -> None:
    """Write shipment_summary.csv, leg_excursions.csv and lot_budget.csv."""
    con = _open_existing(db, "shipment_summary")
    try:
        for table, n in exporter.export(con, out).items():
            typer.echo(f"{out / (table + '.csv')}: {n} rows")
    finally:
        con.close()


@app.command("validate-twb")
def validate_twb(
    path: Annotated[Path, typer.Argument(help="Workbook to validate.")] = Path(
        "tableau/coldlane.twb"
    ),
) -> None:
    """Statically validate the Tableau workbook against the CSV extracts and the registry."""
    errors = workbook.validate_twb(path)
    if errors:
        for err in errors:
            typer.echo(f"error: {err}", err=True)
        raise typer.Exit(1)
    typer.echo(f"{path}: ok")


@app.command()
def report(db: DbPath = Path("coldlane.duckdb")) -> None:
    """Print classification counts, the hottest lane x leg cells and the lots nearest budget."""
    con = _open_existing(db, "shipment_summary")
    try:
        typer.echo(reporting.render_report(con))
    finally:
        con.close()
