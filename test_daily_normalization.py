import sys,unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from history_service import normalize_daily
from data_quality import audit

class Tests(unittest.TestCase):
    def frame(self):
        f=pd.DataFrame({'open':[10.,10.,10.,11.],'high':[10.,10.,10.,12.],'low':[10.,10.,10.,10.],'close':[10.,10.,10.,11.],'volume':[100.,0.,0.,200.]},index=pd.to_datetime(['2001-04-12','2001-04-13','2001-04-16','2001-04-17']))
        f.attrs=dict(source='proxy',venue='GER',currency='EUR',symbol='ANY.DE',timezone='Europe/Berlin')
        return f
    def test_only_holiday_clones_excluded_and_diagnosed(self):
        original=self.frame();out=normalize_daily(original)
        self.assertEqual(len(original),4);self.assertEqual(len(out),2)
        self.assertEqual(out.attrs['daily_normalization']['count'],2)
        self.assertEqual(audit(out,'1D',check_stale=False)['state'],'READY')
    def test_real_trade_or_different_quote_on_closed_day_still_invalid(self):
        for col,value in [('volume',1),('close',11)]:
            frame=self.frame();frame.loc[frame.index[1],col]=value
            out=normalize_daily(frame)
            self.assertIn(frame.index[1],out.index)
            self.assertEqual(audit(out,'1D',check_stale=False)['state'],'INVALID')
    def test_zero_volume_trading_day_is_preserved(self):
        frame=self.frame();frame.loc[frame.index[0],'volume']=0
        self.assertIn(frame.index[0],normalize_daily(frame).index)
    def test_impossible_ohlc_is_not_repaired_by_normalization(self):
        frame=self.frame();frame.loc[frame.index[-1],'close']=13
        self.assertEqual(audit(normalize_daily(frame),'1D',check_stale=False)['state'],'INVALID')
    def test_unknown_calendar_and_duplicates_remain_for_dq(self):
        frame=self.frame();frame.attrs['venue']='UNKNOWN'
        self.assertEqual(len(normalize_daily(frame)),4)
        frame=self.frame();frame=pd.concat([frame,frame.iloc[:1]])
        self.assertEqual(len(normalize_daily(frame)),5)

if __name__=='__main__':unittest.main()
