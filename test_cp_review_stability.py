"""Exact review dependencies; no network, market writes or trading operations."""
import copy, json, sqlite3, sys, unittest
from contextlib import contextmanager
from unittest.mock import patch
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).parent / 'stock_monitor'))
import cp_reconciliation as rv
import cp_controls as ctl


class ReviewStability(unittest.TestCase):
    def setUp(self):
        self.meta = dict(source='gettex_chart', venue='GETTEX', currency='EUR',
                         symbol='AAPL.GTX', timezone='Europe/Berlin')
        self.inputs = dict(info=dict(isin='US0378331005', name='Apple', levels=[],
            indicators={'ema10': True}, drawings=[], position=0), listing=self.meta,
            rows=[], neighbors={'before': ['2026-11-10 10:00:00',311.55,311.75,311.5,311.65,38],
                                'after': ['2026-11-10 10:30:00',311.7,311.7,311.35,311.4,14]},
            proof={'checked_at': '2026-11-10T11:00Z', 'no_trades': []})

    def test_display_settings_do_not_invalidate_exact_market_review(self):
        before = rv._fingerprint(self.inputs)
        for field, value in [('levels',[123]),('drawings',[{'line':1}]),
                             ('indicators',{'ema10':False}),('position',2),('name','Apple Inc.')]:
            changed = copy.deepcopy(self.inputs); changed['info'][field] = value
            with self.subTest(field=field):self.assertEqual(before,rv._fingerprint(changed))

    def test_native_changes_identity_and_evidence_still_invalidate(self):
        before=rv._fingerprint(self.inputs)
        changed=[]
        for field,value in [('currency','USD'),('venue','XETRA'),('source','proxy'),('symbol','APC.DE')]:
            x=copy.deepcopy(self.inputs);x['listing'][field]=value;changed.append(x)
        x=copy.deepcopy(self.inputs);x['info']['isin']='US67066G1040';changed.append(x)
        x=copy.deepcopy(self.inputs);x['neighbors']['after'][-1]=15;changed.append(x)
        x=copy.deepcopy(self.inputs);x['rows']=[x['neighbors']['before']];changed.append(x)
        x=copy.deepcopy(self.inputs);x['proof']['trade_intervals']=[['10:15','10:30']];changed.append(x)
        for x in changed:self.assertNotEqual(before,rv._fingerprint(x))

    def test_legacy_v3_migrates_only_when_exact_old_binding_is_proven(self):
        x=copy.deepcopy(self.inputs);x['fingerprint']=rv._fingerprint(x)
        old=rv._fingerprint(x, version=3)
        job={'fingerprint_version':3,'observation':{'fingerprint_version':3,'input_fingerprint':old}}
        self.assertTrue(rv._observation_current(job,x))
        x['neighbors']['after'][-1]=99;x['fingerprint']=rv._fingerprint(x)
        self.assertFalse(rv._observation_current(job,x))

    def test_legacy_already_stale_review_is_not_revived(self):
        old=rv._fingerprint(self.inputs,version=3)
        x=copy.deepcopy(self.inputs);x['info']['levels']=[123];x['fingerprint']=rv._fingerprint(x)
        job={'fingerprint_version':3,'observation':{'fingerprint_version':3,'input_fingerprint':old}}
        self.assertFalse(rv._observation_current(job,x))

    def test_tv_grant_binding_ignores_drawings_but_not_native_rows(self):
        job=rv._spec('US0378331005',self.meta,'15m','2026-11-10 10:15:00','2026-11-10 10:15:00')
        job['observation']={'result':'ABSENT','at':'2026-11-10T11:00Z'}
        before=ctl._tv_native_binding(job,self.inputs)
        x=copy.deepcopy(self.inputs);x['info']['levels']=[123]
        self.assertEqual(before,ctl._tv_native_binding(job,x))
        x['neighbors']['before'][-1]=99
        self.assertNotEqual(before,ctl._tv_native_binding(job,x))

    def test_legacy_retained_binding_requires_exact_pre_migration_inputs(self):
        j,x=self.reviewed()
        old=ctl._tv_native_binding(j,x,version=1)
        self.assertNotEqual(old,ctl._tv_native_binding(j,x))
        self.assertEqual(old,ctl._tv_native_binding(j,copy.deepcopy(x),version=1))
        x['neighbors']['after'][-1]=999
        self.assertNotEqual(old,ctl._tv_native_binding(j,x,version=1))

    def reviewed(self, result='ABSENT'):
        j=rv._spec('US0378331005',self.meta,'15m','2026-11-10 10:15:00','2026-11-10 10:15:00')
        x=copy.deepcopy(self.inputs);x['fingerprint']=rv._fingerprint(x)
        j['observation']=dict(result=result,at='2026-11-10T11:00:00Z',identity_confirmed=True,
                             input_fingerprint=x['fingerprint'],fingerprint_version=4)
        return j,x

    def test_matching_absence_is_diagnostic_not_ohlcv_or_no_trades(self):
        j,x=self.reviewed();finding={'state':'UNCONFIRMED'}
        c=rv._comparison(j,x,finding,pd.Timestamp('2026-11-10T12:00Z'))
        self.assertTrue(c['presence_match']);self.assertIsNone(c['ohlcv_match'])
        self.assertEqual(c['native_result'],'UNCONFIRMED');self.assertEqual(x['rows'],[])

    def test_present_is_not_claimed_equal_and_changed_input_is_not_counted(self):
        j,x=self.reviewed('PRESENT');t=pd.Timestamp('2026-11-10T12:00Z')
        c=rv._comparison(j,x,{'state':'UNCONFIRMED'},t)
        self.assertFalse(c['presence_match']);self.assertIsNone(c['ohlcv_match'])
        x['neighbors']['after'][-1]=999;x['fingerprint']=rv._fingerprint(x)
        c=rv._comparison(j,x,{'state':'UNCONFIRMED'},t)
        self.assertFalse(c['observation_current']);self.assertIsNone(c['presence_match'])

    def test_open_and_multibar_ranges_do_not_become_single_closed_samples(self):
        j,x=self.reviewed();f={'state':'UNCONFIRMED'}
        self.assertIsNone(rv._comparison(j,x,f,pd.Timestamp('2026-11-10T09:20Z'))['presence_match'])
        j['last']='2026-11-10 10:30:00'
        self.assertIsNone(rv._comparison(j,x,f,pd.Timestamp('2026-11-10T12:00Z'))['presence_match'])

    def test_statistics_deduplicate_and_keep_isin_source_tf_separate(self):
        j,x=self.reviewed();c=rv._comparison(j,x,{'state':'UNCONFIRMED'},pd.Timestamp('2026-11-10T12:00Z'))
        s=rv._comparison_summary([c,copy.deepcopy(c)])[0]
        self.assertEqual((s['reviewed_scopes'],s['presence_compared'],s['presence_agreement_percent']),(1,1,100))
        self.assertIsNone(s['ohlcv_agreement_percent'])
        others=[dict(c,tf='5m'),dict(c,isin='US67066G1040'),dict(c,identity=dict(c['identity'],source='gettex'))]
        self.assertEqual(len(rv._comparison_summary([c,*others])),4)

    def test_calendar_month_recheck_persists_without_grant_or_reference_extension(self):
        j,x=self.reviewed();f={'state':'UNCONFIRMED'};original=copy.deepcopy(j['observation'])
        first=rv._monthly_review(j,f,pd.Timestamp('2026-12-09T12:00Z'))
        self.assertEqual(first['external_state'],'NOT_DUE');self.assertNotIn('native_checks',first)
        j['monthly_review']=first
        due=rv._monthly_review(j,f,pd.Timestamp('2026-12-10T11:00Z'))
        self.assertEqual(due['external_state'],'ASSISTED_RECHECK_REQUIRED');self.assertEqual(due['native_checks'],1)
        j['monthly_review']=json.loads(json.dumps(due))
        self.assertEqual(rv._monthly_review(j,f,pd.Timestamp('2026-12-10T12:00Z')),due)
        self.assertEqual(j['observation'],original)

    def test_calendar_month_uses_month_end_and_ui_keeps_unknown_explicit(self):
        from candlepilot_health import review_comparison_text
        j,x=self.reviewed();j['observation']['at']='2027-01-31T11:00:00Z'
        monthly=rv._monthly_review(j,{'state':'UNCONFIRMED'},pd.Timestamp('2027-02-27T11:00Z'))
        self.assertEqual(monthly['next_native_check_at'],'2027-02-28T11:00:00+00:00')
        j['monthly_review']=monthly;j['source_comparison']={'presence_match':None,'native_checked_at':'test'}
        text=review_comparison_text(j)
        self.assertIn('не измерено',text);self.assertIn('2027-02-28',text)

    def test_v2_binding_migration_keeps_old_evidence_timestamp_guard(self):
        j,x=self.reviewed();old=copy.deepcopy(x);old['proof']['checked_at']='2026-11-09T11:00Z'
        j['fingerprint_version']=2
        j['observation'].update(fingerprint_version=2,evidence_checked_at=old['proof']['checked_at'],
            input_fingerprint=rv._fingerprint(old,legacy=True,version=3))
        self.assertTrue(rv._observation_current(j,x))
        x['proof']['trade_intervals']=[['10:15','10:30']];x['fingerprint']=rv._fingerprint(x)
        self.assertFalse(rv._observation_current(j,x))


