"""Scoped owner review on the existing pipeline; RAM only, no network/fills."""
import contextlib,copy,json,sqlite3,sys,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from performance_cache import ReadCache,install_input_revisions,capture_closed_inputs
import cp_controls as cp
import data_quality as dq

class Memory(Store):
 def __init__(self):
  self.db=sqlite3.connect(':memory:');self.directory=Path(':memory:')
  self.db.executescript('''CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT);
  CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
  CREATE TABLE candles(isin,source,base_tf,ts,open,high,low,close,volume);
  CREATE TABLE series(isin,source,venue,currency,symbol,base_tf,ts,open,high,low,close,volume);''')
  install_input_revisions(self.db);self.db.commit()
  self.read_cache=ReadCache(':memory:');self.read_cache.db.close();self.read_cache.db=self.db
 @contextlib.contextmanager
 def connect(self):
  try:yield self.db;self.db.commit()
  except:self.db.rollback();raise

class ReviewedRisk(unittest.TestCase):
 def setUp(self):
  self.s=Memory();self.i=dict(isin='US0378331005',name='Apple',eur_history_auto=True)
  self.m=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.a='2026-10-28 13:30:00';self.now=pd.Timestamp('2026-10-28T16:00Z')
  self.s.save_instrument(self.i)
  self.s.set_setting('gettex_chart:'+self.i['isin']+':15m',dict(state='SAVED',identity=self.m,listing_proof=dict(self.m,isin=self.i['isin'],status='CONFIRMED')))
  idx=dq.expected_index(self.m,'15m',pd.Timestamp('2026-10-19'),pd.Timestamp('2026-10-28 16:45'),self.now)
  self.raw=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=100.),index=idx).drop(pd.Timestamp(self.a))
  self.raw.attrs=dict(self.m,timeframe='15m',listing_proof=dict(self.m,isin=self.i['isin'],status='CONFIRMED'),classify_absence=True)
  self.s.save_series(self.i,self.raw,'15m');self.key=dq.key(self.i,self.m['symbol'],'15m')
  self.q=dq.audit(self.raw,'15m',now=self.now);self.raw.attrs['data_quality']=self.q
  self.s.set_setting(self.key,self.q)
  self.p=next(p for p in cp.problems(self.s.db,self.i['isin']) if p['range']==[self.a,self.a])
  self.scopes=['instrument:'+self.i['isin'],'market:'+self.i['isin']+':15m','setting:'+self.key]
 def tearDown(self):self.s.read_cache.close()
 def authorize(self):
  return cp.authorize_reviewed_gaps(self.s,self.i['isin'],[self.p],dict(sha256='fixture',owner_decision='Explicit scoped review'),cp._revisions(self.s.db,self.scopes))
 def turn(self):return cp.reviewed_risk_turn(self.s,self.i,self.now)
 def effective(self):return cp.effective_accepted(self.s.db,self.i['isin'],self.s.setting('cp_manual_risk:'+self.i['isin'],{}))
 def native(self):return self.s.db.execute('SELECT * FROM series ORDER BY ts').fetchall()
 def test_exact_scope_no_rewrites_or_false_ready_and_idempotent(self):
  before=self.native();self.authorize();self.assertEqual(self.turn()['accepted'],1)
  self.assertEqual(before,self.native());self.assertEqual(self.q,self.s.setting(self.key))
  self.assertFalse(self.turn()['changed']);self.assertEqual(len(self.effective()),1)
 def test_no_consent_or_wrong_cas_never_grants(self):
  self.assertEqual(self.turn()['state'],'DISABLED')
  with self.assertRaisesRegex(ValueError,'REVISIONS_CHANGED'):
   cp.authorize_reviewed_gaps(self.s,self.i['isin'],[self.p],dict(sha256='x',owner_decision='x'),{self.scopes[0]:-1})
 def test_new_tail_revokes_immediately_then_cp_renews_same_scopes(self):
  self.authorize();self.turn()
  with self.s.connect() as d:d.execute('UPDATE series SET close=100.1 WHERE ts=?',(str(self.raw.index[-1]),))
  self.assertFalse(self.effective());self.assertEqual(self.turn()['accepted'],1)
 def test_changed_reviewed_neighbour_revokes_and_does_not_self_approve(self):
  self.authorize();self.turn()
  with self.s.connect() as d:d.execute('UPDATE series SET close=100.1 WHERE ts=?',('2026-10-28 13:45:00',))
  self.assertFalse(self.effective());self.assertEqual(self.turn()['accepted'],0)
 def test_actual_recovery_removes_grant(self):
  self.authorize();self.turn()
  with self.s.connect() as d:d.execute('INSERT INTO series SELECT isin,source,venue,currency,symbol,base_tf,?,open,high,low,close,volume FROM series LIMIT 1',(self.a,))
  self.assertEqual(self.turn()['accepted'],0)
 def test_bad_dq_or_changed_identity_cannot_be_accepted(self):
  self.authorize();self.turn()
  for changes in (dict(state='INVALID'),dict(identity={**self.m,'currency':'USD'})):
   self.s.set_setting(self.key,{**self.q,**changes});self.assertFalse(self.effective());self.assertEqual(self.turn()['accepted'],0)
 def test_owner_revoke_prevents_automatic_regrant(self):
  self.authorize();self.turn();cp.revoke(self.s,self.i['isin'])
  self.assertEqual(self.turn()['state'],'DISABLED');self.assertFalse(self.effective())
 def test_restart_uses_saved_policy_without_expanding_scope(self):
  self.authorize();self.turn();other=Memory();self.s.db.backup(other.db)
  self.assertFalse(cp.reviewed_risk_turn(other,self.i,self.now)['changed']);other.read_cache.close()
 def test_actual_only_analytical_prices_and_warning(self):
  from analytics import resample
  self.authorize();self.turn();f=cp.accepted_gap_input(self.s,self.i,self.raw,'15m',self.now)
  out=cp.accepted_gap_output(resample(f,'15m','15m',self.now),'15m',self.now)
  self.assertEqual(out.attrs['accepted_risk']['price_model'],cp.TV_PRICE_MODEL)
  self.assertIn('конкретное',out.attrs['accepted_risk']['label'])
  pd.testing.assert_series_equal(cp.analytical_close_series(out),out.close)
  self.assertNotIn(pd.Timestamp(self.a),out.index)
 def test_new_gap_and_stale_tail_do_not_borrow_consent(self):
  self.authorize();self.turn()
  for f in (self.raw.drop(self.raw.index[-3]),self.raw.iloc[:-20]):
   self.assertNotIn('accepted_gap_input',cp.accepted_gap_input(self.s,self.i,f,'15m',self.now).attrs)
 def test_display_does_not_reblock_current_risk_but_unreviewed_gap_stays_partial(self):
  from partial_chart import select_display
  self.authorize();self.turn()
  out=select_display(self.i,'15m',self.raw,self.s,self.now)
  self.assertTrue(out.attrs.get('accepted_risk'));self.assertNotIn('partial_chart',out.attrs)
  self.assertEqual(out.attrs['data_quality']['state'],'UNCONFIRMED')
  cp.revoke(self.s,self.i['isin']);out=select_display(self.i,'15m',self.raw,self.s,self.now)
  self.assertTrue(out.attrs.get('partial_chart'));self.assertFalse(out.attrs.get('accepted_risk'))
 def test_pin_only_authorized_entry_and_current_identity(self):
  self.authorize();self.turn();self.assertEqual(cp.reviewed_entry_identity(self.s,self.i,'15m'),self.m)
  self.assertIsNone(cp.reviewed_entry_identity(self.s,self.i,'5m'))
  self.s.save_instrument(dict(self.i,name='New identity revision'))
  self.assertIsNone(cp.reviewed_entry_identity(self.s,self.i,'15m'))
 def test_actual_window_excludes_only_proven_outside_problem(self):
  series=[dict(spec=dict(base_tf='15m',timeframe='4H',**{'from':'2026-10-20','through':'2026-10-29T00:00Z'}))]
  self.assertTrue(cp._problem_in_dependencies(self.p,series))
  self.assertFalse(cp._problem_in_dependencies({**self.p,'range':['2019-01-01','2019-01-02']},series))
  self.assertTrue(cp._problem_in_dependencies({**self.p,'range':None},series))
  series[0]['spec'].pop('from');self.assertTrue(cp._problem_in_dependencies(self.p,series))
 def test_manual_dependency_gate_real_proof_and_revision_rejection(self):
  self.authorize();self.turn()
  specs=[dict(isin=self.i['isin'],identity=self.m,base_tf='15m',timeframe=tf,**{'from':'2026-10-19','through':'2026-10-29T00:00Z'}) for tf in ('15m','4H','1D')]
  proof=capture_closed_inputs(self.s.db,specs,cp._revisions(self.s.db,self.scopes))
  with patch.object(cp,'problems',return_value=[self.p,{**self.p,'id':'old','range':['2015-01-01','2015-01-01']}]):
   gate=cp.manual_gate(self.s.db,self.i['isin'],self.m,'15m',proof)
   self.assertEqual(gate['problem_ids'],[self.p['id']]);self.assertEqual(gate['price_model'],cp.TV_PRICE_MODEL)
  with patch.object(cp,'problems',return_value=[self.p,{**self.p,'id':'new'}]):
   with self.assertRaisesRegex(ValueError,'NEW_OR_UNACCEPTED'):cp.manual_gate(self.s.db,self.i['isin'],self.m,'15m',proof)
  with self.s.connect() as d:d.execute('UPDATE series SET close=100.2 WHERE ts=?',(str(self.raw.index[-1]),))
  self.turn()
  with self.assertRaisesRegex(ValueError,'DEPENDENCY_CHANGED'):cp.manual_gate(self.s.db,self.i['isin'],self.m,'15m',proof)
 def test_existing_feed_dispatches_reassessment_after_new_review(self):
  from feed_worker import FeedContext
  self.authorize()
  with patch.object(FeedContext,'_calculate',return_value={}),patch.object(cp,'automatic_turn',return_value={}),patch('cp_reconciliation.turn',return_value={}),patch.object(cp,'tv_risk_turn',return_value={}),patch('calculation_window.eligibility_turn',return_value={}) as assess,patch('cp_reconciliation.complete_analysis_request'):
   result=FeedContext(self.s).calculate(self.i)
  self.assertEqual(result['cp_reviewed_risk']['accepted'],1);self.assertEqual(assess.call_count,1)
 def test_tv_absence_and_reviewed_discrepancy_coexist_without_estimates(self):
  from analytics import resample
  self.authorize();self.turn();second='2026-10-27 13:30:00'
  with self.s.connect() as d:d.execute('DELETE FROM series WHERE ts=?',(second,))
  raw=self.raw.drop(pd.Timestamp(second));q=dq.audit(raw,'15m',now=self.now);raw.attrs['data_quality']=q;self.s.set_setting(self.key,q)
  self.s.set_setting(cp.TV_POLICY,dict(enabled=True,approved=True,version=cp.TV_VERSION))
  self.turn();p=next(x for x in cp.problems(self.s.db,self.i['isin']) if x['range']==[second,second])
  g=self.s.setting('cp_manual_risk:'+self.i['isin']);g['accepted'][p['id']]=dict(problem=p,actor=cp.TV_ACTOR,input_revisions=cp._revisions(self.s.db,self.scopes),evidence=dict(price_model=cp.TV_PRICE_MODEL))
  self.s.set_setting('cp_manual_risk:'+self.i['isin'],g)
  f=cp.accepted_gap_input(self.s,self.i,raw,'15m',self.now)
  self.assertEqual(f.attrs['accepted_gap_input']['reviewed_discrepancy_count'],1)
  out=cp.accepted_gap_output(resample(f,'15m','15m',self.now),'15m',self.now)
  self.assertEqual(out.attrs['accepted_risk']['price_estimates'],[]);self.assertEqual(len(out),len(raw))
 def test_scoped_grant_does_not_relax_quote_age_or_invalid_ohlcv(self):
  import paper_account as paper
  self.authorize();self.turn();last=self.raw.iloc[-1];at=self.raw.index[-1].tz_localize('Europe/Berlin');now=at+pd.Timedelta(minutes=15,seconds=1)
  b={k:float(last[k]) for k in dq.FIELDS};b.update(closed=True,open_at=at.isoformat(),close_at=(at+pd.Timedelta(minutes=15)).isoformat())
  q={**self.q,'checked_at':now.isoformat()};result=dict(isin=self.i['isin'],identity=self.m,timeframe='15m',data_quality=q,bars=[b]);proof={**self.m,'listing_proof':dict(self.m,isin=self.i['isin'],status='CONFIRMED')}
  paper.quote(self.i,result,now.timestamp(),accepted_historical_risk=True,stored_listing=proof)
  q['checked_at']=(now+pd.Timedelta(seconds=901)).isoformat()
  with self.assertRaisesRegex(ValueError,'STALE_QUOTE'):paper.quote(self.i,result,now.timestamp()+901,accepted_historical_risk=True,stored_listing=proof)
  q['checked_at']=now.isoformat()
  b['high']=0
  with self.assertRaisesRegex(ValueError,'BAD_OHLCV'):paper.quote(self.i,result,now.timestamp(),accepted_historical_risk=True,stored_listing=proof)

