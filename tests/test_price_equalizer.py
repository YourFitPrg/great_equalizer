import tempfile
import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

from price_equalizer import calculate_limit, plan_equalization, read_mapping, select_wb_price
from xlsx_io import MAIN, REL, read_xlsx, write_ay_copy


def book(path, sheets):
    workbook = ET.Element(f"{{{MAIN}}}workbook")
    sheet_nodes = ET.SubElement(workbook, f"{{{MAIN}}}sheets")
    rels = ET.Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
    with ZipFile(path, "w") as archive:
        for index, (name, rows) in enumerate(sheets.items(), 1):
            ET.SubElement(sheet_nodes, f"{{{MAIN}}}sheet", name=name, sheetId=str(index), attrib={f"{{{REL}}}id": f"rId{index}"})
            ET.SubElement(rels, "Relationship", Id=f"rId{index}", Target=f"worksheets/sheet{index}.xml", Type=REL + "/worksheet")
            # Keep unprefixed tags to exercise the actual vendor template layout.
            root = ET.Element("worksheet", xmlns=MAIN)
            data = ET.SubElement(root, "sheetData")
            for number, columns in rows.items():
                row = ET.SubElement(data, "row", r=str(number))
                for column, raw in columns.items():
                    cell = ET.SubElement(row, "c", r=f"{column}{number}", s="1")
                    if isinstance(raw, tuple):
                        cached, formula = raw
                        ET.SubElement(cell, "f").text = formula
                        if cached is not None:
                            ET.SubElement(cell, "v").text = str(cached)
                    elif isinstance(raw, str):
                        cell.set("t", "inlineStr")
                        ET.SubElement(ET.SubElement(cell, "is"), "t").text = raw
                    elif raw is not None:
                        ET.SubElement(cell, "v").text = str(raw)
            archive.writestr(f"xl/worksheets/sheet{index}.xml", ET.tostring(root, encoding="utf-8"))
        archive.writestr("xl/workbook.xml", ET.tostring(workbook, encoding="utf-8"))
        archive.writestr("xl/_rels/workbook.xml.rels", ET.tostring(rels, encoding="utf-8"))
        archive.writestr("xl/styles.xml", b"original vendor stylesheet: do not rewrite")
        archive.writestr("custom/preserve.bin", b"preserve all non-worksheet content")


MAPPING_HEADER = {"A": "Артикул продавца OZON", "B": "Внешний код OZON", "C": "Внешний код WB"}
TEMPLATE_HEADER = {"A": "Артикул", "B": "SKU", "P": "Предельная цена, руб.",
                   "R": "Цена реализации, руб.", "AY": "Новая предельная цена, руб."}


class FormulaTests(unittest.TestCase):
    def test_ratio_is_remaining_fraction_not_discount_fraction(self):
        # An illustrative old limit of 4000; the user's absent example SKU is not assigned this value.
        new, ratio, expected = calculate_limit("3376", "4000", "2866")
        self.assertEqual(ratio, Decimal("0.7165"))
        self.assertEqual(new, Decimal("4711.79"))
        self.assertEqual(expected, Decimal("3376.00"))

    def test_lowering_price_uses_the_same_formula(self):
        new, ratio, expected = calculate_limit("100", "200", "150")
        self.assertEqual(new, Decimal("133.33"))
        self.assertEqual(expected, Decimal("100.00"))

    def test_invalid_denominator_is_not_used(self):
        for limit, realization in [(0, 10), (10, 0), (10, 11)]:
            with self.assertRaises(ValueError):
                calculate_limit(100, limit, realization)

    def test_ambiguous_size_prices_are_not_averaged_or_minimized(self):
        entries = [{"price_after_spp_rub": "100", "wallet_price_estimate_rub": "97"},
                   {"price_after_spp_rub": "200", "wallet_price_estimate_rub": "194"}]
        self.assertEqual(select_wb_price(entries, "wallet")[2], "wb_size_prices_ambiguous")

    def test_identical_size_prices_can_be_used(self):
        entry = {"price_after_spp_rub": "100", "wallet_price_estimate_rub": "97"}
        self.assertEqual(select_wb_price([entry, entry], "wallet"), (Decimal("97"), Decimal("100"), "ok"))

    def test_missing_size_price_is_not_replaced_by_an_available_size(self):
        entries = [{"price_after_spp_rub": "100", "wallet_price_estimate_rub": "97"},
                   {"price_after_spp_rub": None, "wallet_price_estimate_rub": None}]
        self.assertEqual(select_wb_price(entries, "wallet")[2], "wb_size_price_missing")


