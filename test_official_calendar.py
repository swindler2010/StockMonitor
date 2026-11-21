import copy
from datetime import date, datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent/'stock_monitor'))
from official_calendar import parse, refresh, upcoming, SETTING
from news_runtime import NewsRuntime


def fed():
    return ('<h4>2026 FOMC Meetings</h4>'+''.join('<div class="fomc-meeting__month">'+m+'</div><div class="fomc-meeting__date">'+d+'</div>'
            for m, d in [('January', '27-28'), ('March', '17-18*'), ('September', '15-16*'), ('December', '8-9*')])).encode()


class CalendarTests(unittest.TestCase):
    def test_ecb_dates_and_localized_kind_retain_original_without_invented_time(self):
        rows = parse('ECB', b'<dl><dt>17/12/2026</dt><dd>Governing Council: monetary policy (Day 2), followed by press conference<br></dd></dl>')
        self.assertEqual(rows[0]['date'], '2026-12-17'); self.assertEqual(rows[0]['precision'], 'DATE')
        self.assertEqual(rows[0]['kind'], 'MONETARY_DAY2'); self.assertNotIn('time', rows[0])
        self.assertIn('press conference', rows[0]['original'])

    def test_fed_year_sections_ranges_and_old_exception_are_not_mixed(self):
        html = fed()+b'<h4>2025 FOMC Meetings</h4><div class="fomc-meeting__month">August</div><div class="fomc-meeting__date">22 (notation vote)</div>'
        rows = parse('FED', html, year_floor=2026)
        self.assertEqual(len(rows), 4); self.assertEqual(rows[-1]['end_date'], '2026-12-09')
        self.assertEqual(rows[-1]['precision'], 'DATE_RANGE')

    def test_unknown_format_is_an_error_never_a_confirmed_empty_calendar(self):
        for source in ('ECB', 'FED'):
            with self.assertRaises(ValueError): parse(source, b'<html>Maintenance</html>')

    def test_failed_provider_keeps_own_stale_rows_other_source_updates(self):
        old = refresh({}, lambda url: fed() if 'federalreserve' in url else b'<dt>25/11/2026</dt><dd>General Council meeting</dd>')
        def fetch(url):
            if 'ecb' in url: raise RuntimeError('offline')
            return fed()
        new = refresh(old, fetch)
        self.assertEqual(new['sources']['ECB']['status'], 'STALE_CACHE')
        self.assertEqual(new['sources']['ECB']['rows'], old['sources']['ECB']['rows'])
        self.assertEqual(new['sources']['FED']['status'], 'AVAILABLE')
        self.assertEqual(old['sources']['ECB']['status'], 'AVAILABLE')

    def test_upcoming_is_bounded_and_preserves_in_progress_range(self):
        value = refresh({}, lambda url: fed() if 'federalreserve' in url else b'<dt>25/11/2026</dt><dd>General Council meeting</dd>')
        rows = upcoming(value, today=date(2026, 12, 9), limit=2)
        self.assertEqual(rows[0]['date'], '2026-12-08')
        self.assertEqual(len(rows), 1)

    def test_runtime_uses_same_misc_lane_persists_ui_namespace_and_releases_busy(self):
        writes, pending, seen = [], [], []
        store = SimpleNamespace(directory=Path('.'), setting=lambda k, d: {}, set_setting=lambda k, v: writes.append(k))
        app = SimpleNamespace(store=store, closing=False, _calendar_listener=lambda value: seen.append(value),
                              job=lambda work, done, failed, **kwargs: pending.append((work, done, failed, kwargs)) or True)
        runtime = NewsRuntime(app); runtime.client = lambda url: fed() if 'federalreserve' in url else b'<dt>25/11/2026</dt><dd>General Council meeting</dd>'
        self.assertTrue(runtime.refresh_calendar()); self.assertFalse(runtime.refresh_calendar())
        work, done, _, meta = pending.pop(); self.assertEqual(meta['lane'], 'misc')
        result = work(); app.closing = True; done(result)
        self.assertEqual(writes, [SETTING]); self.assertFalse(runtime.busy); self.assertFalse(seen)

    def test_closed_panel_rejects_late_reply_and_translates_event_labels(self):
        from calendar_panel import Panel, title, dates
        panel = Panel.__new__(Panel); panel.closed = True; panel.app = SimpleNamespace(closing=False)
        panel.receive({'sources': {}})  # no widget access after close
        row = parse('FED', fed(), year_floor=2026)[-1]
        with patch('calendar_panel.language', return_value='de'):
            self.assertIn('Sitzung', title(row)); self.assertIn('08.12.2026', dates(row))
        with patch('calendar_panel.language', return_value='en'):
            self.assertIn('meeting', title(row)); self.assertIn('2026-12-08', dates(row))


    def test_real_calendar_widgets_use_all_three_selected_languages(self):
        import tkinter as tk
        from calendar_panel import Panel
        from main_screen import theme
        from themes import THEMES
        snapshot = refresh({}, lambda url: fed() if 'federalreserve' in url else b'<dt>25/11/2026</dt><dd>General Council meeting</dd>')
        for code, word in [('ru', 'Календарь'), ('de', 'Kalender'), ('en', 'calendar')]:
            with patch('calendar_panel.language', return_value=code):
                root = tk.Tk(); root.withdraw()
                try:
                    app = SimpleNamespace(root=root, closing=False, store=SimpleNamespace(setting=lambda k, d: snapshot if k == SETTING else d),
                        job=lambda work, done, failed, **kwargs: done(work()) or True)
                    theme(app, THEMES['Тёмная'])
                    from tkinter import ttk
                    host = ttk.Frame(root); host.pack()
                    panel = Panel(app, host); panel.show(); root.update()
                    self.assertIn(word.lower(), panel.window.title().lower())
                    self.assertGreater(len(panel.table.get_children()), 0)
                    panel.cleanup()
                finally: root.destroy()


if __name__ == '__main__': unittest.main()
