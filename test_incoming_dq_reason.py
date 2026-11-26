import unittest,sys,copy
from pathlib import Path
from unittest.mock import Mock
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as q
from test_data_quality import frame,NOW,META

class Tests(unittest.TestCase):
    def check_rejected(self,replacement,expected):
        f=frame();f.loc[f.index[8],'high']=1
        f=f.drop(f.index[2]);f.attrs=META.copy()
        report=q.audit(f,'1D',META,NOW);before=f.copy(deep=True);original=copy.deepcopy(report)
        with self.assertRaises(q.QualityError) as caught:
            q.repair_incoming({'isin':'A'},Mock(),'AAPL','1D',f,Mock(return_value=replacement),report,NOW)
        result=caught.exception.report
        self.assertEqual(result['state'],'INVALID')
        for key in ('identity','ranges','issues','missing_count'):
            self.assertEqual(result[key],original[key])
        self.assertEqual(result['recovery_report']['state'],expected)
        self.assertEqual(report,original);pd.testing.assert_frame_equal(f,before)
    def test_empty_gap_retry_does_not_hide_bad_ohlc(self):self.check_rejected(pd.DataFrame(),'SOURCE_UNAVAILABLE')
    def test_wrong_currency_retry_remains_invalid(self):
        f=frame();f.attrs={**META,'currency':'EUR'};self.check_rejected(f,'INVALID')
    def test_malformed_retry_retains_original_ranges(self):
        f=frame();f.iloc[0,f.columns.get_loc('volume')]=-1;self.check_rejected(f,'INVALID')
if __name__=='__main__':unittest.main()