class WorkbookTests(unittest.TestCase):
    def test_formula_without_cached_value_is_resolved_from_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mapping.xlsx"
            book(path, {"Лист1": {1: MAPPING_HEADER, 2: {"A": "vendor", "B": 20, "C": (None, "VLOOKUP(A2,Лист2!A:C,3,0)")}},
                        "Лист2": {5: {"A": "vendor", "B": 100, "C": 30}}})
            mapping, _ = read_mapping(path)
            self.assertEqual(mapping["20"]["wb_id"], "30")

    def test_conflicting_duplicate_sku_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mapping.xlsx"
            book(path, {"Лист1": {1: MAPPING_HEADER,
                                 2: {"A": "a", "B": 20, "C": 30},
                                 3: {"A": "b", "B": 20, "C": 31}}})
            mapping, _ = read_mapping(path)
            self.assertEqual(mapping["20"]["status"], "mapping_ambiguous_sku")

    def test_only_ay_is_changed_and_all_other_zip_parts_are_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.xlsx", Path(directory) / "output.xlsx"
            book(source, {"Инструкция": {1: {"A": "Do not change structure"}},
                          "Товары и цены": {2: TEMPLATE_HEADER,
                                          5: {"A": "v", "B": 20, "P": 200, "R": 150, "AY": None, "AZ": 180}}})
            write_ay_copy(source, output, {5: Decimal("133.33")})
            with ZipFile(source) as before, ZipFile(output) as after:
                self.assertEqual(before.namelist(), after.namelist())
                for name in before.namelist():
                    if name != "xl/worksheets/sheet2.xml":
                        self.assertEqual(before.read(name), after.read(name))
            old = read_xlsx(source)["Товары и цены"]
            new = read_xlsx(output)["Товары и цены"]
            self.assertEqual(new.rows[5]["AY"].value, "133.33")
            self.assertIsNone(old.rows[5]["AY"].value)
            for row, cells in old.rows.items():
                for column, cell in cells.items():
                    if (row, column) != (5, "AY"):
                        self.assertEqual(cell, new.rows[row][column])
            with self.assertRaises(ValueError):
                write_ay_copy(source, output, {5: Decimal("200")})

    def test_default_plan_raises_and_lowers_and_uses_wallet_target(self):
        with tempfile.TemporaryDirectory() as directory:
            mapping, template = Path(directory) / "map.xlsx", Path(directory) / "template.xlsx"
            book(mapping, {"Лист1": {1: MAPPING_HEADER, 2: {"A": "a", "B": 20, "C": 30}, 3: {"A": "b", "B": 21, "C": 31}}})
            book(template, {"Товары и цены": {2: TEMPLATE_HEADER,
                5: {"A": "a", "B": 20, "P": 200, "R": 150, "AY": None},
                6: {"A": "b", "B": 21, "P": 200, "R": 150, "S": 200, "U": "ДА", "AY": None},
                7: {"A": "missing", "B": 22, "P": 200, "R": 150, "AY": None}}})
            snapshot = {"wallet_discount_percent_assumed": "3", "prices": [
                {"article": 30, "price_after_spp_rub": "309", "wallet_price_estimate_rub": "300"},
                {"article": 31, "price_after_spp_rub": "103", "wallet_price_estimate_rub": "100"}]}
            plan = plan_equalization(mapping, template, snapshot)
            self.assertEqual(plan["updates"], {5: Decimal("400.00"), 6: Decimal("133.33")})
            self.assertEqual(plan["summary"], {"update": 2, "mapping_not_found": 1})
            self.assertIn("new_limit_below_current_promotion_floor", plan["audit"][1]["note"])


if __name__ == "__main__":
    unittest.main()
