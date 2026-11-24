"""Coverage for the V1 read-only broker boundary and scenario calculations."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from v1_portfolio import analyze, require_portfolio, simulate
from v1_cost_basis import cost_basis
from v1_scalable import SyncError, read_tool, read_transactions

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


def movement(id, hour, action, quantity, amount, reversal=False):
    row = dict(id=id, lastEventAt=NOW.replace(hour=hour).isoformat(), status="SETTLED",
               currency="EUR", isCancellation=reversal)
    values = dict(isin=APPLE, quantity=str(quantity), amount=str(amount))
    if action in ("BUY", "SELL"):
        return {**row, "kind": "security", "security": {**values, "side": action}}
    return {**row, "kind": "non_trade_security", "nonTradeSecurity": {**values, "transactionType": action}}


class NewsOverlayTest(unittest.TestCase):
    def evaluate(self, articles):
        stock = dict(isin=APPLE, name="Apple", kind="stock")
        return analyze(packet(), [stock], financials, lambda _: {"articles": articles},
                       NOW, {"USD": 1.25})["positions"][0]

    def test_issuer_guidance_cut_changes_the_review_status(self):
        story = dict(isin=APPLE, title="Apple cuts full-year guidance",
                     url="https://apple.example/report", published=AT)
        article = dict(story, relevance="OFFICIAL_ISSUER_FEED")
        row = self.evaluate([story, article])
        self.assertEqual(row["baseline"], "Рассмотреть докупку")
        self.assertEqual(row["decision"], "Пересмотреть позицию")
        self.assertEqual(row["news_reason"], "GUIDANCE_CUT")
        self.assertTrue(row["articles"][0]["verified"])
        self.assertEqual(len(row["articles"]), 1)

    def test_unverified_media_and_issuer_rumor_leave_decision_unchanged(self):
        story = dict(isin=APPLE, title="Apple cuts full-year guidance",
                     url="https://publisher.example/report", published=AT)
        row = self.evaluate([story, dict(story, title="Rumors: Apple cuts guidance?",
                                       relevance="OFFICIAL_ISSUER_FEED")])
        self.assertEqual(row["decision"], "Рассмотреть докупку")
        self.assertIsNone(row["news_reason"])


class CostBasisTest(unittest.TestCase):
    def test_partial_sell_keeps_open_fifo_lots_and_reports_pnl(self):
        rows = [movement("a", 8, "BUY", 2, -20), movement("b", 9, "BUY", 3, -60),
                movement("c", 10, "SELL", 3, 75)]
        snapshot = packet()
        snapshot["cost_basis"] = cost_basis(list(reversed(rows)), snapshot["holdings"]["holdings"])
        row = report(snapshot)["positions"][0]
        self.assertEqual((row["average"], row["cost"], row["pnl"], row["pnl_pct"]), (20, 40, 20, .5))
        self.assertEqual(row["basis_status"], "FIFO_ESTIMATE")

    def test_transfer_reversal_removes_original_lot(self):
        rows = [movement("a", 8, "TRANSFER_IN", 2, 90), movement("b", 9, "BUY", 2, -30),
                movement("c", 10, "TRANSFER_IN", 2, 90, reversal=True)]
        self.assertEqual(cost_basis(rows, packet()["holdings"]["holdings"])[APPLE]["average"], 15)

    def test_matched_transfer_needs_original_purchase_chronology(self):
        rows = [movement("a", 8, "TRANSFER_IN", 2, 90)]
        result = cost_basis(rows, packet()["holdings"]["holdings"])[APPLE]
        self.assertEqual((result["average"], result["status"]), (45, "TRANSFER_ESTIMATE"))

    def test_ambiguous_reversal_with_identical_lots_stays_unknown(self):
        rows = [movement("a", 8, "TRANSFER_IN", 2, 90), movement("b", 9, "TRANSFER_IN", 2, 90),
                movement("c", 10, "TRANSFER_IN", 2, 90, reversal=True)]
        self.assertEqual(cost_basis(rows, packet()["holdings"]["holdings"])[APPLE],
                         {"status": "INCOMPLETE_OR_AMBIGUOUS"})

    def test_same_time_migration_consumes_older_lots(self):
        rows = [movement("a", 8, "BUY", 2, -20),
                movement("b", 9, "TRANSFER_IN", 2, 45),
                movement("c", 9, "TRANSFER_OUT", 2, -45)]
        self.assertEqual(cost_basis(rows, packet()["holdings"]["holdings"])[APPLE]["average"], 22.5)

    def test_same_time_outgoing_cannot_assume_missing_old_lot(self):
        rows = [movement("a", 8, "BUY", 1, -20),
                movement("b", 9, "TRANSFER_IN", 3, 45),
                movement("c", 9, "TRANSFER_OUT", 2, -45)]
        self.assertEqual(cost_basis(rows, packet()["holdings"]["holdings"])[APPLE],
                         {"status": "INCOMPLETE_OR_AMBIGUOUS"})

    def test_unknown_corporate_action_cost_and_quantity_mismatch(self):
        shares = packet()["holdings"]["holdings"]
        self.assertEqual(cost_basis([movement("a", 8, "SWAP_IN", 2, 0)], shares)[APPLE],
                         {"status": "MISSING_LOT_COST"})
        self.assertEqual(cost_basis([movement("b", 9, "BUY", 1, -25)], shares)[APPLE],
                         {"status": "QUANTITY_MISMATCH"})


class ToolBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_whitelist_rejects_broker_mutations(self):
        with self.assertRaisesRegex(SyncError, "SC_READ_ONLY"):
            await read_tool(None, "place_order")

    async def test_portfolio_history_paginates_until_cursor_ends(self):
        class Session:
            async def call_tool(self, name, args):
                assert name == "list_portfolio_transactions" and args["statuses"] == ["SETTLED"]
                cursor = args.get("cursor")
                return SimpleNamespace(structuredContent=dict(portfolioId="sample",
                    transactions=[{"id": cursor or "one"}],
                    page={"nextCursor": None if cursor else "two"}), isError=False)
        self.assertEqual(await read_transactions(Session(), "sample"), [{"id": "one"}, {"id": "two"}])
        with self.assertRaisesRegex(SyncError, "SC_TRANSACTION_HISTORY_INCOMPLETE"):
            await read_transactions(Session(), "sample", max_pages=1)

    async def test_parser_uses_broker_structured_result(self):
        class Session:
            async def call_tool(self, name, args):
                assert name == "get_portfolio_holdings" and args == {"portfolioId": "sample"}
                return SimpleNamespace(structuredContent=packet()["holdings"], isError=False)

        self.assertEqual((await read_tool(Session(), "get_portfolio_holdings", {"portfolioId": "sample"}))["portfolioId"],
                         "sample")


if __name__ == "__main__":
    unittest.main()
