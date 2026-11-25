"""Integration checks for PR25; synthetic inputs, no live database or orders."""
import sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import numpy as np
import pandas as pd
import i18n,paper_view
import paper_v2_30m_features as features

class AppliedV2(unittest.TestCase):
    def test_zero_atr_is_excluded_without_floating_point_failure_and_positive_data_still_works(self):
        index=pd.date_range('2026-11-23T08:00:00Z',periods=12,freq='30min')
        grid=pd.DataFrame(dict(day=['2026-11-23']*12,minutes=np.arange(12)*30+180),index=index)
        settings=SimpleNamespace(FEE=2.,HALF_SPREAD=.001,HALT_DAYS=(),base=SimpleNamespace(MAX_SHARES=10))
        for atr in (0.,1.):
            tapes={code:SimpleNamespace(bars=pd.DataFrame(dict(close=np.arange(12)*.1+100,open=100.,high=102.,low=99.,ret=.001,atr=atr,vol_ratio=1.,risk_move=.01),index=index)) for code in ['TEST']+['P'+str(i) for i in range(12)]}
            with np.errstate(divide='raise',invalid='raise'):
                result=features.sample_features(settings,grid,tapes)
            self.assertEqual(result.empty,atr==0)
            self.assertTrue(result.empty or np.isfinite(result[list(features.FEATURES)].to_numpy()).all())

    def test_siemens_energy_uses_the_same_verified_one_quarter_extension(self):
        import test_paper_v2_30m as fixtures
        case=fixtures.Lock75Extension(methodName='test_extends_once_and_keeps_retry_stable')
        case.setUp()
        try:
            code='DE000ENER6Y0'
            case.db.execute('UPDATE candles SET isin=?',(code,))
            case.code=code;case.entry['isin']=code
            case.result['execution_quote']['isin']=code
            case.result['execution_quote']['identity']['symbol']=code
            case.test_extends_once_and_keeps_retry_stable()
        finally:case.doCleanups()

    def test_daily_rows_follow_selected_language_and_preserve_day_amount_and_codes(self):
        row=dict(day='2026-11-24',status='LOSS',buys=1,sells=1,net='-1.23',profit_lock=False,round_trips=1,reasons=[])
        for language,title,loss in [('ru','Последние сессии','убыток'),('de','Letzte Sitzungen','Verlust'),('en','Recent sessions','loss')]:
            with patch.object(i18n,'_language',language):
                text=paper_view.v2_daily_summary(dict(v2_daily_sessions=[row]))
                self.assertIn(title,text);self.assertIn(loss,text)
                self.assertIn('2026-11-24',text);self.assertIn('-1.23 EUR',text)
                self.assertEqual(paper_view.v2_daily_summary({}),'')
                current=dict(row,status='IN_PROGRESS',profit_lock=True,round_trips=0,reasons=[('V2_WAIT_CODE',2)])
                self.assertIn('V2_WAIT_CODE',paper_view.v2_daily_summary(dict(v2_daily_sessions=[current])))

if __name__=='__main__':unittest.main()
