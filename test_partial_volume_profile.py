"""Visual profile uses verified partial rows, never changes CORE/DQ."""
import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
from chart_indicators import partial_indicators,volume_profile_rows,volume_profile


class PartialVolumeProfile(unittest.TestCase):
 def frame(self,tf='15m'):
  f=pd.DataFrame(dict(open=[10.,11.,12.],high=[11.,12.,13.],low=[9.,10.,11.],close=[10.,11.,12.],volume=[2.,3.,5.]),index=pd.date_range('2026-11-10 07:30',periods=3,freq='15min'))
  f.attrs=dict(source='gettex_chart',symbol='TEST.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin',isin='TEST',timeframe=tf,
      partial_chart={'display_only':True,'break_before':[str(f.index[2])]},data_quality={'state':'UNCONFIRMED','issues':['real gap']})
  return f
 def test_all_eleven_tf_profile_survives_partial_archive_without_permission(self):
  from timeframes import LABELS
  for tf in LABELS:
   f=self.frame(tf);before=copy.deepcopy(f);out=partial_indicators(f)
   rows=volume_profile_rows(out,False);p,v,w=volume_profile(rows)
   self.assertAlmostEqual(float(v.sum()),10.);self.assertEqual(out.attrs['data_quality'],before.attrs['data_quality'])
   self.assertFalse(out.attrs['partial_indicators']['execution_allowed']);pd.testing.assert_frame_equal(f,before)
 def test_invalid_and_incomplete_rows_are_not_hidden_in_histogram(self):
  f=self.frame();f.loc[f.index[1],'close']=99.;f['bar_complete']=[True,True,False]
  out=partial_indicators(f);rows=volume_profile_rows(out,False)
  self.assertEqual(list(rows.index),[f.index[0]]);self.assertAlmostEqual(float(volume_profile(rows)[1].sum()),2.)
 def test_unknown_identity_and_missing_validation_remain_unavailable(self):
  f=self.frame();f.attrs['currency']='USD';out=partial_indicators(f)
  self.assertIsNone(volume_profile_rows(out,False));self.assertIsNone(volume_profile_rows(self.frame(),False))
 def test_full_valid_and_visible_slice_preserve_original_volume(self):
  f=self.frame();self.assertIs(volume_profile_rows(f,True),f)
  out=partial_indicators(f);rows=volume_profile_rows(out.iloc[1:],False)
  self.assertAlmostEqual(float(volume_profile(rows)[1].sum()),8.)


if __name__=='__main__':unittest.main()
