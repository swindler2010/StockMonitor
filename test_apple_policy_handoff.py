"""Only new Apple policy handoff and historical-review receipt guards."""
import copy,json,sqlite3,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_controls as cp
from performance_cache import ReadCache,InputRevisionChanged
from paper_account import ACCOUNT
from timeframes import LABELS
ISIN='US0378331005'
META=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')

class PolicyHandoff(unittest.TestCase):
 def setUp(self):
  self.cache=ReadCache(':memory:');self.addCleanup(self.cache.close)
  self.cache.db.execute('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER)')
  self.old=dict(version=cp.DEMO_INITIALIZATION,isin=ISIN,account=ACCOUNT,identity=META,entry_timeframes=['5m','15m'],analysis_timeframes=list(LABELS),approved=True)
  self.config={'cp_manual_risk:'+ISIN:{'initialization_policy':self.old},cp.DEMO_ALL_POLICY:dict(version=cp.DEMO_ALL_INITIALIZATION,account=ACCOUNT,scope='registered_stocks',older_verified_warmup=True,approved=True),
    'gettex_chart:'+ISIN+':1D':dict(state='SAVED',identity=META,listing_proof=dict(META,isin=ISIN,status='CONFIRMED'))}
  self.store=SimpleNamespace(read_cache=self.cache,setting=lambda k,d=None:self.config.get(k,d));self.info=dict(isin=ISIN,name='Apple')
 def test_later_global_policy_reaches_existing_15m_demo_context(self):
  with self.cache.track_inputs() as token:p=cp.initialization_policy_for(self.store,self.info)
  self.assertEqual(p['version'],cp.DEMO_ALL_INITIALIZATION);self.assertEqual(p['entry_timeframes'],['15m']);self.assertEqual(p['identity'],META)
  self.assertIn('setting:'+cp.DEMO_ALL_POLICY,token);self.assertIn('setting:cp_manual_risk:'+ISIN,token)
  self.assertIn('setting:gettex_chart:'+ISIN+':1D',token)
  self.cache.db.execute('INSERT INTO input_revisions VALUES (?,1)',('setting:'+cp.DEMO_ALL_POLICY,))
  with self.assertRaises(InputRevisionChanged):self.cache.assert_current(token)
 def test_no_new_authority_preserves_separate_legacy_mode(self):
  self.config[cp.DEMO_ALL_POLICY]['approved']=False
  self.assertEqual(cp.initialization_policy_for(self.store,self.info),self.old)
 def test_global_mode_cannot_switch_legacy_pinned_listing(self):
  s=self.config['gettex_chart:'+ISIN+':1D'];s['identity']={**META,'symbol':'OTHER.GTX'};s['listing_proof']={**s['identity'],'isin':ISIN,'status':'CONFIRMED'}
  self.assertEqual(cp.initialization_policy_for(self.store,self.info),self.old)
 def test_wrong_currency_cannot_get_either_context(self):
  self.assertIsNone(cp.initialization_policy_for(self.store,self.info,{**META,'currency':'USD'}))

class HistoricalReviewScope(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
  self.db.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER);INSERT INTO input_revisions VALUES ("native",1);')
  self.problem=dict(id='p',kind='archive',identity=META,tf='15m',range=['2026-10-16 22:15:00']*2)
  self.original=dict(problem=self.problem,actor='owner_local_ui',accepted_at='2026-10-30')
  self.grant=dict(account=ACCOUNT,accepted={'p':copy.deepcopy(self.original)},historical_review=dict(version=1,state='RUNNING',execution_suspended=True,targets={'p':copy.deepcopy(self.original)}))
  self.put(cp.TV_POLICY,dict(version=cp.TV_VERSION,enabled=True,approved=True))
 def put(self,k,v):self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(k,json.dumps(v)))
 def newer(self):self.grant['accepted']['p']=dict(problem=self.problem,actor=cp.TV_ACTOR,accepted_at='2026-11-03',input_revisions={'native':1})
 def gate(self):
  self.put('cp_manual_risk:'+ISIN,self.grant)
  with patch('paper_account.registered_instrument',return_value={'isin':ISIN}),patch.object(cp,'problems',return_value=[self.problem]):
   return cp.manual_gate(self.db,ISIN,META)
 def test_original_owner_exception_remains_suspended(self):
  with self.assertRaisesRegex(ValueError,'HISTORICAL_REVIEW_PENDING'):self.gate()
 def test_new_current_tv_receipt_is_not_the_revoked_old_exception(self):
  self.newer();out=self.gate()
  self.assertEqual(out['mode'],'MANUAL_RISK_ACCEPTED');self.assertFalse(out['automatic_signal'])
  self.assertEqual(out['problem_ids'],['p'])
  self.assertFalse(cp._original_review_receipt(self.grant['historical_review'],'p',self.grant['accepted']['p']))
 def test_changed_revision_and_revoked_policy_still_block(self):
  self.newer();self.db.execute('UPDATE input_revisions SET revision=2')
  with self.assertRaises(ValueError):self.gate()
  self.db.execute('UPDATE input_revisions SET revision=1');self.put(cp.TV_POLICY,dict(version=cp.TV_VERSION,enabled=True,approved=False))
  with self.assertRaises(ValueError):self.gate()
 def test_old_tv_receipt_and_changed_diagnostic_text_cannot_escape_review(self):
  self.newer();self.grant['historical_review']['targets']['p']=copy.deepcopy(self.grant['accepted']['p'])
  self.grant['accepted']['p']['problem']=dict(self.problem,reasons=['updated report, same owner decision'])
  with self.assertRaisesRegex(ValueError,'HISTORICAL_REVIEW_PENDING'):self.gate()
 def test_completed_old_review_does_not_delete_new_tv_receipt(self):
  import pandas as pd
  from contextlib import contextmanager
  self.newer();self.problem.update(isin=ISIN,report_key='data_quality:'+ISIN+':AAPL.GTX:15m')
  review=self.grant['historical_review'];review.update(results={},groups={})
  review['targets']['p']['problem']=copy.deepcopy(self.problem)
  self.db.execute('CREATE TABLE instruments(isin TEXT,payload TEXT)')
  self.db.execute('INSERT INTO instruments VALUES (?,?)',(ISIN,json.dumps({'isin':ISIN})))
  self.put('cp_manual_risk:'+ISIN,self.grant);self.db.commit()
  @contextmanager
  def connect():
   with self.db:yield self.db
  frame=pd.DataFrame(columns=['open','high','low','close','volume'],index=pd.DatetimeIndex([]))
  s=SimpleNamespace(connect=connect,load_series=lambda *a:frame.copy(),setting=lambda k,d=None:cp.read(self.db,k,d))
  report=dict(state='UNCONFIRMED',ranges=[self.problem['range']],repairable=False)
  with patch('eur_listings.stored_identity',return_value=META),patch('data_quality.attach_interval_evidence'),patch('data_quality.audit',return_value=report):
   cp.historical_review_turn(s,{'isin':ISIN},pd.Timestamp('2026-11-03T13:00Z'))
  saved=cp.read(self.db,'cp_manual_risk:'+ISIN)
  self.assertEqual(saved['historical_review']['state'],'COMPLETE')
  self.assertEqual(saved['accepted']['p'],self.grant['accepted']['p'])
  self.assertEqual(saved['historical_review']['results']['p']['state'],'UNCONFIRMED')

if __name__=='__main__':unittest.main()
