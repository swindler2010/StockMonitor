import unittest
from types import SimpleNamespace
from v1_transactions import transaction_rows, detail_lines
from v1_scalable import read_transactions

class HistoryTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_order_statuses(self):
        class Session:
            async def call_tool(self, name, args):
                assert "statuses" not in args
                return SimpleNamespace(isError=False, structuredContent={
                    "portfolioId": "p", "transactions": [], "page": {"nextCursor": None}})
        self.assertEqual(await read_transactions(Session(), "p", all_statuses=True), [])

    def test_pending_order_limit_is_not_an_execution_price(self):
        tx = {"id": "a", "kind": "security", "status": "CANCELLED", "currency": "USD",
              "security": {"isin": "US0378331005", "side": "BUY", "quantity": "3", "amount": "-600"}}
        rows = transaction_rows([tx], {})
        self.assertEqual(rows[0]["status"], "Отменена")
        self.assertEqual(rows[0]["currency"], "USD")
        lines = dict(detail_lines({"securityTrade": {"limitPrice": "200",
            "numberOfShares": {"total": "3", "filled": "0"}, "fee": "0"}, "currency": "USD"}))
        self.assertEqual(lines["Средняя цена исполнения"], "—")
        self.assertEqual(lines["Исполнено"], "0")
        self.assertEqual(lines["Комиссия"], "0 USD")
        self.assertEqual(lines["Лимитная цена"], "200 USD")
