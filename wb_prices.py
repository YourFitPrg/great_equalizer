"""Read buyer-facing WB prices using an existing browser session.

This prototype does not create or refresh browser cookies, log into WB, change
seller prices, or interact with Ozon. Keep session values out of source control.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import requests


PRICE_URL = "https://www.wildberries.ru/__internal/card/cards/v4/detail"
DEFAULT_ARTICLES = [732445767, 732449137, 386287758, 1247697027, 1050925338, 804787539]


class WBRequestError(RuntimeError):
    """A safe error message that never includes request credentials or bodies."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(repr=False)
class BrowserContext:
    wbaas_token: str
    headers: dict[str, str]

    def __repr__(self) -> str:
        return "BrowserContext(<session values hidden>)"

    @classmethod
    def create(cls, token: str, deviceid: str, user_agent: str) -> BrowserContext:
        return cls(token, {"accept": "application/json", "deviceid": deviceid, "user-agent": user_agent})

    def request_headers(self) -> dict[str, str]:
        headers = {
            key.lower(): value for key, value in self.headers.items()
            if key.lower() not in {"authorization", "cookie"}
        }
        if not all(isinstance(value, str) and value.strip() for value in (
            self.wbaas_token, headers.get("deviceid"), headers.get("user-agent")
        )):
            raise ValueError("x_wbaas_token, deviceid and User-Agent are required")
        return headers


def load_browser_context(path: str | Path) -> BrowserContext:
    """Read literal cookies/headers from a local Python export without executing it."""
    settings: dict[str, Any] = {}
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8-sig"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"cookies", "headers"}:
                    settings[target.id] = ast.literal_eval(node.value)
        cookies = settings.get("cookies", {})
        headers = settings.get("headers", {})
        if not isinstance(cookies, dict) or not isinstance(headers, dict):
            raise ValueError()
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise ValueError()
        context = BrowserContext(cookies.get("x_wbaas_token", ""), headers)
        context.request_headers()
        return context
    except (OSError, SyntaxError, ValueError, TypeError):
        raise ValueError("Cannot read literal cookies/headers from the context file") from None


def parse_prices(data: Any, articles: list[int], wallet_discount: Decimal = Decimal("3")) -> dict:
    """Amounts from price.product are hundredths of a ruble; wallet is an estimate."""
    if not wallet_discount.is_finite() or not Decimal("0") <= wallet_discount <= Decimal("100"):
        raise ValueError("Wallet discount must be between 0 and 100")
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise WBRequestError("WB did not return a products array")
    wanted = set(articles)
    found: set[int] = set()
    rows = []
    seen: set[tuple[int, Any]] = set()
    try:
        for product in data["products"]:
            article = product.get("id")
            if article not in wanted:
                continue
            found.add(article)
            for size in product.get("sizes") or [{}]:
                key = (article, size.get("optionId"))
                if key in seen:
                    continue
                seen.add(key)
                raw = (size.get("price") or {}).get("product")
                price = Decimal(str(raw)) / 100 if raw is not None else None
                if price is not None and (not price.is_finite() or price <= 0):
                    price = None
                wallet = (
                    (price * (1 - wallet_discount / 100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                    if price is not None else None
                )
                rows.append({
                    "article": article,
                    "size_id": size.get("optionId"),
                    "price_after_spp_rub": str(price) if price is not None else None,
                    "wallet_price_estimate_rub": str(wallet) if wallet is not None else None,
                })
    except (AttributeError, TypeError, InvalidOperation):
        raise WBRequestError("WB returned an unexpected product price structure") from None
    missing = sorted(wanted - found)
    return {
        "prices": rows,
        "missing_articles": missing,
        "wallet_discount_percent_assumed": str(wallet_discount),
        "complete": bool(rows) and not missing and all(row["price_after_spp_rub"] is not None for row in rows),
    }


def fetch_prices(
    articles: list[int], context: BrowserContext, *, dest: str,
    wallet_discount: Decimal = Decimal("3"), timeout: float = 30,
) -> dict:
    if not articles or any(type(article) is not int or article <= 0 for article in articles):
        raise ValueError("Provide at least one positive WB article number")
    if not dest or not str(dest).lstrip("-").isdigit():
        raise ValueError("Provide a numeric delivery destination")
    if not wallet_discount.is_finite() or not Decimal("0") <= wallet_discount <= Decimal("100"):
        raise ValueError("Wallet discount must be between 0 and 100")
    params = {
        "appType": "1", "curr": "rub", "dest": str(dest), "hide_dtype": "13",
        "spp": "30", "ab_testing": "false", "lang": "ru",
        "nm": ";".join(str(article) for article in dict.fromkeys(articles)),
    }
    headers = context.request_headers()
    try:
        response = requests.get(
            PRICE_URL, params=params, cookies={"x_wbaas_token": context.wbaas_token},
            headers=headers, timeout=timeout, allow_redirects=False,
        )
    except requests.RequestException:
        raise WBRequestError("Network request to WB failed; the response could not be obtained") from None
    if response.status_code != 200:
        raise WBRequestError(
            f"WB returned HTTP {response.status_code}; login requirements cannot be inferred from this status alone",
            status_code=response.status_code,
        )
    try:
        data = response.json()
    except ValueError:
        raise WBRequestError("WB returned HTTP 200 but the response was not JSON", status_code=200) from None
    result = parse_prices(data, articles, wallet_discount)
    result.update({
        "http_status": response.status_code,
        "dest": str(dest),
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("articles", nargs="*", type=int, default=DEFAULT_ARTICLES)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--context-file", type=Path, help="Private local Python file containing cookies and headers")
    parser.add_argument("--wallet-discount", type=Decimal, default=Decimal("3"))
    args = parser.parse_args()
    try:
        context = (
            load_browser_context(args.context_file) if args.context_file else BrowserContext.create(
                os.environ.get("WB_WBAAS_TOKEN", ""), os.environ.get("WB_DEVICE_ID", ""),
                os.environ.get("WB_USER_AGENT", ""),
            )
        )
        result = fetch_prices(args.articles, context, dest=args.dest, wallet_discount=args.wallet_discount)
    except (ValueError, WBRequestError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
