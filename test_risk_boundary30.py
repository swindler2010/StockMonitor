"""Only the newly fixed leading boundary; reused existing isolated fixture."""
from pathlib import Path
import sys, unittest, copy, os
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
if os.getenv('CP_BOUNDARY_CANDIDATE'):
 sys.path.insert(0,str(Path(__file__).parent/'history64-evidence/alternate30/candidate'))
import cp_controls as cp
from test_cp_accepted_analysis import AcceptedAnalysis
import data_quality as dq
import analytics as a
import calculation_window as cw
import pandas as pd

class Boundary(unittest.TestCase):
 def setUp(self):
  self.fx=AcceptedAnalysis();self.fx.setUp()
  f=self.fx.raw['1H'].copy()
  # Begin at a true session opening and lose an internal child of its first
  # aggregate. Subsequent complete real buckets remain calculable.
  opening=next(t for t in f.index if t.hour==9)
  f=f.loc[f.index>=opening].copy();f=f.drop(opening+pd.Timedelta(hours=1))
  f.attrs.update(intraday_span=[str(f.index.min()),str(f.index.max())])
  f.attrs['data_quality']=dq.audit(f,'1H',now=self.fx.now)
  self.fx.raw['1H']=f
  self.fx.store.set_setting(dq.key(self.fx.info,self.fx.meta['symbol'],'1H'),f.attrs['data_quality'])
 def tearDown(self):self.fx.tearDown()
 def test_leading_gap_no_longer_cancels_reviewed_real_core(self):
  self.fx.grant();f=self.fx.frame('4H');risk=f.attrs.get('accepted_risk',{})
  self.assertTrue(risk.get('approved'),{'dq':f.attrs.get('data_quality'),'module':cp.__file__})
  self.assertTrue(risk['unestimated_leading_intervals'])
  self.assertTrue(all(pd.Timestamp(t)<f.index[0] for t in risk['unestimated_leading_intervals']))
  self.assertTrue(all(pd.Timestamp(e['timestamp'])>f.index[0] for e in risk['price_estimates']))
  _,v=a._safe_features(f,self.fx.info,self.fx.now,prove_scope=True)
  self.assertTrue(v['sufficient_history'],v);self.assertTrue(v['scope_initialization']['stable'])
  self.assertEqual(self.fx.core()['status'],'READY')
  self.assertNotEqual(f.attrs['data_quality']['state'],'READY')
 def test_no_grant_and_new_internal_gap_still_block(self):
  self.assertNotIn('accepted_risk',self.fx.frame('4H').attrs)
  self.fx.grant();f=self.fx.raw['1H'].drop(self.fx.raw['1H'].index[100])
  self.assertNotIn('accepted_risk',self.fx.frame('4H',f).attrs)
 def test_raw_rows_and_quality_remain_unchanged(self):
  self.fx.grant();old=copy.deepcopy(self.fx.raw['1H'])
  report=copy.deepcopy(self.fx.store.setting(dq.key(self.fx.info,self.fx.meta['symbol'],'1H')))
  f=self.fx.frame('4H');ordinary=a.resample(cw.scope_raw(old,'1H',self.fx.now),'1H','4H',self.fx.now)
  pd.testing.assert_frame_equal(f,ordinary);pd.testing.assert_frame_equal(old,self.fx.raw['1H'])
  self.assertEqual(old.attrs,self.fx.raw['1H'].attrs)
  self.assertEqual(report,self.fx.store.setting(dq.key(self.fx.info,self.fx.meta['symbol'],'1H')))
 def test_missing_right_neighbour_still_rejected(self):
  f=self.fx.raw['1H'];tail=f.index[-1]+pd.Timedelta(hours=1)
  # Choose a known session bar after this stored tail, at a later as-of time.
  now=self.fx.now+pd.Timedelta(days=1)
  grid=dq.expected_index(f.attrs,'1H',tail,tail+pd.Timedelta(days=1),now)
  self.assertTrue(len(grid))
  with self.assertRaisesRegex(ValueError,'CLOSED_NEIGHBOURS'):
   cp.accepted_close_estimates(f,{'ranges':[[str(grid[0]),str(grid[0])]]},now)
 def test_real_row_never_replaced_and_open_next_bar_not_used(self):
  f=self.fx.raw['1H'];at=f.index[5]
  with self.assertRaisesRegex(ValueError,'CANNOT_REPLACE_REAL_ROW'):
   cp.accepted_close_estimates(f,{'ranges':[[str(at),str(at)]]},self.fx.now)
  missing=f.index[-2];short=f.drop(missing)
  now=a.bar_ends(f)[-1]-pd.Timedelta(seconds=1)
  with self.assertRaisesRegex(ValueError,'CLOSED_NEIGHBOURS'):
   cp.accepted_close_estimates(short,{'ranges':[[str(missing),str(missing)]]},now)
 def test_short_warmup_and_changed_identity_remain_blocked(self):
  self.fx.grant();f=self.fx.frame('4H').tail(20).copy()
  _,v=a._safe_features(f,self.fx.info,self.fx.now,prove_scope=True)
  self.assertFalse(v['sufficient_history'])
  f=self.fx.raw['1H'].copy();f.attrs['currency']='USD'
  self.assertNotIn('accepted_risk',self.fx.frame('4H',f).attrs)

if __name__=='__main__':unittest.main()
