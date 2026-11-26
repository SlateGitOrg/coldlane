from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest

from coldlane.analyse import analyse
from coldlane.generate import generate
from coldlane.ingest import IngestStats, load

REPO = Path(__file__).resolve().parent.parent


@dataclass
class Dataset:
    data_dir: Path
    manifest: dict
    stats: IngestStats
    con: duckdb.DuckDBPyConnection


@pytest.fixture(scope="session")
def dataset(tmp_path_factory: pytest.TempPathFactory):
    """The full seeded dataset, generated, ingested and analysed once per test session."""
    data_dir = tmp_path_factory.mktemp("coldlane-data")
    generate(data_dir)
    manifest = json.loads((data_dir / "manifest.json").read_text())
    con = duckdb.connect()
    stats = load(con, data_dir)
    analyse(con)
    yield Dataset(data_dir, manifest, stats, con)
    con.close()


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO
