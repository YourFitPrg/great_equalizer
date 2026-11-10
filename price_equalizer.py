"""Join WB prices to Ozon SKU and prepare a new Ozon price-update workbook.

Default target: WB price after SPP and the agreed 3% Wallet estimate.
Default direction: both increases and decreases. No prices are uploaded to Ozon.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from wb_prices import WBRequestError, fetch_prices, load_browser_context
from xlsx_io import Cell, read_xlsx, write_ay_copy

MONEY = Decimal("0.01")


def normalize_id(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value).strip())
        return str(int(number)) if number.is_finite() and number > 0 and number == number.to_integral_value() else None
    except (InvalidOperation, ValueError):
        return None


def value(cells, column):
    return cells.get(column, Cell(None)).value


def vendor_key(text):
    return str(text or "").strip().casefold()


def read_mapping(path):
    sheets = read_xlsx(path)
    if "Лист1" not in sheets:
        raise ValueError("Mapping sheet 'Лист1' was not found")
    sheet = sheets["Лист1"]
    expected = {"A": "Артикул продавца OZON", "B": "Внешний код OZON", "C": "Внешний код WB"}
    if any(value(sheet.rows.get(1, {}), col) != text for col, text in expected.items()):
        raise ValueError("Mapping columns A:C do not match the supplied format")
    lookup = defaultdict(set)
    if "Лист2" in sheets:
        for row, cells in sheets["Лист2"].rows.items():
            if row < 5:
                continue
            wb = normalize_id(value(cells, "C"))
            if wb and vendor_key(value(cells, "A")):
                lookup[vendor_key(value(cells, "A"))].add(wb)
    candidates = defaultdict(list)
    issues = []
    for row, cells in sheet.rows.items():
        if row == 1 or not any(c.value for c in cells.values()):
            continue
        sku = normalize_id(value(cells, "B"))
        vendor = value(cells, "A") or ""
        wb_cell = cells.get("C", Cell(None))
        wb = normalize_id(wb_cell.value)
        status = "ok"
        matches = lookup.get(vendor_key(vendor), set())
        if wb_cell.formula:
            if len(matches) > 1 or (wb and matches and wb not in matches):
                status = "mapping_formula_conflict"
            elif wb is None and len(matches) == 1:
                wb = next(iter(matches))
        if sku is None:
            status = "mapping_missing_ozon_sku"
        elif wb is None and status == "ok":
            status = "mapping_missing_wb_id"
        link = {"mapping_row": row, "ozon_sku": sku, "vendor_code": vendor, "wb_id": wb, "status": status}
        if status != "ok":
            issues.append(link)
        if sku:
            candidates[sku].append(link)
    resolved = {}
    for sku, links in candidates.items():
        signatures = {(link["wb_id"], link["status"]) for link in links}
        if len(signatures) != 1:
            resolved[sku] = {**links[0], "status": "mapping_ambiguous_sku"}
        else:
            resolved[sku] = links[0]
    return resolved, issues


def read_template(path):
    sheet = read_xlsx(path).get("Товары и цены")
    if sheet is None:
        raise ValueError("Ozon sheet 'Товары и цены' was not found")
    expected = {"A": "Артикул", "B": "SKU", "P": "Предельная цена, руб.",
                "R": "Цена реализации, руб.", "AY": "Новая предельная цена, руб."}
    if any(value(sheet.rows.get(2, {}), col) != text for col, text in expected.items()):
        raise ValueError("Ozon headers A/B/P/R/AY do not match the supplied template")
    rows = []
    for row, cells in sheet.rows.items():
        if row < 5 or not value(cells, "B"):
            continue
        sku = normalize_id(value(cells, "B"))
        rows.append({
            "template_row": row, "ozon_sku": sku,
            "vendor_code": value(cells, "A") or "", "title": value(cells, "C") or "",
            "limit_price": value(cells, "P"), "realization_price": value(cells, "R"),
            "crossed_out_price": value(cells, "O"), "promotion_floor": value(cells, "S"),
            "promotion_floor_enabled": value(cells, "U"),
            "existing_new_limit": value(cells, "AY") if not cells.get("AY", Cell(None)).formula else "formula_present",
        })
    return rows


def articles_for_template(mapping_path, template_path):
    mapping, issues = read_mapping(mapping_path)
    rows = read_template(template_path)
    articles = sorted({int(mapping[row["ozon_sku"]]["wb_id"]) for row in rows
                       if row["ozon_sku"] in mapping and mapping[row["ozon_sku"]]["status"] == "ok"})
    return articles


def fetch_template_snapshot(mapping_path, template_path, context, *, dest, batch_size=50):
    if batch_size < 1 or batch_size > 100:
        raise ValueError("Batch size must be between 1 and 100")
    articles = articles_for_template(mapping_path, template_path)
    if not articles:
        raise ValueError("No valid WB articles matched the Ozon template")
    snapshots = []
    for start in range(0, len(articles), batch_size):
        if start:
            time.sleep(1)
        batch = articles[start:start + batch_size]
        # Stop on HTTP/network failures; do not silently use old prices.
        snapshots.append(fetch_prices(batch, context, dest=dest))
    return {
        "dest": str(dest), "wallet_discount_percent_assumed": "3",
        "retrieved_at_utc": snapshots[-1]["retrieved_at_utc"],
        "first_batch_at_utc": snapshots[0]["retrieved_at_utc"],
        "request_count": len(snapshots), "requested_articles": articles,
        "prices": [row for snapshot in snapshots for row in snapshot["prices"]],
        "missing_articles": sorted({a for snapshot in snapshots for a in snapshot["missing_articles"]}),
    }


def select_wb_price(entries, target_mode):
    if not entries:
        return None, None, "wb_article_not_returned"
    column = "wallet_price_estimate_rub" if target_mode == "wallet" else "price_after_spp_rub"
    if any(entry.get(column) is None or entry.get("price_after_spp_rub") is None for entry in entries):
        return None, None, "wb_size_price_missing"
    try:
        targets = {Decimal(str(entry[column])) for entry in entries}
        basic = {Decimal(str(entry["price_after_spp_rub"])) for entry in entries}
    except InvalidOperation:
        return None, None, "wb_invalid_price"
    if len(targets) != 1 or len(basic) != 1:
        return None, None, "wb_size_prices_ambiguous"
    target, before_wallet = next(iter(targets)), next(iter(basic))
    if not target.is_finite() or target <= 0:
        return None, None, "wb_invalid_price"
    return target, before_wallet, "ok"


def calculate_limit(target, old_limit, realization):
    values = [Decimal(str(x)) for x in (target, old_limit, realization)]
    if any(not number.is_finite() or number <= 0 for number in values):
        raise ValueError("Prices must be positive and finite")
    target, old_limit, realization = values
    ratio = realization / old_limit
    if ratio > 1:
        raise ValueError("Realization price exceeds the limit price; inspect the source row")
    new_limit = (target * old_limit / realization).quantize(MONEY, rounding=ROUND_HALF_UP)
    return new_limit, ratio, (new_limit * ratio).quantize(MONEY, rounding=ROUND_HALF_UP)


def plan_equalization(mapping_path, template_path, snapshot, *, target_mode="wallet", direction="both"):
    if target_mode not in {"wallet", "without-wallet"} or direction not in {"both", "raise-only"}:
        raise ValueError("Unsupported target mode or price direction")
    if target_mode == "wallet" and Decimal(str(snapshot.get("wallet_discount_percent_assumed", "NaN"))) != Decimal("3"):
        raise ValueError("This workflow expects the agreed WB Wallet discount of 3 percent")
    mapping, issues = read_mapping(mapping_path)
    template = read_template(template_path)
    sku_counts = Counter(row["ozon_sku"] for row in template)
    wb_prices = defaultdict(list)
    for entry in snapshot.get("prices", []):
        article = normalize_id(entry.get("article"))
        if article:
            wb_prices[article].append(entry)
    updates = {}
    audit = []
    for row in template:
        record = {**row, "wb_id": None, "wb_price_after_spp_rub": None, "target_wb_rub": None,
                  "wb_dest": snapshot.get("dest"), "wb_retrieved_at_utc": snapshot.get("retrieved_at_utc"),
                  "target_mode": target_mode,
                  "ozon_price_coefficient": None, "ozon_discount_percent": None,
                  "new_limit_price_rub": None, "modeled_realization_rub": None,
                  "direction": None, "status": "pending", "note": ""}
        sku = row["ozon_sku"]
        link = mapping.get(sku)
        if sku is None or sku_counts[sku] > 1:
            record["status"] = "template_invalid_or_duplicate_sku"
        elif link is None:
            record["status"] = "mapping_not_found"
        elif link["status"] != "ok":
            record["status"] = link["status"]
        elif row["existing_new_limit"] not in (None, ""):
            record["status"] = "existing_ay_preserved"
        else:
            record["wb_id"] = link["wb_id"]
            if vendor_key(row["vendor_code"]) != vendor_key(link["vendor_code"]):
                record["note"] = "vendor_code_differs; joined by exact Ozon SKU"
            target, before_wallet, status = select_wb_price(wb_prices[link["wb_id"]], target_mode)
            record["status"] = status
            if status == "ok":
                record["target_wb_rub"], record["wb_price_after_spp_rub"] = str(target), str(before_wallet)
                try:
                    new_limit, ratio, expected = calculate_limit(target, row["limit_price"], row["realization_price"])
                except (ValueError, InvalidOperation, TypeError):
                    record["status"] = "ozon_invalid_prices"
                else:
                    change = new_limit - Decimal(str(row["limit_price"]))
                    record.update({
                        "ozon_price_coefficient": str(ratio),
                        "ozon_discount_percent": str(((1 - ratio) * 100).quantize(Decimal("0.0001"))),
                        "new_limit_price_rub": str(new_limit), "modeled_realization_rub": str(expected),
                        "direction": "increase" if change > 0 else "decrease" if change < 0 else "unchanged",
                    })
                    notes = [record["note"]] if record["note"] else []
                    if row["promotion_floor_enabled"] == "ДА" and row["promotion_floor"]:
                        try:
                            if new_limit < Decimal(row["promotion_floor"]):
                                notes.append("new_limit_below_current_promotion_floor")
                        except InvalidOperation:
                            notes.append("promotion_floor_not_numeric")
                    record["note"] = "; ".join(notes)
                    if change == 0:
                        record["status"] = "already_aligned"
                    elif change < 0 and direction == "raise-only":
                        record["status"] = "decrease_skipped"
                    else:
                        record["status"] = "update"
                        updates[row["template_row"]] = new_limit
        audit.append(record)
    return {
        "updates": updates, "audit": audit, "mapping_issues": issues,
        "summary": dict(Counter(row["status"] for row in audit)),
        "target_mode": target_mode, "direction": direction,
        "dest": snapshot.get("dest"), "wb_retrieved_at_utc": snapshot.get("retrieved_at_utc"),
    }


def write_outputs(template_path, plan, output_path, audit_path):
    output_path, audit_path = Path(output_path), Path(audit_path)
    if output_path.resolve() == audit_path.resolve() or output_path.exists() or audit_path.exists():
        raise ValueError("Choose new output paths; existing files are not overwritten")
    write_ay_copy(template_path, output_path, plan["updates"])
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("x", encoding="utf-8-sig", newline="") as handle:
        columns = list(plan["audit"][0]) if plan["audit"] else ["status"]
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter=";")
        writer.writeheader()
        writer.writerows(plan["audit"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, help="Previously saved WB price JSON; omit to fetch live")
    parser.add_argument("--context-file", type=Path)
    parser.add_argument("--dest", default="-1257786")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--save-snapshot", type=Path)
    parser.add_argument("--target", choices=["wallet", "without-wallet"], default="wallet")
    parser.add_argument("--direction", choices=["both", "raise-only"], default="both")
    args = parser.parse_args()
    try:
        destinations = [args.output, args.audit] + ([args.save_snapshot] if args.save_snapshot else [])
        if len({path.resolve() for path in destinations}) != len(destinations) or any(path.exists() for path in destinations):
            raise ValueError("Output, audit and snapshot destinations must be distinct new files")
        if args.snapshot:
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8-sig"))
        else:
            if not args.context_file:
                raise ValueError("Provide --context-file for live requests or --snapshot for offline calculations")
            snapshot = fetch_template_snapshot(args.mapping, args.template, load_browser_context(args.context_file), dest=args.dest)
        plan = plan_equalization(args.mapping, args.template, snapshot, target_mode=args.target, direction=args.direction)
        write_outputs(args.template, plan, args.output, args.audit)
        if args.save_snapshot:
            args.save_snapshot.parent.mkdir(parents=True, exist_ok=True)
            with args.save_snapshot.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    except (ValueError, OSError, WBRequestError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps({"summary": plan["summary"], "output": str(args.output), "audit": str(args.audit),
                      "dest": plan["dest"], "wb_retrieved_at_utc": plan["wb_retrieved_at_utc"],
                      "target_mode": plan["target_mode"], "direction": plan["direction"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
