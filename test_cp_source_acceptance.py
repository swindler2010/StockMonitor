"""Owner source approval stays advisory; no live store or network access."""
import copy,json,unittest
from unittest.mock import patch
from test_cp_review_stability import NormalTurn,MemoryStore,pd,rv,ctl

POLICY=dict(version=1,approved=True,source='gettex',review_days=7)

class SourceAcceptance(unittest.TestCase):
    def setUp(self):
        self.base=NormalTurn();self.base.setUp();self.addCleanup(self.base.doCleanups)
        self.s=self.base.s;self.i=self.base.i;self.t=self.base.t;self.key=self.base.key
        with self.s.connect() as db:
            db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.i['isin'],'gettex','15m','2026-11-10 10:15:00',300,301,299,300.5,10))
            j=ctl.read(db,self.key);x=rv._input(db,j)
            j.update(fingerprint_version=4,input_fingerprint=x['fingerprint'],status='RECHECK_QUEUED',next_check=self.t.isoformat())
            j['observation'].update(result='DIFFERENT',input_fingerprint=x['fingerprint'],fingerprint_version=4)
            ctl.write(db,self.key,j);ctl.write(db,rv.SOURCE_POLICY,POLICY)
        self.job=j;self.inputs=x

    def test_normal_turn_marks_valid_official_difference_weekly_without_grant(self):
        self.assertEqual(rv.turn(self.s,self.i,self.t)['checked'],1)
        with self.s.connect() as db:
            j=ctl.read(db,self.key)
            self.assertEqual(j['status'],'REVIEW_WEEKLY')
            self.assertEqual(j['finding']['state'],'SOURCE_DISCREPANCY')
            self.assertEqual(j['finding']['native_dq'],'READY')
            self.assertFalse(j['source_disposition']['trading_permission'])
            self.assertIsNone(ctl.read(db,'cp_manual_risk:'+self.i['isin'],None))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candles').fetchone()[0],1)
            db.execute('PRAGMA query_only=ON');r=rv.collect(db,self.i['isin'])
        self.assertEqual(r['source_admission'],POLICY)
        self.assertTrue(r['source_comparisons'][0]['value_discrepancy_scopes'])

    def test_invalid_gap_incomplete_and_stale_observation_not_accepted(self):
        valid=dict(state='SOURCE_DISCREPANCY',native_dq='READY',native_complete=True,observation_current=True)
        for patching in (dict(state='INVALID'),dict(native_dq='UNCONFIRMED'),dict(native_complete=False),dict(observation_current=False)):
            self.assertIsNone(rv._source_disposition(self.job,{**valid,**patching},POLICY))
        with self.s.connect() as db:db.execute('UPDATE candles SET high=1')
        rv.turn(self.s,self.i,self.t)
        with self.s.connect() as db:j=ctl.read(db,self.key)
        self.assertNotEqual(j['status'],'REVIEW_WEEKLY');self.assertEqual(j['finding']['native_dq'],'INVALID')

    def test_lseg_wrong_currency_venue_symbol_and_disabled_not_admitted(self):
        f=dict(state='SOURCE_DISCREPANCY',native_dq='READY',native_complete=True,observation_current=True)
        for changes in (dict(source='gettex_chart'),dict(venue='XETRA'),dict(currency='USD'),dict(symbol='OTHER')):
            j=copy.deepcopy(self.job);j['identity'].update(changes)
            self.assertIsNone(rv._source_disposition(j,f,POLICY))
        self.assertIsNone(rv._source_disposition(self.job,f,{**POLICY,'approved':False}))

    def test_changed_policy_during_calculation_cannot_publish_acceptance(self):
        original=rv._classify
        def race(*args):
            result=original(*args)
            with self.s.connect() as db:ctl.write(db,rv.SOURCE_POLICY,{**POLICY,'approved':False})
            return result
        with patch.object(rv,'_classify',side_effect=race):self.assertEqual(rv.turn(self.s,self.i,self.t)['checked'],0)
        with self.s.connect() as db:self.assertIsNone(ctl.read(db,self.key).get('source_disposition'))

    def test_revocation_removes_advisory_acceptance(self):
        rv.turn(self.s,self.i,self.t)
        with self.s.connect() as db:ctl.write(db,rv.SOURCE_POLICY,{**POLICY,'approved':False})
        rv.turn(self.s,self.i,self.t+pd.Timedelta(hours=2))
        with self.s.connect() as db:j=ctl.read(db,self.key)
        self.assertEqual(j['status'],'AWAITING_SOURCE');self.assertIsNone(j['source_disposition'])

    def test_weekly_migration_persists_and_does_not_extend_observation(self):
        from candlepilot_health import review_comparison_text
        f={'state':'SOURCE_DISCREPANCY'}
        j=copy.deepcopy(self.job);j['monthly_review']=rv._monthly_review(j,f,self.t)
        original=copy.deepcopy(j['observation'])
        r=rv._monthly_review(j,f,self.t,POLICY)
        self.assertEqual(r['cadence_days'],7)
        self.assertEqual(pd.Timestamp(r['external_due_at']),self.t+pd.DateOffset(weeks=1))
        j['monthly_review']=json.loads(json.dumps(r))
        due=rv._monthly_review(j,f,self.t+pd.DateOffset(weeks=1),POLICY)
        self.assertEqual(due['external_state'],'ASSISTED_RECHECK_REQUIRED');self.assertEqual(due['native_checks'],1)
        j['monthly_review']=json.loads(json.dumps(due));j['source_comparison']={'native_checked_at':'test'}
        self.assertEqual(rv._monthly_review(j,f,self.t+pd.DateOffset(weeks=1,hours=1),POLICY),due)
        self.assertEqual(j['observation'],original)
        self.assertIn('Еженедельная сверка Work №1',review_comparison_text(j))

    def test_explicit_review_beyond_page_is_served_with_background_53_200(self):
        for size in (53,200):
            with self.subTest(size=size):
                with self.s.connect() as db:
                    db.execute('DELETE FROM settings WHERE key LIKE ?',(rv.PREFIX+'%',))
                    base=copy.deepcopy(self.job);base.update(id='f'*64,status='RECHECK_QUEUED')
                    ctl.write(db,rv.key(base),base)
                    for n in range(size):
                        ordinary=copy.deepcopy(base);ordinary.update(id=f'{n:064x}',status='AWAITING_USER');ordinary.pop('observation',None);ordinary.pop('checked_at',None)
                        ctl.write(db,rv.key(ordinary),ordinary)
                    ctl.write(db,rv.PROGRESS+self.i['isin'],dict(scan_cursor='',counts={'RECHECK_QUEUED':1,'AWAITING_USER':size}))
                with patch.object(rv,'SCAN_LIMIT',16):result=rv.turn(self.s,self.i,self.t)
                self.assertEqual(result['checked'],4)
                with self.s.connect() as db:
                    self.assertEqual(ctl.read(db,rv.key(base))['status'],'REVIEW_WEEKLY')
                    ordinary_checked=sum(bool(json.loads(v).get('checked_at')) for v, in db.execute('SELECT value FROM settings WHERE key LIKE ? AND key!=?',(rv.PREFIX+'%',rv.key(base))))
                self.assertEqual(ordinary_checked,3)

if __name__=='__main__':unittest.main()
