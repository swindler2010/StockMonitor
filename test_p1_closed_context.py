import sys,unittest,copy
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as dq
from analytics import resample
from calculation_window import bounded_features
class ClosedContext(unittest.TestCase):
 def setUp(self):
  self.now=pd.Timestamp('2026-10-26T18:00Z')
  self.meta=dict(source='proxy',symbol='TEST.DE',venue='GER',currency='EUR',timezone='Europe/Berlin')
  self.info={'isin':'TEST','eur_listings':[dict(self.meta,isin='TEST',status='CONFIRMED')]}
  idx=dq.expected_index(self.meta,'1D',pd.Timestamp('2018-01-01'),pd.Timestamp('2026-10-26'),self.now)
  self.daily=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=100.,source='proxy'),index=idx)
  self.daily.attrs={**self.meta,'timeframe':'1D','data_quality':{'state':'READY'}}
 def check(self,tf):
  f=resample(self.daily,'1D',tf,self.now);original=copy.deepcopy(f.attrs)
  self.assertEqual(f.attrs['data_quality']['state'],'READY')
  r=bounded_features(self.info,f,tf,self.now)
  self.assertTrue(r['analysis_ready'],r);self.assertEqual(f.attrs,original)
  self.assertLess(r['count'],len(f))
 def test_forming_week_does_not_invalidate_proven_closed_reference(self):self.check('1W')
 def test_forming_month_does_not_invalidate_proven_closed_reference(self):self.check('1M')
 def test_missing_closed_session_still_blocks(self):
  d=self.daily.drop(self.daily.index[-10]);f=resample(d,'1D','1W',self.now)
  self.assertFalse(bounded_features(self.info,f,'1W',self.now)['analysis_ready'])
 def test_unconfirmed_or_invalid_upstream_still_blocks(self):
  for state in ('UNCONFIRMED','INVALID','SOURCE_UNAVAILABLE'):
   d=self.daily.copy();d.attrs['data_quality']={'state':state}
   f=resample(d,'1D','1W',self.now);self.assertFalse(bounded_features(self.info,f,'1W',self.now)['analysis_ready'])
 def test_only_forming_history_has_explicit_reason(self):
  f=resample(self.daily.tail(1),'1D','1M',self.now)
  r=bounded_features(self.info,f,'1M',self.now)
  self.assertEqual(r['blockers'],['NO_CLOSED_BARS']);self.assertFalse(r['analysis_ready'])
if __name__=='__main__':unittest.main()
