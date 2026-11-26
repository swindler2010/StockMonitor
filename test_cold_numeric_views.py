"""Numerical gates and levels ignore unused metadata without losing it."""
import sys,unittest
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from history_backfill import validate,FIELDS
from significant_levels import zones

class NoClone:
    def __deepcopy__(self,memo):raise AssertionError('Archive evidence copied during numeric work')

def frame():
    x=np.arange(120);close=100+np.sin(x/5)*4+x/40
    f=pd.DataFrame(dict(open=close-.2,high=close+1,low=close-1,close=close,volume=np.ones(120)),index=pd.date_range('2026-01-01',periods=120,freq='h'))
    f.attrs=dict(source='gettex_chart',symbol='TEST',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
    return f

class NumericViews(unittest.TestCase):
    def test_53_200_checks_preserve_rows_metadata_and_zone_results_without_evidence_copies(self):
        clean=frame();expected=zones(clean);self.assertTrue(expected)
        numeric=clean.to_numpy(copy=True);index=clean.index.copy();proof=NoClone()
        clean.attrs['archive_evidence']={'large_unchanged_proof':proof}
        for count in (53,200):
            for _ in range(count):validate(clean)
            self.assertEqual(zones(clean),expected)
        np.testing.assert_array_equal(clean.to_numpy(),numeric)
        self.assertTrue(clean.index.equals(index));self.assertIs(clean.attrs['archive_evidence']['large_unchanged_proof'],proof)

    def test_invalid_numeric_identity_and_time_checks_still_reject(self):
        cases=[('nonfinite',lambda f:f.__setitem__('volume',np.nan)),
               ('negative',lambda f:f.__setitem__('close',-1)),
               ('impossible',lambda f:f.__setitem__('high',1)),
               ('identity',lambda f:f.attrs.pop('currency')),
               ('duplicate',lambda f:setattr(f,'index',pd.DatetimeIndex([f.index[0]]*len(f)))),
               ('order',lambda f:setattr(f,'index',f.index[::-1]))]
        for label,change in cases:
            with self.subTest(label=label):
                f=frame();change(f);f.attrs['archive_evidence']=NoClone()
                with self.assertRaises(ValueError):validate(f)

    def test_all_timeframes_and_missing_values_leave_levels_numerically_identical(self):
        for tf in ('5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M'):
            f=frame();f.loc[f.index[10],'close']=np.nan;f.attrs['timeframe']=tf
            expected=zones(f);f.attrs['archive_evidence']=NoClone()
            self.assertEqual(zones(f),expected)

if __name__=='__main__':unittest.main()
