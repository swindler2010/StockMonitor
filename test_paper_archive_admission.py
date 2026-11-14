"""Two targeted regressions: weekend admission and recovered-source proof roles."""
import copy
import json
import unittest
import pandas as pd
import test_paper_preparation as fixture
import paper_preparation as prep
import paper_hourly as hourly
import paper_account as paper
from performance_cache import capture_closed_inputs,validate_closed_inputs,InputRevisionChanged


class ArchiveAdmission(unittest.TestCase):
    def setUp(self):
        self.p=fixture.PreparationTests();self.p.setUp();self.f=self.p.f

    def tearDown(self):self.p.tearDown()

    def test_closed_session_admits_but_does_not_execute_and_missing_tail_blocks(self):
        f=self.f
        f.now=pd.Timestamp('2026-11-14T10:00Z').timestamp()
        f.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (f.isin,'gettex_chart','GETTEX','EUR',f.identity['symbol'],'15m',
             '2026-11-13 22:45:00',100,102,99,101,20))
        # The ordinary queue may select a new candidate while the anchor's
        # price is stale because the market is closed.
        f.write(prep.STATE,{})
        f.write(hourly.STATUS+self.p.anchor,dict(core='READY',quote_blocker='PAPER_STALE_QUOTE',at=f.now))
        self.assertEqual(prep.choose(f.db,f.now),f.info)
        r=self.p.refresh();r['quote_blocker']='PAPER_STALE_QUOTE';r.pop('execution_quote')
        specs=[dict(s['spec'],through='2026-11-13T22:00Z') for s in r['hourly']['proof']['series']]
        r['hourly']['proof']=capture_closed_inputs(f.db,specs,r['_input_revisions'])
        original=copy.deepcopy(r)
        # Archive admission cannot accept a missing final session candle.
        f.db.execute("UPDATE series SET ts='2026-11-13 22:30:00' WHERE base_tf='15m'")
        r['_input_revisions']=dict(f.db.execute('SELECT scope,revision FROM input_revisions'))
        with self.assertRaisesRegex(ValueError,'SESSION_TAIL_MISSING'):prep.admit(f.db,f.info,r,f.now)
        f.db.execute("UPDATE series SET ts='2026-11-13 22:45:00' WHERE base_tf='15m'")
        r['_input_revisions']=dict(f.db.execute('SELECT scope,revision FROM input_revisions'))
        cfg=copy.deepcopy(hourly.read(f.db,hourly.CONFIG))
        report=prep.admit(f.db,f.info,r,f.now)
        self.assertEqual(report['admission']['basis'],'CLOSED_SESSION_ARCHIVE')
        self.assertFalse(report['execution_permission'])
        from paper_orders import owner_route,order_choices
        after=hourly.read(f.db,hourly.CONFIG)
        self.assertTrue(owner_route(after,f.isin))
        self.assertIn(f.isin,order_choices(dict(status='ACTIVE',automation=after,allowed_isins=[f.isin])))
        self.assertEqual({k:v for k,v in after.items() if k not in ('names','identities')},
                         {k:v for k,v in cfg.items() if k not in ('names','identities')})
        self.assertEqual(paper.ledger(f.db)['fills'],[])
        with self.assertRaisesRegex(ValueError,'PAPER_SESSION_CLOSED'):paper.validate_session(f.identity,f.now)
        f.cfg=after
        stale=self.p.refresh();stale['hourly'].pop('preparation_only');stale['quote_blocker']='PAPER_STALE_QUOTE'
        stale['execution_context']['trading_engine']['signal']='BUY'
        f.args=f.args_for('BUY')
        with self.assertRaisesRegex(ValueError,'PAPER_STALE_QUOTE'):
            paper.execute(f.db,f.info,stale,f.args,f.now)
        self.assertEqual(paper.ledger(f.db)['fills'],[])
        # The same stale-price exception never permits admission in an open session.
        with self.assertRaisesRegex(ValueError,'REQUIRES_CLOSED_SESSION'):
            prep.closed_session_archive(f.db,f.info,f.identity,original,pd.Timestamp('2026-11-16T10:00Z').timestamp())

    def test_recovery_evidence_is_not_a_second_core_listing_but_remains_strict(self):
        f=self.f;r=f.result
        official=dict(f.identity,source='gettex',symbol=f.isin)
        spec=dict(isin=f.isin,identity=official,base_tf='15m',timeframe='15m',
                  **{'from':'2026-10-01','through':'2026-10-02T00:00Z'})
        f.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',
                     (f.isin,'gettex','15m','2026-10-01 13:30:00',100,102,99,101,20))
        specs=[copy.deepcopy(s['spec']) for s in r['hourly']['proof']['series']]
        specs[0]['recovered_inputs']=[spec]
        r['_input_revisions']=dict(f.db.execute('SELECT scope,revision FROM input_revisions'))
        r['hourly']['proof']=capture_closed_inputs(f.db,specs,r['_input_revisions'])
        hourly.validate_analysis(f.db,f.info,r,f.now,f.cfg)
        self.assertEqual(len(r['hourly']['proof']['series']),4)
        # An unrelated provider, or a recovered input used instead of 1H, fails.
        bad=copy.deepcopy(r);bad['hourly']['proof']['series'][0]['spec']['recovered_inputs']=[]
        with self.assertRaisesRegex(ValueError,'DEPENDENCY_BLOCKED'):
            hourly.validate_analysis(f.db,f.info,bad,f.now,f.cfg)
        bad=copy.deepcopy(r);bad['hourly']['proof']['series'].pop(0)
        with self.assertRaisesRegex(ValueError,'DEPENDENCY_BLOCKED'):
            hourly.validate_analysis(f.db,f.info,bad,f.now,f.cfg)
        # A correction to the official underlying row still revokes the result.
        f.db.execute('UPDATE candles SET close=100.5')
        with self.assertRaises(InputRevisionChanged):validate_closed_inputs(f.db,r['hourly']['proof'])


if __name__=='__main__':unittest.main()
