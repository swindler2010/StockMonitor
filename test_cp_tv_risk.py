"""New owner-authorized TV absence policy; isolated fixtures, no market calls."""
import copy,json,unittest
from unittest.mock import patch
import test_cp_reconciliation as fx
from test_cp_reconciliation import ctl,rv,dq,pd,Store
from analytics import resample,_safe_features

class TVRisk(unittest.TestCase):
 setUp=fx.Reconciliation.setUp
 tearDown=fx.Reconciliation.tearDown
 event=fx.Reconciliation.event
 snapshot=fx.Reconciliation.snapshot
 def prepare(self,enable=True):
  self.now=pd.Timestamp.now(tz='UTC')
  end=self.now.tz_convert('Europe/Berlin').tz_localize(None).floor('15min')-pd.Timedelta(minutes=15)
  idx=dq.expected_index(self.m,'15m',pd.Timestamp('2026-10-19'),end,self.now)
  self.raw=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=100.),index=idx).drop(pd.Timestamp(self.a))
  self.raw.attrs=dict(self.m,timeframe='15m',classify_absence=True)
  with self.s.connect() as d:
   d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(self.i['isin'],'gettex','15m',str(t),*r) for t,r in self.raw.iterrows()])
  self.raw['source']='gettex'
  self.report=dq.audit(self.raw,'15m',now=self.now);self.raw.attrs['data_quality']=self.report
  self.assertEqual(self.report['state'],'UNCONFIRMED',self.report)
  self.s.set_setting(dq.key(self.i,self.m['symbol'],'15m'),self.report)
  self.event();rv.turn(self.s,self.i,self.now)
  with self.s.connect() as d:self.job=json.loads(d.execute('select value from settings where key like ?', (rv.PREFIX+self.i['isin']+':%',)).fetchone()[0])
  rv.observe(self.s,self.job['id'],self.i['isin'],ctl.digest(self.job),'ABSENT','GETTEX:APC',True)
  if enable:self.s.set_setting(ctl.TV_POLICY,dict(enabled=True,approved=True,version=ctl.TV_VERSION))
 def turn(self):return ctl.tv_risk_turn(self.s,self.i,self.now)
 def accepted(self):
  with self.s.connect() as d:return ctl.effective_accepted(d,self.i['isin'],ctl.read(d,'cp_manual_risk:'+self.i['isin'],{}))
 def output(self,raw=None):
  f=ctl.accepted_gap_input(self.s,self.i,self.raw if raw is None else raw,'15m',self.now)
  return ctl.accepted_gap_output(resample(f,'15m','15m',self.now),'15m',self.now)
 def test_exact_current_review_creates_one_disclosed_risk_and_no_data_writes(self):
  self.prepare();before=self.snapshot();out=self.turn()
  self.assertEqual(out['accepted'],1);self.assertEqual(before,self.snapshot())
  self.assertEqual(self.s.setting(dq.key(self.i,self.m['symbol'],'15m')),self.report)
  entry=next(iter(self.accepted().values()));self.assertEqual(entry['evidence']['label'],ctl.TV_LABEL)
  prior=self.s.setting('cp_manual_risk:'+self.i['isin']);self.assertFalse(self.turn()['changed'])
  self.assertEqual(prior,self.s.setting('cp_manual_risk:'+self.i['isin']))
 def test_disabled_policy_never_infers_consent(self):
  self.prepare(False);self.assertEqual(self.turn()['state'],'DISABLED');self.assertFalse(self.accepted())
 def test_changed_native_rows_or_identity_revoke_without_waiting_for_cp(self):
  self.prepare();self.turn()
  with self.s.connect() as d:d.execute('update candles set close=100.5 where isin=? and ts=?',(self.i['isin'],str(self.raw.index[-1])))
  self.assertFalse(self.accepted())
  self.s.save_instrument(dict(self.i,name='Changed input'))
  self.assertEqual(self.turn()['accepted'],0)
 def test_changed_observation_or_policy_revokes(self):
  self.prepare();self.turn()
  with self.s.connect() as d:
   j=ctl.read(d,rv.key(self.job));j['observation']['result']='PRESENT';ctl.write(d,rv.key(j),j)
  self.assertFalse(self.accepted());self.assertEqual(self.turn()['accepted'],0)
 def test_fresh_proof_rechecks_but_cannot_erase_or_expand_prior_review(self):
  self.prepare();self.turn()
  self.s.set_setting('interval_evidence:'+self.i['isin'],{'checked_at':self.now.isoformat()})
  self.assertFalse(self.accepted());self.assertEqual(self.turn()['accepted'],1)
  with self.s.connect() as d:d.execute('update candles set close=100.5 where isin=? and ts=?',(self.i['isin'],'2026-10-28 10:00:00'))
  self.assertFalse(self.accepted());self.assertEqual(self.turn()['accepted'],0)
 def test_broader_unreviewed_gap_and_invalid_report_not_granted(self):
  self.prepare()
  for state,span in [('UNCONFIRMED',[self.a,'2026-10-28 10:30:00']),('INVALID',[self.a,self.a])]:
   q=copy.deepcopy(self.report);q.update(state=state,ranges=[span]);self.s.set_setting(dq.key(self.i,self.m['symbol'],'15m'),q)
   self.assertEqual(self.turn()['accepted'],0)
 def test_other_source_cannot_borrow_tv_review(self):
  self.prepare();q=copy.deepcopy(self.report);q['identity']['source']='proxy';q['identity']['symbol']='APC.F';q['identity']['venue']='FRA'
  self.s.set_setting(dq.key(self.i,self.m['symbol'],'15m'),q);self.assertEqual(self.turn()['accepted'],0)
 def test_actual_bar_indicators_pass_without_estimates_and_factual_dq_retained(self):
  self.prepare();self.turn();out=self.output();risk=out.attrs['accepted_risk']
  self.assertEqual(risk['price_model'],ctl.TV_PRICE_MODEL);self.assertEqual(risk['price_estimates'],[])
  pd.testing.assert_series_equal(ctl.analytical_close_series(out),out.close)
  _,feature=_safe_features(out,self.i,self.now,prove_scope=True)
  self.assertTrue(feature['sufficient_history'],feature)
  self.assertEqual(feature['data_quality']['state'],'UNCONFIRMED')
 def test_invalid_ohlcv_stale_and_real_warmup_remain_blocked(self):
  self.prepare();self.turn()
  bad=self.raw.copy();bad.iloc[-5,bad.columns.get_loc('high')]=1
  self.assertNotIn('accepted_risk',self.output(bad).attrs)
  self.assertNotIn('accepted_risk',self.output(self.raw.iloc[:-20]).attrs)
  out=self.output().tail(20).copy();_,feature=_safe_features(out,self.i,self.now,prove_scope=True)
  self.assertFalse(feature['sufficient_history'])
 def test_revoke_and_restart_do_not_regrant(self):
  self.prepare();self.turn();ctl.revoke(self.s,self.i['isin']);self.s.read_cache.close();self.s=Store(self.temp.name)
  self.assertFalse(self.accepted());self.assertEqual(self.turn()['state'],'DISABLED')
 def test_existing_historical_review_does_not_resurrect_old_or_block_new_policy(self):
  self.prepare();self.s.set_setting('cp_manual_risk:'+self.i['isin'],dict(account='five-eur-10000-v1',accepted={},historical_review=dict(version=1,state='COMPLETE',results={})))
  self.turn();self.assertEqual(len(self.accepted()),1)
 def test_existing_feed_runs_policy_before_requested_eligibility(self):
  self.prepare();from feed_worker import FeedContext
  with patch.object(FeedContext,'_calculate',return_value={}),patch.object(ctl,'automatic_turn',return_value={}),patch('calculation_window.eligibility_turn',return_value={'checked_at':'new','error':None}) as call:
   r=FeedContext(self.s).calculate(self.i);self.assertEqual(r['cp_tv_risk']['accepted'],1);self.assertEqual(call.call_count,1)
  self.assertIsNotNone(self.s.setting(rv.PROGRESS+self.i['isin'])['analysis_receipt'])
 def test_read_only_health_exposes_exact_reason(self):
  self.prepare();self.turn()
  with self.s.connect() as d:
   d.execute('pragma query_only=on');rows=ctl.view(d,self.i['isin'])
  accepted=[x for x in rows if x['accepted']];self.assertEqual(len(accepted),1)
  self.assertEqual(accepted[0]['acceptance_evidence']['label'],ctl.TV_LABEL)
 def test_health_and_mcp_summary_same_receipts_readonly(self):
  from candlepilot_health import collect
  self.prepare();self.turn();before=self.snapshot()
  view=collect(self.s.path)['owner_controls'][self.i['isin']]['tv_risk']
  self.assertEqual(view['label'],ctl.TV_LABEL);self.assertEqual(view['reviewed_ranges'],1);self.assertEqual(view['active_ranges'],1)
  self.assertEqual(view['ranges'][0]['price_model'],ctl.TV_PRICE_MODEL)
  self.assertEqual(before,self.snapshot())
 def test_registered_stock_tv_grant_enters_demo_universe_but_revocation_blocks(self):
  import paper_account
  allowed=set(paper_account.ALLOWED)
  self.i={'isin':'DE0005557508','name':'Telekom'};self.m['symbol']=self.i['isin'];self.s.save_instrument(self.i)
  self.prepare();self.turn();out=self.output()
  self.assertIn('accepted_risk',out.attrs);_,feature=_safe_features(out,self.i,self.now,prove_scope=True)
  self.assertTrue(feature['sufficient_history'],feature);self.assertEqual(allowed,paper_account.ALLOWED)
  with self.s.connect() as d:
   self.assertEqual(ctl.manual_gate(d,self.i['isin'],self.m)['mode'],'MANUAL_RISK_ACCEPTED')
  ctl.revoke(self.s,self.i['isin'])
  with self.s.connect() as d:
   with self.assertRaisesRegex(ValueError,'PAPER_MANUAL_NOT_GRANTED'):ctl.manual_gate(d,self.i['isin'],self.m)
 def test_demo_quote_still_rejects_stale_or_invalid_price(self):
  import paper_account
  self.prepare();self.turn();last=self.raw.iloc[-1];at=self.raw.index[-1].tz_localize('Europe/Berlin')
  b={k:float(last[k]) for k in dq.FIELDS};b.update(closed=True,open_at=at.isoformat(),close_at=(at+pd.Timedelta(minutes=15)).isoformat())
  quote_now=at+pd.Timedelta(minutes=15,seconds=1)
  result=dict(isin=self.i['isin'],identity=self.m,timeframe='15m',data_quality=dict(self.report,checked_at=quote_now.isoformat()),bars=[b])
  paper_account.quote(self.i,result,quote_now.timestamp(),accepted_historical_risk=True)
  result['data_quality']['checked_at']=(quote_now+pd.Timedelta(seconds=901)).isoformat()
  with self.assertRaisesRegex(ValueError,'PAPER_STALE_QUOTE'):paper_account.quote(self.i,result,quote_now.timestamp()+901,accepted_historical_risk=True)
  result['data_quality']['checked_at']=quote_now.isoformat()
  b['volume']=0
  with self.assertRaisesRegex(ValueError,'PAPER_BAD_OHLCV'):paper_account.quote(self.i,result,quote_now.timestamp(),accepted_historical_risk=True)

if __name__=='__main__':unittest.main()
