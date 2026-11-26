"""Static validation of the Tableau workbook (``.twb`` is plain XML).

Tableau cannot run in CI, so the workbook is checked against the things it depends on:

* every datasource is a textscan connection with a relative directory, and its CSV exists
* every field a calculation or worksheet references is a column of that CSV or a calculated field
* the calculated-field captions (and formulas) equal the metric registry
* the workbook has the required worksheets and a dashboard that only uses worksheets
* no attribute holds an absolute path
"""

from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from coldlane.registry import REGISTRY

CUMULATIVE = "Cumulative Excursion %"
REQUIRED_WORKSHEETS = ("Lane x Leg Heatmap", "Hub Pareto", "Lot Budget Burn", "Monthly Trend")
_FIELD_REF = re.compile(r"\[([^\]]+)\]")
_SHELF_REF = re.compile(r"\[([^\]]+)\]\.\[([^\]]+)\]")
_ABSOLUTE = re.compile(r"^(?:/|[A-Za-z]:[\\/]|\\\\|~)")
_HOME_DIR = re.compile(r"(?:^|[\\/])(?:Users|home)[\\/]")


@dataclass
class _Source:
    name: str
    caption: str
    csv_name: str
    header: list[str]
    calcs: dict[str, str] = field(default_factory=dict)  # internal name -> caption

    @property
    def known(self) -> set[str]:
        return set(self.header) | set(self.calcs) | set(self.calcs.values())


def _strip(name: str) -> str:
    return name[1:-1] if name.startswith("[") and name.endswith("]") else name


def validate_twb(path: Path) -> list[str]:
    """Return a list of problems; an empty list means the workbook is valid."""
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        return [f"cannot parse {path}: {exc}"]

    errors: list[str] = []
    _check_absolute_paths(root, errors)
    sources = _check_datasources(root, path.parent, errors)
    _check_registry(root, errors)
    _check_worksheets(root, sources, errors)
    _check_pareto(root, errors)
    _check_dashboards(root, errors)
    return errors


def _check_absolute_paths(root: ET.Element, errors: list[str]) -> None:
    for el in root.iter():
        for attr, value in el.attrib.items():
            if attr == "formula":
                continue
            if _ABSOLUTE.match(value) or _HOME_DIR.search(value):
                errors.append(f"absolute path in <{el.tag} {attr}='{value}'>; use a relative path")


def _check_datasources(root: ET.Element, base: Path, errors: list[str]) -> dict[str, _Source]:
    sources: dict[str, _Source] = {}
    for ds in root.findall("./datasources/datasource"):
        name = ds.get("name", "")
        if name == "Parameters":
            continue
        caption = ds.get("caption", name)
        conn = next((c for c in ds.iter("connection") if c.get("class") == "textscan"), None)
        if conn is None:
            errors.append(f"datasource '{caption}' has no textscan connection")
            continue
        directory, filename = conn.get("directory", ""), conn.get("filename", "")
        csv_path = base / directory / filename
        if not csv_path.is_file():
            errors.append(f"datasource '{caption}': {csv_path} does not exist")
            continue
        with csv_path.open(newline="") as fh:
            header = next(csv.reader(fh), [])
        src = _Source(name=name, caption=caption, csv_name=filename, header=header)
        for col in ds.findall("column"):
            calc = col.find("calculation")
            if calc is not None:
                internal = _strip(col.get("name", ""))
                src.calcs[internal] = col.get("caption", internal)
        sources[name] = src

        _check_relation(ds, src, errors)
        _check_calcs(ds, src, errors)
    if not sources:
        errors.append("workbook has no usable datasource")
    return sources


def _check_relation(ds: ET.Element, src: _Source, errors: list[str]) -> None:
    for rel in ds.iter("relation"):
        names = [c.get("name", "") for c in rel.iter("column")]
        for col in names:
            if col not in src.header:
                errors.append(
                    f"datasource '{src.caption}' declares column '{col}' "
                    f"missing from the header of {src.csv_name}"
                )
        for col in src.header:
            if names and col not in names:
                errors.append(
                    f"{src.csv_name} has column '{col}' that datasource '{src.caption}' does not"
                )
    for rec in ds.iter("metadata-record"):
        remote = rec.findtext("remote-name")
        if remote is not None and remote not in src.header:
            errors.append(
                f"datasource '{src.caption}' metadata names column '{remote}' "
                f"missing from the header of {src.csv_name}"
            )


def _check_calcs(ds: ET.Element, src: _Source, errors: list[str]) -> None:
    for col in ds.findall("column"):
        calc = col.find("calculation")
        if calc is None:
            continue
        caption = col.get("caption", col.get("name", ""))
        for ref in _FIELD_REF.findall(calc.get("formula", "")):
            if ref not in src.known:
                errors.append(
                    f"calculated field '{caption}' (datasource '{src.caption}') references "
                    f"[{ref}], which is not a column of {src.csv_name} or a calculated field"
                )


