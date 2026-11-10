import copy,sys,unittest
from pathlib import Path
import pandas as pd
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as dq
from calculation_window import scope_raw

class NativeRejectScope(unittest.TestCase):
 def setUp(self):
  self.now=pd.Timestamp('2026-10-21T07:00Z')
  self.meta=dict(source='gettex_chart',symbol='NVDA.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  idx=dq.expected_index(self.meta,'15m',pd.Timestamp('2026-10-20 07:30'),pd.Timestamp('2026-10-20 22:45'),self.now)
  self.f=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=10.),index=idx)
  self.f.attrs={**self.meta,'timeframe':'15m','classify_absence':True}
  raw=self.f.copy();raw.loc[pd.Timestamp('2026-10-20 23:00')]=[195.34,197.,195.18,197.,564.]
  report=dq.audit(raw,'15m',now=self.now,check_stale=False)
  self.f.attrs.update(archive_invalid={**report,'rejected_timestamps':['2026-10-20 23:00:00']},data_quality=report,partial_chart={'display_only':True})
 def test_excluded_off_session_record_not_a_dependency_and_archive_unchanged(self):
  before=copy.deepcopy(self.f.attrs);out=scope_raw(self.f,'15m',self.now)
  self.assertNotIn('archive_invalid',out.attrs)
  self.assertEqual(out.attrs['archive_quality_before_scope']['state'],'INVALID')
  self.assertEqual(out.attrs['excluded_archive_faults']['state'],'OUTSIDE_SESSION_INPUT')
  self.assertEqual(self.f.attrs,before);pd.testing.assert_frame_equal(out,self.f,check_flags=False)
 def test_real_unknown_gap_remains_unconfirmed(self):
  f=self.f.drop(self.f.index[20]);out=scope_raw(f,'15m',self.now)
  self.assertNotIn('archive_invalid',out.attrs)
  r=dq.audit(out,'15m',now=self.now,check_stale=False)
  self.assertEqual(r['state'],'UNCONFIRMED');self.assertEqual(r['missing_count'],1)
 def test_rejected_in_session_candle_remains_invalid(self):
  f=self.f.copy();f.attrs=copy.deepcopy(self.f.attrs);f.attrs['archive_invalid']['rejected_timestamps']=['2026-10-20 12:00:00']
  self.assertIn('archive_invalid',scope_raw(f,'15m',self.now).attrs)
 def test_wrong_identity_unknown_localization_or_additional_damage_fail_closed(self):
  for mutation in ('identity','empty','unlocalized','price'):
   f=self.f.copy();f.attrs=copy.deepcopy(self.f.attrs);r=f.attrs['archive_invalid']
   if mutation=='identity':r['identity']['currency']='USD'
   elif mutation=='empty':r['rejected_timestamps']=[]
   elif mutation=='unlocalized':r['repairable']=False
   else:r['issues'].append('Невозможные OHLC/цены/объёмы')
   with self.subTest(mutation=mutation):self.assertIn('archive_invalid',scope_raw(f,'15m',self.now).attrs)
 def test_bad_remaining_row_is_still_invalid(self):
  f=self.f.copy();f.loc[f.index[4],'high']=1
  out=scope_raw(f,'15m',self.now)
  self.assertEqual(dq.audit(out,'15m',now=self.now)['state'],'INVALID')
 def test_misalignment_inside_open_session_and_wrong_venue_are_not_excluded(self):
  for stamp,venue in [('2026-10-20 12:01:00','GETTEX'),('2026-10-20 23:00:00','GER')]:
   f=self.f.copy();f.attrs=copy.deepcopy(self.f.attrs)
   f.attrs['archive_invalid']['rejected_timestamps']=[stamp];f.attrs['venue']=venue
   self.assertIn('archive_invalid',scope_raw(f,'15m',self.now).attrs)
 def test_candidate_scopes_only_calculation_and_leaves_display_archive_gate(self):
  import gettex_chart as g
  from types import SimpleNamespace
  empty=pd.DataFrame();store=SimpleNamespace()
  with patch.object(g,'load',return_value=self.f),patch.object(g,'retain_deeper',side_effect=lambda i,t,c,b,f,fb,n:(f,fb)):
   original,_=g.candidate(store,{'isin':'US67066G1040'},'15m',empty,'15m',self.now)
   self.assertTrue(original.empty)
   selected,_=g.candidate(store,{'isin':'US67066G1040'},'15m',empty,'15m',self.now,analysis_at=self.now)
   self.assertEqual(len(selected),len(self.f));self.assertNotIn('archive_invalid',selected.attrs)
 def test_stale_price_remains_stale_after_exclusion(self):
  out=scope_raw(self.f,'15m',self.now)
  self.assertGreater(out.attrs['data_quality'].get('stale_count',0),0)
 def test_derivatives_keep_real_missing_intervals(self):
  from analytics import resample
  f=self.f.drop(self.f.index[20]);out=scope_raw(f,'15m',self.now)
  for tf in ('30m','1H','2H','3H','4H'):
   derived=resample(out,'15m',tf,self.now)
   with self.subTest(tf=tf):
    self.assertFalse(derived.empty)
    self.assertNotEqual(derived.attrs['data_quality']['state'],'READY')

if __name__=='__main__':unittest.main()
