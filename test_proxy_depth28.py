import unittest
from unittest.mock import patch
import pandas as pd
import data_quality as dq
import gettex_chart as chart
from desktop014 import Desktop014
import test_proxy_session28 as fixture

class PreserveDepth(unittest.TestCase):
 def setUp(self):
  self.f=fixture.ProxySession('runTest');self.f.setUp()
  self.addCleanup(self.f.tearDown)
  self.s,self.info,self.ctx=self.f.s,self.f.info,self.f.ctx
 def old_history(self,base='1H'):
  idx=dq.expected_index(self.f.p,base,'2026-09-01','2026-10-27',self.f.now)
  f=pd.DataFrame(dict(open=900.,high=902.,low=899.,close=901.,volume=10.),index=idx)
  f.attrs=dict(self.f.p);self.s.save_series(self.info,f,base)
  return f
 def test_primary_gettex_keeps_deeper_reserve_in_sqlite(self):
  self.old_history()
  for tf in ('1H','2H','3H','4H'):
   out=Desktop014._frame_uncached(self.ctx,self.info,tf)
   self.assertEqual(out.attrs['source'],'gettex')
   self.assertTrue(out.close.eq(101).all())
   reserve=self.s.load_series(self.info['isin'],'1H','proxy')
   self.assertLess(reserve.index.min(),pd.Timestamp('2026-10-01'))
   self.assertTrue(reserve.close.eq(901).all())
 def test_30m_uses_requested_tf_depth_not_15m_row_count(self):
  self.old_history('15m')
  out=Desktop014._frame_uncached(self.ctx,self.info,'30m')
  self.assertEqual(out.attrs['venue'],'GETTEX');self.assertTrue(out.close.eq(101).all())
  self.assertLess(self.s.load_series(self.info['isin'],'15m','proxy').index.min(),pd.Timestamp('2026-10-01'))
 def test_new_chart_source_cannot_replace_longer_same_venue_series(self):
  meta=dict(source='gettex',symbol=self.info['isin'],venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  idx=dq.expected_index(meta,'15m','2026-10-23','2026-10-27',self.f.now)
  old=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=10.),index=idx);old.attrs=dict(meta)
  new=old.loc['2026-10-27':].copy();new.attrs=dict(chart.identity(self.info['isin'],'SAPG.GTX'))
  new.attrs['listing_proof']=dict(new.attrs,isin=self.info['isin'],status='CONFIRMED')
  # A more recent tail cannot pay for lost preceding sessions.
  prior=old.iloc[:-8].copy()
  with patch.object(chart,'load',return_value=new):selected,base=chart.candidate(self.s,self.info,'1H',prior,'15m',self.f.now)
  self.assertEqual(selected.index.min(),prior.index.min());self.assertEqual(selected.attrs['source'],'gettex')
 def test_bad_or_foreign_history_not_promoted_and_stale_stays_stale(self):
  old=self.old_history();new=old.iloc[-8:].copy()
  for altered in (dict(self.f.p,currency='USD'),dict(self.f.p,venue='UNCONFIRMED')):
   invalid=old.copy();invalid.attrs=altered
   chosen,_=chart.retain_deeper(self.info,'1H',invalid,'1H',new,'1H',self.f.now)
   self.assertIs(chosen,new)
  bad=old.copy();bad.iloc[0,bad.columns.get_loc('high')]=1
  chosen,_=chart.retain_deeper(self.info,'1H',bad,'1H',new,'1H',self.f.now)
  self.assertIs(chosen,new)
  selected,_=chart.retain_deeper(self.info,'1H',old.iloc[:-9],'1H',new,'1H',self.f.now)
  self.assertNotEqual(dq.audit(selected,'1H',now=self.f.now)['state'],'READY')

if __name__=='__main__':unittest.main()
