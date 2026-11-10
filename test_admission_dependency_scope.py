import copy,json,sys,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import calculation_window as cw,gettex_chart as g,data_quality as dq
from test_apple_monthly_warmup import AppleMonthlyWarmup

class DependencyScope(unittest.TestCase):
 def setUp(self):
  self.now=pd.Timestamp('2026-11-02T12:00Z');self.meta=dict(source=g.SOURCE,symbol='TEST.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  dates=dq.expected_index(self.meta,'1D',pd.Timestamp('2020-01-02'),pd.Timestamp('2026-10-30'),self.now)
  self.frame=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=100.,source=g.SOURCE),index=dates)
  self.report={'state':'INVALID','repairable':True,'identity':self.meta,'ranges':[['2020-02-03','2020-02-03']],'rejected_timestamps':['2020-02-03'],'issues':['Impossible OHLC']}
  self.frame.attrs={**self.meta,'timeframe':'1D','data_quality':self.report,'archive_invalid':self.report,'partial_chart':{'display_only':True}}
 def test_old_localized_fault_is_outside_scoped_inputs_but_archive_kept(self):
  before=copy.deepcopy(self.frame);out=cw.scope_raw(self.frame,'1D',self.now)
  self.assertNotIn('archive_invalid',out.attrs);self.assertNotIn('partial_chart',out.attrs)
  self.assertEqual(out.attrs['archive_quality_before_scope'],self.report)
  self.assertEqual(dq.audit(out,'1D',expected=self.meta,now=self.now)['state'],'READY')
  pd.testing.assert_frame_equal(before,self.frame);self.assertEqual(self.frame.attrs,before.attrs)
 def test_in_scope_rejection_unknown_scope_or_wrong_identity_not_cleared(self):
  for mode in ('inside','unlocalized','identity','badstamp'):
   f=self.frame.copy();q=copy.deepcopy(self.report)
   if mode=='inside':q.update(ranges=[['2026-10-01','2026-10-01']],rejected_timestamps=['2026-10-01'])
   if mode=='unlocalized':q['ranges']=[]
   if mode=='identity':q['identity']={**self.meta,'currency':'USD'}
   if mode=='badstamp':q['rejected_timestamps']=['invalid timestamp']
   f.attrs['archive_invalid']=q
   with self.subTest(mode=mode):self.assertIn('archive_invalid',cw.scope_raw(f,'1D',self.now).attrs)
 def test_actual_current_bad_ohlcv_still_rejected(self):
  self.frame.iloc[-2,self.frame.columns.get_loc('high')]=0
  out=cw.scope_raw(self.frame,'1D',self.now)
  self.assertEqual(dq.audit(out,'1D',expected=self.meta,now=self.now)['state'],'INVALID')
 def choose(self,report=None,scoped=True):
  fallback=self.frame.tail(100).copy();fallback.attrs={**self.meta,'symbol':'OTHER.GTX','timeframe':'1D'}
  store=NS(setting=lambda key,default:report or {})
  with patch.object(g,'load',return_value=self.frame),patch('onvista_archive.available_daily',return_value=[]),patch('eur_listings.confirmed_listing',return_value=True):
   return g.daily_candidate(store,{'isin':'TEST'},'1D',fallback,'1D',pd.DataFrame(),'1D',self.now,analysis_at=self.now if scoped else None)[0]
 def test_selection_uses_calculation_scope_and_chart_keeps_archive_gate(self):
  selected=self.choose(self.report);self.assertEqual(selected.attrs['symbol'],'TEST.GTX');self.assertGreater(len(selected),100)
  self.assertEqual(self.choose(self.report,False).attrs['symbol'],'OTHER.GTX')
 def test_unlocalized_provider_failure_does_not_get_ready(self):
  failure={'state':'SOURCE_UNAVAILABLE','issues':['identity proof unavailable'],'ranges':[]}
  self.assertEqual(self.choose(failure).attrs['symbol'],'OTHER.GTX')
 def test_new_relevant_rejection_changes_selection_without_ohlcv_rewrite(self):
  self.assertEqual(self.choose().attrs['symbol'],'TEST.GTX')
  self.frame.attrs['archive_invalid']={**self.report,'ranges':[['2026-10-01','2026-10-01']],'rejected_timestamps':['2026-10-01']}
  self.assertEqual(self.choose().attrs['symbol'],'OTHER.GTX')

class MonthlyDependency(AppleMonthlyWarmup):
 # Only these NEW methods are selected by the runner; inherited prior PASS tests are not repeated.
 def test_added_daily_prefix_cannot_poison_disjoint_approved_native_warmup(self):
  expected=g.monthly_warmup(self.store,self.info,self.current,self.now)
  old=self.daily.iloc[:1].copy();old.index=pd.DatetimeIndex(['2019-05-23']);old.loc[old.index[0],'high']=0
  self.store.save_series(self.info,old,'1D')
  state=self.store.setting(g.state_key(self.isin,'1D'),{})
  state['archive_invalid']={'state':'INVALID','repairable':True,'identity':self.meta,'ranges':[['2019-05-23','2019-05-23']],'rejected_timestamps':['2019-05-23']}
  self.store.set_setting(g.state_key(self.isin,'1D'),state)
  actual=g.monthly_warmup(self.store,self.info,self.current,self.now)
  pd.testing.assert_frame_equal(expected,actual)
  self.assertEqual(actual.attrs['warmup_dependencies'][1]['from'],'2021-01-01 00:00:00')
  with self.store.connect() as db:self.assertEqual(db.execute("select high from series where base_tf='1D' and ts='2019-05-23 00:00:00'").fetchone()[0],0)
 def test_added_rejection_inside_actual_daily_warmup_remains_blocking(self):
  state=self.store.setting(g.state_key(self.isin,'1D'),{})
  state['archive_invalid']={'state':'INVALID','repairable':True,'identity':self.meta,'ranges':[['2021-08-02','2021-08-02']],'rejected_timestamps':['2021-08-02']}
  self.store.set_setting(g.state_key(self.isin,'1D'),state)
  with self.assertRaisesRegex(ValueError,'DAILY_REFERENCE_UNCONFIRMED'):g.monthly_warmup(self.store,self.info,self.current,self.now)
 def test_missing_daily_dependency_keeps_structured_block(self):
  with patch.object(g,'load',return_value=pd.DataFrame()):
   with self.assertRaisesRegex(ValueError,'DAILY_REFERENCE_UNCONFIRMED'):g.monthly_warmup(self.store,self.info,self.current,self.now)

if __name__=='__main__':
 suite=unittest.TestLoader().loadTestsFromTestCase(DependencyScope)
 for name in ('test_added_daily_prefix_cannot_poison_disjoint_approved_native_warmup','test_added_rejection_inside_actual_daily_warmup_remains_blocking','test_missing_daily_dependency_keeps_structured_block'):suite.addTest(MonthlyDependency(name))
 result=unittest.TextTestRunner(verbosity=2).run(suite);sys.exit(not result.wasSuccessful())