class MemoryStore:
    def __init__(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT); CREATE TABLE candles(isin TEXT,source TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL);')
    @contextmanager
    def connect(self):
        with self.db:yield self.db


class NormalTurn(unittest.TestCase):
    def setUp(self):
        self.s=MemoryStore();self.addCleanup(self.s.db.close)
        self.i={'isin':'US0378331005','name':'Apple','levels':[]}
        self.meta=dict(source='gettex',symbol=self.i['isin'],venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
        self.t=pd.Timestamp('2026-11-10T12:00Z')
        self.patch=patch('gettex_history.attach_known_interruptions',return_value=False);self.patch.start();self.addCleanup(self.patch.stop)
        with self.s.connect() as d:
            d.execute('INSERT INTO instruments VALUES (?,?)',(self.i['isin'],json.dumps(self.i)))
            spec=rv._spec(self.i['isin'],self.meta,'15m','2026-11-10 10:15:00','2026-11-10 10:15:00')
            j,_=rv._enqueue(d,spec,{'kind':'test'},self.t)
        rv.turn(self.s,self.i,self.t)
        with self.s.connect() as d:
            j=ctl.read(d,rv.key(spec));x=rv._input(d,j)
            old=rv._fingerprint(x,version=3)
            j.update(fingerprint_version=3,input_fingerprint=old)
            j['observation']=dict(result='ABSENT',tv_symbol='GETTEX:APC',identity_confirmed=True,at=self.t.isoformat(),input_fingerprint=old,fingerprint_version=3)
            ctl.write(d,rv.key(j),j)
        self.key=rv.key(j)

    def test_normal_turn_migrates_and_readonly_health_reports_once(self):
        result=rv.turn(self.s,self.i,self.t)
        self.assertEqual(result['checked'],1)
        with self.s.connect() as d:j=ctl.read(d,self.key)
        self.assertEqual(j['observation']['fingerprint_version'],4)
        self.assertTrue(j['finding']['observation_current'])
        self.i['levels']=[321]
        with self.s.connect() as d:d.execute('UPDATE instruments SET payload=?',(json.dumps(self.i),))
        rv.turn(self.s,self.i,self.t+pd.Timedelta(hours=2))
        with self.s.connect() as d:
            j=ctl.read(d,self.key);self.assertTrue(j['finding']['observation_current'])
            d.execute('PRAGMA query_only=ON');summary=rv.collect(d,self.i['isin'])
        self.assertEqual(summary['source_comparisons'][0]['reviewed_scopes'],1)
        self.assertEqual(summary['source_comparisons'][0]['presence_agreement_percent'],100)

    def test_native_revision_during_calculation_cannot_publish_receipt(self):
        original=rv._classify
        def race(j,x,t):
            answer=original(j,x,t)
            with self.s.connect() as d:d.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.i['isin'],'gettex','15m','2026-11-10 10:15:00',1,1,1,1,1))
            return answer
        with patch.object(rv,'_classify',race):result=rv.turn(self.s,self.i,self.t)
        self.assertEqual(result['checked'],0)
        with self.s.connect() as d:j=ctl.read(d,self.key)
        self.assertEqual(j['observation']['fingerprint_version'],3)
        self.assertNotIn('source_comparison',j)

    def risk_fixture(self, enabled=True):
        with self.s.connect() as d:
            j=ctl.read(d,self.key);x=rv._input(d,j)
            # Emulate a previously admitted exact native binding after a
            # harmless evidence refresh, the existing retention policy.
            j['observation']['input_fingerprint']='old-proof';ctl.write(d,self.key,j)
            p=dict(id='scope',kind='archive',identity=self.meta,tf='15m',range=[j['first'],j['last']],
                   state='UNCONFIRMED',report_key='data_quality:test')
            ctl.write(d,ctl.TV_POLICY,dict(enabled=enabled,approved=True,version=ctl.TV_VERSION))
            ctl.write(d,'cp_manual_risk:'+self.i['isin'],dict(accepted={'scope':dict(actor=ctl.TV_ACTOR,problem=p,
                evidence=dict(native_binding=ctl._tv_native_binding(j,x,version=1),job_ids=[j['id']]))}))
        return p

    def test_existing_legacy_grant_survives_migration_not_changed_market_data(self):
        p=self.risk_fixture()
        with patch.object(ctl,'problems',return_value=[p]),patch.object(ctl,'_revisions',return_value={}):
            self.assertEqual(ctl.tv_risk_turn(self.s,self.i,self.t)['accepted'],1)
            self.i['levels']=[456]
            with self.s.connect() as d:d.execute('UPDATE instruments SET payload=?',(json.dumps(self.i),))
            self.assertEqual(ctl.tv_risk_turn(self.s,self.i,self.t)['accepted'],1)
            with self.s.connect() as d:d.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.i['isin'],'gettex','15m','2026-11-10 10:00:00',1,1,1,1,1))
            self.assertEqual(ctl.tv_risk_turn(self.s,self.i,self.t)['accepted'],0)

    def test_owner_disabled_or_invalid_scope_cannot_be_admitted(self):
        p=self.risk_fixture(False)
        self.assertEqual(ctl.tv_risk_turn(self.s,self.i,self.t)['state'],'DISABLED')
        p=self.risk_fixture();p['state']='INVALID'
        with patch.object(ctl,'problems',return_value=[p]),patch.object(ctl,'_revisions',return_value={}):
            self.assertEqual(ctl.tv_risk_turn(self.s,self.i,self.t)['accepted'],0)

if __name__=='__main__':unittest.main()
