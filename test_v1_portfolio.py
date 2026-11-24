"""Coverage for the V1 read-only broker boundary and scenario calculations."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from v1_portfolio import analyze, require_portfolio, simulate
from v1_scalable import SyncError, read_tool

NOW = datetime(2026, 11, 24, 20, tzinfo=timezone.utc)
AT = NOW.isoformat()
APPLE = "US0378331005"
ASML = "NL0010273215"
NEW = "US5949181045"


def packet():
    return dict(synced_at=AT, holdings=dict(portfolioId="sample", holdings=[
        dict(isin=APPLE, name="Apple", position=dict(filled=2),
             currentQuote=dict(currency="EUR", midPrice=30, timestamp=AT)),
        dict(isin=ASML, name="ASML", position=dict(filled=3),
             currentQuote=dict(currency="EUR", midPrice=20, timestamp=AT)),
    ]), cash=dict(portfolioId="sample", cash=dict(buyingPowerWithoutCredit=70, buyingPower=8000)),
        overview=dict(portfolioId="sample", valuation=dict(total=1000)))


def financials(info):
    currency = "USD" if info.get("isin") == APPLE else "EUR"
    return dict(overview=dict(sector="Technology", industry="Semiconductors", financialCurrency=currency),
                updated_at=AT, metrics=[
        dict(metric=metric, period_end=f"{year}-12-31", frequency="yearly", currency=currency,
             value=value, unit="shares" if metric == "OrdinarySharesNumber" else "money")
        for year in (2025, 2024, 2023)
        for metric, value in (("TotalRevenue", 2000), ("FreeCashFlow", 100), ("OrdinarySharesNumber", 10))
    ])


def report(data=None, now=NOW):
    inputs = [dict(isin=APPLE, name="Apple", kind="stock"),
              dict(isin=ASML, name="ASML", kind="stock"), dict(isin=NEW, name="Microsoft", kind="stock")]
    articles = lambda isin: dict(articles=[dict(isin=isin, title="Quarterly results",
                                               url="https://issuer.example/results", published=AT)])
    return analyze(data or packet(), inputs, financials, articles, now, {"USD": 1.25}, lambda *_: 20)


class PortfolioTest(unittest.TestCase):
    def test_scope_and_mismatch(self):
        snapshot = packet()
        with self.assertRaisesRegex(ValueError, "SC_PORTFOLIO_MISMATCH"):
            require_portfolio(snapshot["holdings"], snapshot["cash"], {"portfolioId": "another"})

    def test_purchase_threshold_and_sources(self):
        data = report()
        self.assertEqual(data["covered"], 2)
        self.assertEqual(data["cash"], 70)
        self.assertEqual(data["positions"][0]["zone"]["buy_below"], 68)
        self.assertEqual(data["positions"][0]["decision"], "Рассмотреть докупку")
        self.assertEqual(data["candidates"][0]["decision"], "Рассмотреть покупку")
        self.assertEqual(data["positions"][0]["articles"][0]["date"], AT)

    def test_what_if_uses_own_cash_and_does_not_mutate_snapshot(self):
        data = report()
        with self.assertRaisesRegex(ValueError, "V1_AMOUNT_EXCEEDS"):
            simulate(data, APPLE, 71)
        change = simulate(data, APPLE, 40)
        self.assertEqual((change["before"], change["after"]), (.06, .10))
        self.assertAlmostEqual(change["scenarios"][0]["loss"], -32)
        self.assertEqual(change["scenarios"][2]["loss"], -10)
        self.assertEqual(data["positions"][0]["value"], 60)
        self.assertEqual(simulate(data, NEW, 20)["before"], 0)
        self.assertEqual(simulate(data, APPLE, -60)["after"], 0)

    def test_stale_report_cannot_model_a_trade(self):
        data = report(now=NOW.replace(hour=21))
        self.assertEqual(data["positions"][0]["decision"], "Обновить данные")
        with self.assertRaisesRegex(ValueError, "V1_REFRESH_REQUIRED"):
            simulate(data, APPLE, 10)

    def test_unknown_currency_is_not_a_euro_valuation(self):
        snapshot = packet()
        snapshot["holdings"]["holdings"][0]["currentQuote"]["currency"] = "USD"
        data = report(snapshot)
        self.assertEqual(data["covered"], 1)
        self.assertIsNone(data["positions"][-1]["value"])


class ToolBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_whitelist_rejects_broker_mutations(self):
        with self.assertRaisesRegex(SyncError, "SC_READ_ONLY"):
            await read_tool(None, "place_order")

    async def test_parser_uses_broker_structured_result(self):
        class Session:
            async def call_tool(self, name, args):
                assert name == "get_portfolio_holdings" and args == {"portfolioId": "sample"}
                return SimpleNamespace(structuredContent=packet()["holdings"], isError=False)

        self.assertEqual((await read_tool(Session(), "get_portfolio_holdings", {"portfolioId": "sample"}))["portfolioId"],
                         "sample")


if __name__ == "__main__":
    unittest.main()
