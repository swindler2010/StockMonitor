"""Sequential admission and negative execution gates; RAM fixtures only."""
import copy
import json
import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import test_paper_hourly as fixture
import paper_preparation as prep


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.HourlyTests();self.f.setUp();f=self.f
        self.anchor='US67066G1040'
        meta=dict(f.identity,symbol='NVDA.GTX')
        f.cfg['identities']={self.anchor:meta}
        f.cfg['automatic_admission']=dict(version=prep.VERSION,approved=True,enabled=True,
            scope='registered_eur_stocks',prerequisites=[self.anchor],priority=[f.isin])
        f.write(fixture.hourly.CONFIG,f.cfg)
        self.status=dict(core='READY',quote_blocker=None,quote_at='2026-11-04T09:30Z',at=f.now)
        f.write(fixture.hourly.STATUS+self.anchor,self.status)
        f.db.execute('INSERT INTO instruments VALUES (?,?)',(self.anchor,json.dumps(dict(isin=self.anchor,name='NVIDIA',kind='stock'))))
        self.assertEqual(prep.choose(f.db,f.now),f.info)
        self.refresh()

    def tearDown(self):self.f.db.close()

    def refresh(self):
        f=self.f;f.result=f.result_for('WAIT');f.result['hourly']['preparation_only']=True
        return f.result

    def admit(self):return prep.admit(self.f.db,self.f.info,self.f.result,self.f.now)

    def assert_blocked(self,pattern):
        with self.assertRaisesRegex(ValueError,pattern):self.admit()
        self.assertNotIn(self.f.isin,fixture.hourly.read(self.f.db,fixture.hourly.CONFIG)['identities'])
        self.assertEqual(fixture.paper.ledger(self.f.db)['fills'],[])

    def test_ready_wait_promotes_without_order_and_preserves_strategy(self):
        f=self.f;before=copy.deepcopy(f.cfg);report=self.admit()
        self.assertEqual(report['state'],'ADMITTED');self.assertFalse(report['execution_permission'])
        after=fixture.hourly.read(f.db,fixture.hourly.CONFIG)
        self.assertEqual(after['identities'][f.isin],f.identity)
        for k in before:
            if k!='identities':self.assertEqual(after[k],before[k])
        self.assertEqual(fixture.paper.ledger(f.db)['fills'],[])
        self.assertIsNone(prep.maintenance(f.db))
        with self.assertRaisesRegex(ValueError,'PREPARATION_CANNOT_EXECUTE'):
            fixture.hourly.validate(f.db,f.info,f.result,f.args,f.now)

    def test_candidate_analysis_authority_is_never_execution_authority(self):
        f=self.f
        fixture.hourly.authorize_hourly(f.db,f.isin,f.identity)
        with self.assertRaisesRegex(ValueError,'NOT_AUTHORIZED'):fixture.hourly.authority(f.db,f.isin)
        with self.assertRaisesRegex(ValueError,'NOT_AUTHORIZED'):f.trade()
        self.assertEqual(prep.maintenance(f.db),(f.isin,f.identity))

    def test_dq_seed_benchmark_warmup_and_cross_tf_identity_stay_blocking(self):
        f=self.f;original=copy.deepcopy(f.result)
        mutations=[('ENGINE_DATA_BLOCKED',lambda r:r['execution_context']['trading_engine'].update(status='DATA BLOCKED')),
            ('DQ_BLOCKED',lambda r:r['execution_context']['trading_engine']['timeframe_context']['4H']['data_quality'].update(state='UNCONFIRMED')),
            ('SEED_BLOCKED',lambda r:r['execution_context']['trading_engine'].update(demo_initialization={'consistent':False})),
            ('BENCHMARK_BLOCKED',lambda r:r['execution_context']['trading_engine'].update(benchmark_context={})),
            ('DEPENDENCY_BLOCKED',lambda r:r['hourly']['proof']['series'][0].update(count=34)),
            ('DEPENDENCY_BLOCKED',lambda r:r['hourly']['proof']['series'][0]['spec']['identity'].update(venue='FRA'))]
        for code,change in mutations:
            f.result=copy.deepcopy(original);change(f.result);self.assert_blocked(code)

    def test_stale_invalid_unclosed_wrong_quote_is_not_waivable(self):
        f=self.f;original=copy.deepcopy(f.result)
        mutations=[lambda r:r.update(quote_blocker='PAPER_STALE_QUOTE'),
            lambda r:r['execution_quote']['bars'][0].update(close_at='2026-11-04T08:00Z'),
            lambda r:r['execution_quote']['bars'][0].update(volume=0),
            lambda r:r['execution_quote']['bars'][0].update(closed=False),
            lambda r:r['execution_quote']['identity'].update(currency='USD'),
            lambda r:r['execution_quote']['data_quality'].update(state='INVALID')]
        for change in mutations:
            f.result=copy.deepcopy(original);change(f.result);self.assert_blocked('PAPER_')

    def test_revision_policy_identity_and_epoch_changes_revoke_preparation(self):
        f=self.f
        f.db.execute("UPDATE series SET close=100 WHERE base_tf='1H'")
        self.assert_blocked('STALE_INPUTS')
        self.refresh();f.cfg['automatic_admission']['enabled']=False;f.write(fixture.hourly.CONFIG,f.cfg)
        self.assert_blocked('NOT_AUTHORIZED');self.assertIsNone(prep.maintenance(f.db))
        f.cfg['automatic_admission']['enabled']=True;f.write(fixture.hourly.CONFIG,f.cfg)
        self.refresh();f.write(fixture.hourly.EPOCH,'reset-after-calculation')
        self.assert_blocked('NOT_AUTHORIZED')

    def test_missing_listing_and_wrong_currency_are_rejected(self):
        f=self.f;old=fixture.hourly.read(f.db,prep.STATE)
        for key,value in [('venue','FRA'),('currency','USD'),('source','proxy')]:
            bad=copy.deepcopy(old);bad['identity'][key]=value;f.write(prep.STATE,bad)
            self.assert_blocked('IDENTITY_BLOCKED')
        f.write(prep.STATE,old);f.db.execute('DELETE FROM settings WHERE key=?',(f.proof_setting,))
        self.assert_blocked('LISTING_UNCONFIRMED')

    def test_no_new_manual_risk_grants_are_created(self):
        f=self.f;f.result['execution_context']['status']='MANUAL_RISK_ACCEPTED'
        self.assert_blocked('MANUAL_NOT_GRANTED')
        self.assertIsNone(fixture.hourly.read(f.db,'cp_manual_risk:'+f.isin))

    def test_context_expiry_missing_revisions_or_wrong_instrument_block(self):
        f=self.f;original=copy.deepcopy(f.result)
        f.result['hourly']['expires_at']='2026-11-04T09:34Z';self.assert_blocked('STALE_WINDOW')
        f.result=copy.deepcopy(original);f.result['_input_revisions'].pop('setting:'+prep.STATE)
        self.assert_blocked('REVISIONS_REQUIRED')
        f.result=copy.deepcopy(original);f.result['isin']=self.anchor;self.assert_blocked('CONTEXT_CHANGED')

    def test_restart_preserves_single_candidate_cooldown_and_receipt(self):
        f=self.f;clone=sqlite3.connect(':memory:');f.db.commit();f.db.backup(clone)
        try:
            self.assertIsNone(prep.choose(clone,f.now+10))
            self.assertEqual(prep.choose(clone,f.now+301)['isin'],f.isin)
            self.assertEqual(fixture.hourly.read(clone,prep.STATE)['requested_at'],f.now)
        finally:clone.close()
        self.admit();clone=sqlite3.connect(':memory:');f.db.commit();f.db.backup(clone)
        try:
            self.assertEqual(fixture.hourly.read(clone,prep.RECEIPT+f.isin)['state'],'ADMITTED')
            self.assertIsNone(prep.choose(clone,f.now+10))
            self.assertEqual(fixture.paper.ledger(clone)['fills'],[])
        finally:clone.close()

    def test_fresh_pair_required_to_start_but_waiting_candidate_continues(self):
        f=self.f
        f.write(fixture.hourly.STATUS+self.anchor,dict(self.status,core='DATA BLOCKED'))
        self.assertEqual(prep.choose(f.db,f.now+301)['isin'],f.isin)
        f.db.execute('DELETE FROM settings WHERE key=?',(prep.STATE,))
        self.assertIsNone(prep.choose(f.db,f.now+601))

    def test_53_200_instruments_select_one_and_maintenance_has_no_execution(self):
        f=self.f
        for n in range(199):
            isin='XX'+str(n).zfill(10)
            f.db.execute('INSERT INTO instruments VALUES (?,?)',(isin,json.dumps(dict(isin=isin,kind='stock',name=isin))))
        for size in (53,200):
            @contextmanager
            def connect():yield f.db
            from candle_schedule import trade_maintenance
            infos=[f.info]+[dict(isin='XX'+str(n).zfill(10),kind='stock') for n in range(size-1)]
            got=trade_maintenance(SimpleNamespace(connect=connect),infos,fixture.pd.Timestamp(f.now,unit='s',tz='UTC'))
            self.assertEqual(set(got),{f.isin});self.assertFalse(got[f.isin][0]['execution_permission'])
        f.db.execute('DELETE FROM settings WHERE key=?',(prep.STATE,))
        self.assertEqual(prep.choose(f.db,f.now)['isin'],f.isin)

    def test_runtime_uses_idle_existing_lane_without_starving_pair(self):
        f=self.f;calls=[];statuses={fixture.hourly.STATUS+self.anchor:{'next_check':f.now+100}}
        store=SimpleNamespace(setting=lambda k,d=None:f.cfg if k==fixture.hourly.CONFIG else statuses.get(k,d))
        app=SimpleNamespace(store=store,closing=False,user_dirty=False,watchlist=[dict(isin=self.anchor)],
            root=SimpleNamespace(after=lambda *a:None))
        def job(work,done,failed=None,**kwargs):
            calls.append(kwargs);done(work());return True
        app.job=job
        with patch.object(prep,'turn',return_value=None) as turn, patch.object(fixture.hourly.time,'time',return_value=f.now):
            runtime=fixture.hourly.Runtime(app)
            for _ in range(30):runtime.tick()
            self.assertEqual(turn.call_count,1)
        self.assertEqual(calls,[dict(lane='trading',key='paper_preparation')])


if __name__=='__main__':unittest.main()