class DailyTail(unittest.TestCase):
 def setUp(self):
  self.s=Memory();self.i=dict(isin='US0378331005',name='Apple');self.s.save_instrument(self.i)
  self.m=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.now=pd.Timestamp('2026-10-28T23:30Z');self.day='2026-10-28 00:00:00'
  self.s.set_setting('gettex_chart:'+self.i['isin']+':1D',dict(state='SAVED',identity=self.m,listing_proof=dict(self.m,isin=self.i['isin'],status='CONFIRMED')))
  parent=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=10.),index=pd.DatetimeIndex(['2026-10-27']));parent.attrs=self.m.copy();self.s.save_series(self.i,parent,'1D')
  idx=pd.date_range('2026-10-28 07:30','2026-10-28 22:45',freq='15min')
  self.children=pd.DataFrame(dict(open=100.,high=102.,low=98.,close=101.,volume=2.),index=idx);self.children.attrs=self.m.copy();self.s.save_series(self.i,self.children,'15m')
 def tearDown(self):self.s.read_cache.close()
 def turn(self,now=None):
  import gettex_chart as g
  return g.recover_closed_daily_tail(self.i,self.s,{'last_complete':self.day},now or self.now)
 def rows(self):return self.s.load_series(self.i['isin'],'1D',self.m['source'],self.m['symbol'],'GETTEX','EUR')
 def test_late_daily_recovered_once_strict_children_provenance(self):
  before=self.rows();out=self.turn();after=self.rows()
  self.assertEqual(out['completed'],[[self.day,self.day]]);self.assertEqual(after.loc[self.day,'volume'],124.)
  pd.testing.assert_frame_equal(before[list(dq.FIELDS)],after.loc[before.index,list(dq.FIELDS)])
  self.assertFalse(after.attrs['invalid_parent_provenance']);self.assertEqual(self.turn()['state'],'UNCHANGED')
  # A newly completed tail is not a repaired hole inside the old archive.
  self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM candle_recoveries').fetchone()[0],0)
 def test_unknown_child_never_manufactures_daily_or_calls_network(self):
  with self.s.connect() as d:d.execute("DELETE FROM series WHERE base_tf='15m' AND ts='2026-10-28 13:30:00'")
  out=self.turn();self.assertFalse(out['completed']);self.assertEqual(len(self.rows()),1)
 def test_invalid_child_and_identity_remain_blocked(self):
  with self.s.connect() as d:d.execute("UPDATE series SET high=1 WHERE base_tf='15m' AND ts='2026-10-28 13:30:00'")
  self.assertFalse(self.turn()['completed']);self.assertEqual(len(self.rows()),1)
  saved=self.s.setting('gettex_chart:'+self.i['isin']+':1D');saved['identity']['currency']='USD';self.s.set_setting('gettex_chart:'+self.i['isin']+':1D',saved)
  self.assertEqual(self.turn()['state'],'NOT_AVAILABLE')
 def test_open_day_and_child_revision_never_pass_as_closed_valid_parent(self):
  self.assertEqual(self.turn(pd.Timestamp('2026-10-28T20:00Z'))['completed'],[])
  self.assertEqual(len(self.rows()),1);self.turn()
  with self.s.connect() as d:d.execute("UPDATE series SET high=103 WHERE base_tf='15m' AND ts='2026-10-28 13:30:00'")
  self.assertEqual(self.rows().attrs['invalid_parent_provenance'],[self.day])
 def test_default_recovery_behavior_does_not_request_stale_tail(self):
  out=dq.repair(self.i,self.s,self.m['symbol'],'1D',self.m,lambda *a,**k:self.fail('network'),self.now)
  self.assertEqual(out['state'],'STALE');self.assertEqual(len(self.rows()),1)

if __name__=='__main__':unittest.main()
