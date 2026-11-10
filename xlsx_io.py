"""Read cached XLSX values and patch only numeric AY cells in an Ozon template.

Works directly with the XLSX ZIP/XML so the vendor's styles, instructions,
validations, sheet order, and all other workbook parts are retained unchanged.
No spreadsheet formulas, macros, or external links are executed.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN}


@dataclass(frozen=True)
class Cell:
    value: str | None
    formula: str | None = None
    kind: str | None = None


@dataclass
class Sheet:
    name: str
    part: str
    rows: dict[int, dict[str, Cell]]


def read_xlsx(path: str | Path) -> dict[str, Sheet]:
    sheets = {}
    with ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = [
                "".join(node.text or "" for node in item.iter(f"{{{MAIN}}}t"))
                for item in ET.fromstring(archive.read("xl/sharedStrings.xml"))
            ]
        relationships = {
            item.get("Id"): item.get("Target")
            for item in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        }
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        for item in workbook.find("m:sheets", NS):
            target = relationships[item.get(f"{{{REL}}}id")]
            part = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            root = ET.fromstring(archive.read(part))
            rows = {}
            for row in root.findall("m:sheetData/m:row", NS):
                cells = {}
                for cell in row.findall("m:c", NS):
                    reference = cell.get("r", "")
                    match = re.fullmatch(r"([A-Z]+)[1-9][0-9]*", reference)
                    if not match:
                        raise ValueError("Unsupported cell reference in XLSX")
                    column = match.group(1)
                    kind = cell.get("t")
                    value = cell.find("m:v", NS)
                    text = value.text if value is not None else None
                    if kind == "s" and text is not None:
                        text = strings[int(text)]
                    elif kind == "inlineStr":
                        text = "".join(n.text or "" for n in cell.findall(".//m:t", NS))
                    formula = cell.find("m:f", NS)
                    cells[column] = Cell(text, formula.text if formula is not None else None, kind)
                rows[int(row.get("r"))] = cells
            sheets[item.get("name")] = Sheet(item.get("name"), part, rows)
    return sheets


AY_CELL = re.compile(
    r'<(?P<prefix>(?:[A-Za-z_][\w.-]*:)?)c\b'
    r'(?P<attrs>[^>]*?\br=(?P<quote>["\'])(?P<ref>AY[1-9][0-9]*)(?P=quote)[^>]*?)'
    r'(?:\s*/>|>.*?</(?P=prefix)c\s*>)',
    re.DOTALL,
)


def write_ay_copy(source: str | Path, destination: str | Path, updates: dict[int, Decimal]) -> None:
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError("Destination must be a new file; the input is never overwritten")
    sheets = read_xlsx(source)
    sheet = sheets.get("Товары и цены")
    if sheet is None:
        raise ValueError("Ozon sheet 'Товары и цены' was not found")
    for row, value in updates.items():
        if row < 5 or not value.is_finite() or value <= 0:
            raise ValueError("AY updates must be positive numeric prices on product rows")
        cell = sheet.rows.get(row, {}).get("AY")
        if cell is None or cell.value not in (None, "") or cell.formula:
            raise ValueError(f"AY{row} is missing or already contains a value")
    with ZipFile(source) as archive:
        xml = archive.read(sheet.part).decode("utf-8")
        replaced = set()

        def replace(match):
            reference = match.group("ref")
            row = int(reference[2:])
            if row not in updates:
                return match.group(0)
            if row in replaced:
                raise ValueError("Duplicate AY cell in template XML")
            replaced.add(row)
            attributes = re.sub(r'\s+t=(["\']).*?\1', "", match.group("attrs")).rstrip()
            prefix = match.group("prefix")
            return f'<{prefix}c{attributes}><{prefix}v>{format(updates[row], "f")}</{prefix}v></{prefix}c>'

        changed = AY_CELL.sub(replace, xml)
        if replaced != set(updates):
            raise ValueError("Could not find every AY cell; output was not written")
        ET.fromstring(changed)
        destination.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with destination.open("xb") as handle:
                created = True
                with ZipFile(handle, "w") as output:
                    output.comment = archive.comment
                    for info in archive.infolist():
                        output.writestr(info, changed.encode("utf-8") if info.filename == sheet.part else archive.read(info.filename))
        except Exception:
            if created and destination.exists():
                destination.unlink()
            raise