def _check_registry(root: ET.Element, errors: list[str]) -> None:
    found: list[tuple[str, str, str]] = []  # caption, formula, datasource caption
    for ds in root.findall("./datasources/datasource"):
        for col in ds.findall("column"):
            calc = col.find("calculation")
            if calc is not None:
                found.append(
                    (col.get("caption", ""), calc.get("formula", ""), ds.get("caption", ""))
                )
    counts = Counter(c for c, _, _ in found)
    expected = {m.name: m for m in REGISTRY}
    for name in expected:
        if counts[name] == 0:
            errors.append(f"registry metric '{name}' has no calculated field in the workbook")
        elif counts[name] > 1:
            errors.append(f"registry metric '{name}' is defined {counts[name]} times")
    for caption, formula, ds_caption in found:
        metric = expected.get(caption)
        if metric is None:
            errors.append(f"calculated field '{caption}' is not in the metric registry")
            continue
        if formula != metric.formula:
            errors.append(
                f"calculated field '{caption}' has formula {formula!r}; "
                f"the registry says {metric.formula!r}"
            )
        if ds_caption != metric.datasource:
            errors.append(
                f"calculated field '{caption}' lives in datasource '{ds_caption}'; "
                f"the registry says '{metric.datasource}'"
            )


def _check_worksheets(root: ET.Element, sources: dict[str, _Source], errors: list[str]) -> None:
    sheets = {ws.get("name", ""): ws for ws in root.findall("./worksheets/worksheet")}
    for required in REQUIRED_WORKSHEETS:
        if required not in sheets:
            errors.append(f"missing worksheet '{required}'")
    for name, ws in sheets.items():
        instances: dict[str, set[str]] = {}
        for dep in ws.iter("datasource-dependencies"):
            ds_name = dep.get("datasource", "")
            src = sources.get(ds_name)
            if src is None:
                errors.append(f"worksheet '{name}' uses unknown datasource '{ds_name}'")
                continue
            for col in dep.findall("column"):
                ref = _strip(col.get("name", ""))
                if ref not in src.known:
                    errors.append(
                        f"worksheet '{name}' references field [{ref}], "
                        f"which is not in {src.csv_name} or a calculated field"
                    )
            known_inst = instances.setdefault(ds_name, set())
            for inst in dep.findall("column-instance"):
                known_inst.add(_strip(inst.get("name", "")))
                ref = _strip(inst.get("column", ""))
                if ref not in src.known:
                    errors.append(
                        f"worksheet '{name}' has a column instance on [{ref}], "
                        f"which is not in {src.csv_name} or a calculated field"
                    )
        for el in ws.iter():
            texts = [el.text or ""] + [v for k, v in el.attrib.items() if k in ("column", "using")]
            for text in texts:
                for ds_name, inst in _SHELF_REF.findall(text):
                    if ds_name not in instances or inst not in instances[ds_name]:
                        errors.append(
                            f"worksheet '{name}' shelf reference [{ds_name}].[{inst}] "
                            "has no matching column instance"
                        )


def _check_pareto(root: ET.Element, errors: list[str]) -> None:
    """The Hub Pareto needs its cumulative line: a table calculation on a second axis."""
    sheet = next(
        (w for w in root.findall("./worksheets/worksheet") if w.get("name") == "Hub Pareto"), None
    )
    if sheet is None:
        return  # reported as a missing worksheet
    internal = {
        _strip(c.get("name", ""))
        for c in root.iter("column")
        if c.get("caption") == CUMULATIVE and c.find("calculation") is not None
    }
    instances = [
        i
        for i in sheet.iter("column-instance")
        if _strip(i.get("column", "")) in internal and i.find("table-calc") is not None
    ]
    if not instances:
        errors.append(
            f"worksheet 'Hub Pareto' has no table calculation on '{CUMULATIVE}' "
            "(the cumulative-percent line)"
        )
        return
    if len(sheet.findall("./table/panes/pane")) < 2:
        errors.append(f"worksheet 'Hub Pareto' needs a second pane for '{CUMULATIVE}'")
    shelf = sheet.findtext("./table/rows") or ""
    if instances[0].get("name", "") not in shelf:
        errors.append(f"worksheet 'Hub Pareto' does not place '{CUMULATIVE}' on the rows shelf")


def _check_dashboards(root: ET.Element, errors: list[str]) -> None:
    sheets = {ws.get("name", "") for ws in root.findall("./worksheets/worksheet")}
    dashboards = root.findall("./dashboards/dashboard")
    if not dashboards:
        errors.append("workbook has no dashboard")
    for dash in dashboards:
        for zone in dash.iter("zone"):
            zname = zone.get("name")
            if zname and zname not in sheets:
                errors.append(
                    f"dashboard '{dash.get('name')}' places '{zname}', which is not a worksheet"
                )
