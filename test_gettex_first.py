"""Issue20: factual calculation and CP opinion are independent."""
import copy,unittest
import test_trading_engine as fixtures
from analytics import _safe_features,resample
from gettex_first import calculation_allowed,bind,bound
from trading_engine import evaluate

class GettexFirstTests(unittest.TestCase):
 def test_aggregation_keeps_actual_bars_without_claiming_complete_coverage(self):
  pd=fixtures.pd
  f=self.frame().iloc[:7].copy()
  f.index=pd.date_range('2026-11-13 07:30',periods=7,freq='15min')
  f.attrs.update(timeframe='15m',data_quality={'state':'UNCONFIRMED'})
  f=f.drop(f.index[2]);f=bind(f,{'isin':'US0378331005'})
  now=pd.Timestamp('2026-11-13T12:00Z')
  for tf in ('30m','1H','2H','3H','4H','1D','1W','1M'):
   with self.subTest(tf=tf):
    out=resample(f,'15m',tf,now)
    self.assertFalse(out.empty)
    self.assertEqual(float(out.volume.sum()),float(f.volume.sum()))
    self.assertEqual(float(out.high.max()),float(f.high.max()))
    self.assertEqual(float(out.low.min()),float(f.low.min()))
    self.assertFalse(out.bar_complete.all())
    self.assertNotEqual(out.attrs['data_quality']['state'],'READY')
 def test_false_or_damaged_source_contract_cannot_enable_aggregation(self):
  f=bind(self.frame(),{'isin':'US0378331005'});self.assertTrue(bound(f))
  f.attrs['currency']='USD';self.assertFalse(bound(f))
  f=bind(self.frame(),{'isin':'US0378331005'});f.iloc[0,f.columns.get_loc('volume')]=-1
  self.assertFalse(bound(f))
 def test_corrected_child_recomputes_parent_and_indicators_without_changing_original(self):
  pd=fixtures.pd
  f=self.frame().copy();f.index=pd.date_range('2026-11-02 07:30',periods=len(f),freq='15min')
  f.attrs.update(timeframe='15m',data_quality={'state':'UNCONFIRMED'})
  f=bind(f,{'isin':'US0378331005'});original=f.copy(deep=True)
  now=pd.Timestamp('2026-12-01T12:00Z');before=resample(f,'15m','1H',now)
  changed=f.copy();changed.iloc[3,changed.columns.get_loc('high')]+=5
  after=resample(changed,'15m','1H',now)
  self.assertTrue(f.equals(original));self.assertNotEqual(before.high.iloc[0],after.high.iloc[0])
  self.assertEqual(float(before.volume.sum()),float(after.volume.sum()))
  self.assertEqual(before.attrs['data_quality']['state'],after.attrs['data_quality']['state'])
 def test_long_gettex_archive_survives_short_clean_alternative(self):
  import gettex_chart
  pd=fixtures.pd
  info={'isin':'US0378331005'}
  long=self.frame();long.index=pd.date_range('2026-11-13 07:30',periods=len(long),freq='5min')
  long.attrs.update(timeframe='5m',data_quality={'state':'INVALID'})
  short=long.iloc[-10:].copy();short.attrs['data_quality']={'state':'READY'}
  view,base=gettex_chart.retain_deeper(info,'1H',long,'5m',short,'5m',pd.Timestamp('2026-11-14T12:00Z'))
  self.assertEqual(base,'5m');self.assertEqual(len(view),len(long))
  self.assertEqual(view.attrs['data_quality']['state'],'INVALID')
 def test_general_assessment_and_display_preserve_advisory_quality(self):
  from calculation_window import bounded_features
  from partial_chart import select_display
  from unittest.mock import patch
  f=bind(self.frame(),{'isin':'US0378331005'})
  f.attrs.update(data_quality={'state':'INVALID','issues':['historical suspicion']},partial_chart={'display_only':True})
  info={'isin':'US0378331005'};now=fixtures.pd.Timestamp('2026-02-01T00:00Z')
  r=bounded_features(info,f,'1H',now)
  self.assertTrue(r['analysis_ready']);self.assertNotEqual(r['window_quality']['state'],'READY')
  with patch('partial_chart.live_tail',side_effect=lambda i,t,v,s,n:v):
   view=select_display(info,'1H',f,None,now)
  self.assertNotIn('partial_chart',view.attrs);self.assertEqual(view.attrs['data_quality']['state'],'INVALID')
  self.assertTrue(f.attrs['partial_chart']['display_only'])
 def test_partial_coverage_closed_bars_are_calculable_and_forming_bar_excluded(self):
  f=bind(self.frame(),{'isin':'US0378331005'});f['bar_complete']=False
  f.attrs.update(data_quality={'state':'UNCONFIRMED'},analysis_scope={'start':'2021-01-01'})
  _,r=_safe_features(f,{'isin':'US0378331005'},'2026-02-01T00:00Z',prove_scope=True)
  self.assertTrue(calculation_allowed(r));self.assertFalse(r['scope_initialization']['checked'])
  f.index=fixtures.pd.date_range('2026-01-30 07:30',periods=len(f),freq='1h')
  _,r=_safe_features(f,{'isin':'US0378331005'},'2026-01-30T09:00Z')
  self.assertFalse(calculation_allowed(r));self.assertLess(r['closed_bar_count'],35)
 def frame(self):
  cls=next(c for c in vars(fixtures).values() if isinstance(c,type) and hasattr(c,'market_frames'))
  f=cls().market_frames()['1H'];f['source']='gettex'
  f.attrs.update(source='gettex',symbol='US0378331005',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  return f
 def test_warnings_preserved_and_identical_indicators_for_real_bars(self):
  f=self.frame();info={'isin':'US0378331005'};now='2026-02-01T00:00Z'
  base_x,base=_safe_features(f,info,now)
  for state in ('GAP','UNCONFIRMED','INVALID','STALE','SOURCE_GAP','RECOVERING'):
   with self.subTest(state=state):
    f.attrs['data_quality']={'state':state,'ranges':[['2026-01-01','2026-01-01']]}
    before=copy.deepcopy(f.attrs);x,r=_safe_features(f,info,now)
    self.assertTrue(calculation_allowed(r));self.assertEqual(r['facts'],base['facts'])
    self.assertTrue(x.ema30.equals(base_x.ema30));self.assertEqual(f.attrs,before)
    self.assertEqual(r['data_quality']['state'],state)
    self.assertFalse(r['calculation_availability']['execution_permission'])
 def test_bad_values_missing_bars_and_wrong_listing_remain_unavailable(self):
  for kind in ('bad','empty','short','currency','isin','provider','row_source'):
   f=self.frame();info={'isin':'US0378331005'};f.attrs['data_quality']={'state':'UNCONFIRMED'}
   if kind=='bad':f.iloc[-1,f.columns.get_loc('high')]=0
   if kind=='empty':f=f.iloc[:0]
   if kind=='short':f=f.tail(10)
   if kind=='currency':f.attrs['currency']='USD'
   if kind=='isin':info['isin']='WRONG'
   if kind=='provider':f.attrs['source']='proxy'
   if kind=='row_source':f.iloc[0,f.columns.get_loc('source')]='yahoo'
   _,r=_safe_features(f,info,'2026-02-01T00:00Z')
   self.assertFalse(calculation_allowed(r),kind)
 def test_core_uses_calculation_availability_and_keeps_strategy_rules(self):
  f=self.frame();f.attrs['data_quality']={'state':'UNCONFIRMED'}
  _,r=_safe_features(f,{'isin':'US0378331005'},'2026-02-01T00:00Z')
  contexts={tf:copy.deepcopy(r) for tf in ('1H','4H','1D')}
  self.assertEqual(evaluate(contexts,'1H')['status'],'READY')
  contexts['4H']['ohlcv_valid']=False
  self.assertEqual(evaluate(contexts,'1H')['status'],'DATA BLOCKED')
 def test_paper_dq_warning_does_not_remove_revision_or_quote_guards(self):
  import test_paper_hourly as fixture
  import paper_hourly as hourly
  import paper_account as paper
  case=fixture.HourlyTests();case.setUp();self.addCleanup(case.tearDown)
  result=case.result_for('BUY')
  engine=result['execution_context']['trading_engine']
  for tf in result['hourly']['required_tf']:
   engine['timeframe_context'][tf]={'data_quality':{'state':'UNCONFIRMED'},
    'calculation_availability':{'policy':'gettex_first_v1','source_contract':True,'analysis_available':True}}
  hourly.validate_analysis(case.db,case.info,result,case.now,case.cfg)
  bad=copy.deepcopy(result);bad['hourly']['ledger_token']='changed'
  with self.assertRaisesRegex(ValueError,'POSITION_CHANGED'):
   hourly.validate_analysis(case.db,case.info,bad,case.now,case.cfg)
  bad=copy.deepcopy(result);bad['quote_blocker']='PAPER_STALE_QUOTE'
  with self.assertRaisesRegex(ValueError,'PAPER_STALE_QUOTE'):
   hourly.validate(case.db,case.info,bad,case.args,case.now)
  bad=copy.deepcopy(result);bad['execution_context']['trading_engine']['timeframe_context']['4H']['calculation_availability']['source_contract']=False
  with self.assertRaisesRegex(ValueError,'DQ_BLOCKED'):
   hourly.validate_analysis(case.db,case.info,bad,case.now,case.cfg)

if __name__=='__main__':unittest.main()
