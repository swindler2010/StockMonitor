import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
import pandas as pd
import data_quality as dq
from snapshot_worker import SnapshotContext,Value

class ReviewedEntrySource(unittest.TestCase):
 def run_context(self,approved=True,wrong=None):
  meta=dict(source='gettex_chart',symbol='NVD.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  c=SnapshotContext();c.analysis_at=pd.Timestamp('2026-11-04T10:00Z');c.timeframe=Value('15m');c.period=Value('Max')
  c.store=NS(setting=lambda k,d=None:d,read_cache=NS(depend=lambda *a:None,dependencies=lambda **kw:{},get=lambda k,f,**kw:f()))
  c.user_state=lambda i:{'source_mode':'proxy','benchmark_id':'UNREGISTERED','history_currency':'EUR'};c.source_mode=lambda i:'proxy'
  def frame(i,tf,*args):
   f=pd.DataFrame(dict(open=[100.],high=[102.],low=[99.],close=[101.],volume=[10.]),index=pd.DatetimeIndex(['2026-11-03 07:30' if tf!='1D' else '2026-11-03']))
   f.attrs={**meta,'timeframe':tf}
   if wrong and tf=='4H':f.attrs.update(wrong)
   return f
  c.frame_for=frame;c.frame_for_entry=frame
  def audit(frames,tf,state,*a,**kw):
   issues=[]
   for t,f in frames.items():issues+=dq.audit(f,t,expected=state['expected_identity'],now=c.analysis_at,check_stale=False).get('issues',[])
   return {'status':'DATA BLOCKED' if issues else 'READY','issues':issues,'expected':state['expected_identity'],'warnings':[]}
  with patch('cp_controls.initialization_policy_for',return_value=None),patch('cp_controls.reviewed_entry_identity',return_value=meta if approved else None),patch('analytics.analyse_multiframe',side_effect=audit):
   return c._trading_analysis({'isin':'TEST','name':'Test'})
 def test_reviewed_provider_is_expected_instead_of_proxy(self):
  r=self.run_context();self.assertEqual(r['status'],'READY');self.assertEqual(r['expected']['source'],'gettex_chart')
 def test_unapproved_or_revoked_pin_does_not_override_preference(self):
  r=self.run_context(False);self.assertEqual(r['status'],'DATA BLOCKED');self.assertEqual(r['expected']['source'],'proxy')
 def test_wrong_dependent_source_currency_or_venue_still_block(self):
  for change in ({'source':'proxy'},{'currency':'USD'},{'venue':'Frankfurt'}):
   with self.subTest(change=change):self.assertEqual(self.run_context(wrong=change)['status'],'DATA BLOCKED')

if __name__=='__main__':unittest.main()
