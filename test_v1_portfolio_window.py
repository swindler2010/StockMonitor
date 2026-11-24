"""Tk verification for the standalone Scalable Capital window and its default scope."""
import ast
from pathlib import Path
from tkinter import TclError, Tk, ttk
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch

import v1_portfolio_view as v1


class WindowTest(TestCase):
    def setUp(self):
        try:
            self.root = Tk()
        except TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_portfolio_button_opens_one_dedicated_window(self):
        app = SimpleNamespace(root=self.root)
        window = v1.show(app)
        self.assertIs(v1.show(app), window)
        self.assertIn("Scalable Capital", window.title())
        self.assertFalse(any(isinstance(widget, ttk.Notebook) for widget in window.winfo_children()))
        window.destroy()
        self.assertIsNot(v1.show(app), window)

    def test_sc_positions_are_the_only_default_rows(self):
        dashboard = v1.Dashboard(SimpleNamespace(root=self.root), ttk.Frame(self.root))
        position = dict(isin="US0378331005", name="Apple", quantity=2, value=400, pnl=20, decision="Держать")
        candidate = dict(isin="US5949181045", name="Microsoft", price=350, decision="Ждать")
        report = dict(positions=[position], candidates=[candidate], scenarios=[], current=True,
                      synced_at="2026-11-24T21:00:00+00:00", portfolio_id="test", total=500, cash=100)
        dashboard.render(report, None)
        self.assertEqual(dashboard.rows.get_children(), (position["isin"],))
        self.assertEqual(dashboard.rows["displaycolumns"], dashboard.POSITIONS)
        self.assertIn("100.00 EUR", dashboard.summary.cget("text"))
        dashboard.show_section("candidates")
        self.assertEqual(dashboard.rows.get_children(), (candidate["isin"],))
        self.assertEqual(dashboard.rows["displaycolumns"], dashboard.CANDIDATES)
        dashboard.show_section("positions")
        self.assertEqual(dashboard.rows.get_children(), (position["isin"],))

    def test_missing_sc_data_offers_connection_without_demo_data(self):
        dashboard = v1.Dashboard(SimpleNamespace(root=self.root), ttk.Frame(self.root))
        dashboard.render(None, "SC_AUTH_REQUIRED")
        self.assertEqual(dashboard.rows.get_children(), ())
        self.assertIn("SC", dashboard.sync.cget("text"))
        self.assertTrue(dashboard.analysis.instate(["disabled"]))

    def test_main_portfolio_method_routes_to_sc(self):
        desktop = Path(__file__).resolve().parent / "stock_monitor" / "desktop014.py"
        if not desktop.exists():
            self.skipTest("desktop source not available")
        tree = ast.parse(desktop.read_text(encoding="utf-8"))
        action = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef) and node.name == "show_portfolio")
        runner = {}
        exec(compile(ast.Module(body=[action], type_ignores=[]), str(desktop), "exec"), runner)
        with patch.object(v1, "show", return_value="SC") as show:
            self.assertEqual(runner["show_portfolio"](self.root), "SC")
            show.assert_called_once_with(self.root)


if __name__ == "__main__":
    main()
