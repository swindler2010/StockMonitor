import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'stock_monitor'))
from paper_account import initialize, ledger
from paper_hourly import reset
from paper_journal import read, save_note


class PaperJournalTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute('CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT)')
        initialize(self.db)

    def fill(self, name, side, qty, gross, mode='V1', initiator='AUTO', reason=''):
        fill = dict(request_id=name, isin='US0378331005', identity={'symbol': 'AAPL.GTX'},
                    at=f'2026-11-02T12:00:{name}+00:00', timeframe='1H', side=side,
                    quantity=str(qty), gross=str(gross), fee='1.00', initiator=initiator,
                    smart_eligibility={'strategy_mode': mode, 'signal_at': 'test',
                                       'signal_reason': 'Signal', 'protective_exit': {'reason': reason}})
        self.db.execute('INSERT INTO paper_fills VALUES (?,?,?)', (name, 'test', json.dumps(fill)))

    def journal(self):
        state = ledger(self.db)
        output = read(self.db, state['fills'])
        return state, output

    def test_partial_and_manual_sell_attributed_to_entry_and_archived(self):
        self.fill('00', 'BUY', 10, 1000, 'AGGRESSIVE')
        self.fill('01', 'SELL', 4, 440, 'V1', 'MANUAL')
        save_note(self.db, '01', '  Частичный выход  ')
        state, output = self.journal()
        stats = output['totals']['current']['V5']
        self.assertEqual((stats['buys'], stats['sells'], stats['winners'], stats['losers']), (1, 1, 0, 0))
        self.assertEqual(state['realized'], stats['realized'])
        self.assertEqual(output['entries'][0]['note'], 'Частичный выход')
        self.fill('02', 'SELL', 6, 624, 'MOMENTUM', reason='TAKE_PROFIT')
        state, output = self.journal()
        stats = output['totals']['current']['V5']
        self.assertEqual((stats['sells'], stats['winners'], stats['realized'], stats['fees']), (2, 1, '61.00', '3.00'))
        self.assertEqual(output['entries'][0]['reason'], 'TAKE_PROFIT')
        self.assertEqual(state['realized'], stats['realized'])
        reset(self.db, 'test archive')
        _, output = self.journal()
        self.assertEqual(output['totals']['archive']['V5'], stats)
        self.assertEqual(output['totals']['current']['V5']['buys'], 0)
        save_note(self.db, '01', '')
        self.assertEqual(self.journal()[1]['entries'][1]['note'], '')

    def test_mixed_entries_share_existing_average_cost_and_never_mix_account_epochs(self):
        self.fill('00', 'BUY', 5, 500, 'PULLBACK')
        self.fill('01', 'BUY', 5, 525, 'V1', 'CHATGPT')
        self.fill('02', 'SELL', 10, 1010, 'V2')
        state, output = self.journal()
        stats = output['totals']['current']
        self.assertEqual((stats['V3']['realized'], stats['—']['realized']), ('3.50', '-21.50'))
        self.assertEqual(state['realized'], '-18.00')
        self.assertEqual(output['entries'][0]['codes'], ['V3', '—'])
        self.fill('03', 'BUY', 1, 100, 'V1')
        reset(self.db, 'first')
        self.fill('04', 'BUY', 1, 200, 'V2')
        self.fill('05', 'SELL', 1, 201, 'V1')
        reset(self.db, 'second')
        totals = self.journal()[1]['totals']['archive']
        self.assertEqual((totals['V1']['sells'], totals['V2']['realized']), (0, '-1.00'))
        with self.assertRaises(ValueError):
            save_note(self.db, '09', 'unknown')

    def test_proportional_partial_sales_close_both_strategy_legs(self):
        self.fill('00', 'BUY', 1, 100, 'V2')
        self.fill('01', 'BUY', 2, 190, 'V1')
        self.fill('02', 'SELL', 1, 101)
        self.fill('03', 'SELL', 1, 102)
        self.fill('04', 'SELL', 1, 103)
        self.fill('05', 'BUY', 1, 112, 'V1')
        self.fill('06', 'SELL', 1, 100)
        state, output = self.journal()
        totals = output['totals']['current']
        self.assertEqual((totals['V1']['winners'], totals['V1']['losers']), (1, 1))
        self.assertEqual((totals['V2']['winners'], totals['V2']['flat']), (0, 1))
        self.assertEqual(output['entries'][0]['codes'], ['V1'])
        self.assertEqual(state['realized'], '-3.00')


if __name__ == '__main__':
    unittest.main()
