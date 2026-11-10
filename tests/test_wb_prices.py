import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from wb_prices import BrowserContext, WBRequestError, fetch_prices, load_browser_context, parse_prices


class PriceParsingTests(unittest.TestCase):
    def test_observed_wb_price_and_wallet_rounding(self):
        payload = {"products": [{"id": 732445767, "sizes": [{
            "optionId": 1036875819, "price": {"basic": 2486000, "product": 363700, "wallet": 0},
        }]}]}
        result = parse_prices(payload, [732445767])
        self.assertTrue(result["complete"])
        self.assertEqual(result["prices"][0]["price_after_spp_rub"], "3637")
        self.assertEqual(result["prices"][0]["wallet_price_estimate_rub"], "3528")

    def test_wallet_half_ruble_rounds_up(self):
        payload = {"products": [{"id": 1, "sizes": [{"optionId": 2, "price": {"product": 5000}}]}]}
        self.assertEqual(parse_prices(payload, [1])["prices"][0]["wallet_price_estimate_rub"], "49")

    def test_each_size_keeps_its_own_price(self):
        payload = {"products": [{"id": 1, "sizes": [
            {"optionId": 2, "price": {"product": 10000}},
            {"optionId": 3, "price": {"product": 20000}},
        ]}]}
        rows = parse_prices(payload, [1])["prices"]
        self.assertEqual([(r["size_id"], r["price_after_spp_rub"]) for r in rows], [(2, "100"), (3, "200")])

    def test_missing_articles_and_prices_are_not_zero(self):
        payload = {"products": [{"id": 1, "sizes": [{"optionId": 2}]}]}
        result = parse_prices(payload, [1, 3])
        self.assertFalse(result["complete"])
        self.assertIsNone(result["prices"][0]["price_after_spp_rub"])
        self.assertEqual(result["missing_articles"], [3])

    def test_empty_sizes_are_incomplete(self):
        result = parse_prices({"products": [{"id": 1, "sizes": []}]}, [1])
        self.assertFalse(result["complete"])
        self.assertIsNone(result["prices"][0]["price_after_spp_rub"])

    def test_wrong_payload_is_rejected(self):
        with self.assertRaises(WBRequestError):
            parse_prices({"data": {"listGoods": []}}, [1])


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.context = BrowserContext("fixture-cookie", {
            "deviceid": "fixture-device", "user-agent": "fixture-agent",
            "Authorization": "must-not-be-sent", "Cookie": "must-not-be-sent",
        })

    @patch("wb_prices.requests.get")
    def test_request_sends_only_required_cookie_and_no_authorization(self, get):
        response = Mock(status_code=200)
        response.json.return_value = {"products": [{"id": 1, "sizes": [{"optionId": 2, "price": {"product": 10000}}]}]}
        get.return_value = response
        result = fetch_prices([1, 1], self.context, dest="-1257786")
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs["cookies"], {"x_wbaas_token": "fixture-cookie"})
        self.assertNotIn("authorization", kwargs["headers"])
        self.assertNotIn("cookie", kwargs["headers"])
        self.assertEqual(kwargs["params"]["nm"], "1")
        self.assertEqual(kwargs["params"]["dest"], "-1257786")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("verify", kwargs)
        self.assertTrue(result["complete"])

    @patch("wb_prices.requests.get")
    def test_498_does_not_expose_session_or_claim_login_is_required(self, get):
        get.return_value = Mock(status_code=498)
        with self.assertRaises(WBRequestError) as caught:
            fetch_prices([1], self.context, dest="-1257786")
        self.assertEqual(caught.exception.status_code, 498)
        self.assertNotIn("fixture-cookie", str(caught.exception))
        self.assertNotIn("fixture-device", repr(self.context))

    @patch("wb_prices.requests.get")
    def test_http_200_non_json_is_not_success(self, get):
        response = Mock(status_code=200)
        response.json.side_effect = ValueError("html instead of json")
        get.return_value = response
        with self.assertRaises(WBRequestError):
            fetch_prices([1], self.context, dest="-1257786")

    @patch("wb_prices.requests.get", side_effect=requests.Timeout("sensitive original error"))
    def test_network_failure_is_sanitized(self, get):
        with self.assertRaises(WBRequestError) as caught:
            fetch_prices([1], self.context, dest="-1257786")
        self.assertNotIn("sensitive original error", str(caught.exception))

    @patch("wb_prices.requests.get")
    def test_bad_inputs_do_not_send_request(self, get):
        with self.assertRaises(ValueError):
            fetch_prices([], self.context, dest="-1257786")
        with self.assertRaises(ValueError):
            fetch_prices([1], self.context, dest="-1257786", wallet_discount=Decimal("NaN"))
        get.assert_not_called()


class LocalContextTests(unittest.TestCase):
    def test_source_file_is_parsed_without_executing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "executed"
            source = Path(folder) / "private_request.py"
            source.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).touch()\n"
                "cookies = {'x_wbaas_token': 'fixture-cookie', 'account': 'unused'}\n"
                "headers = {'deviceid': 'fixture-device', 'user-agent': 'fixture-agent'}\n"
            )
            context = load_browser_context(source)
            self.assertFalse(marker.exists())
            self.assertEqual(context.wbaas_token, "fixture-cookie")
            self.assertEqual(context.request_headers()["deviceid"], "fixture-device")


if __name__ == "__main__":
    unittest.main()
