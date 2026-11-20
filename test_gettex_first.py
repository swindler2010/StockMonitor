"""Issue20: factual calculation and CP opinion are independent."""
import copy,unittest
import test_trading_engine as fixtures
from analytics import _safe_features
from gettex_first import calculation_allowed
from trading_engine import evaluate

class GettexFirstTests(unittest.TestCase):
 def frame(self):
  cls=next(c for c in vars(fixtures).values() if isinstance(c,type) and hasattr(c,'market_frames'))
  f=cls().market_frames()['1H'];f['source']='gettex'
  f.attrs.update(source='gettex',symbol='US0378331005',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  return f
 def test_warnings_preserved_and_identical_indicators_for_real_bars(self):
  f=self.frame();info={'isin':'US0378331005'};now='2026-02-01T00:00Z'
  base_x,base=_safe_features(f,info,now)
  for state in ('GAP','UNCONFIRMED','INVALID','STALE','SOURCE_GAP','RECOVERING'):
   with self.subTest(state=state):
    f.attrs['data_quality']={'state':state,'ranges':[['2026-01-01','2026-01-01']]}
    before=copy.deepcopy(f.attrs);x,r=_safe_features(f,info,now)
    self.assertTrue(calculation_allowed(r));self.assertEqual(r['facts'],base['facts'])
    self.assertTrue(x.ema30.equals(base_x.ema30));self.assertEqual(f.attrs,before)
    self.assertEqual(r['data_quality']['state'],state)
    self.assertFalse(r['calculation_availability']['execution_permission'])
 def test_bad_values_missing_bars_and_wrong_listing_remain_unavailable(self):
  for kind in ('bad','empty','short','currency','isin','provider','row_source'):
   f=self.frame();info={'isin':'US0378331005'};f.attrs['data_quality']={'state':'UNCONFIRMED'}
   if kind=='bad':f.iloc[-1,f.columns.get_loc('high')]=0
   if kind=='empty':f=f.iloc[:0]
   if kind=='short':f=f.tail(10)
   if kind=='currency':f.attrs['currency']='USD'
   if kind=='isin':info['isin']='WRONG'
   if kind=='provider':f.attrs['source']='proxy'
   if kind=='row_source':f.iloc[0,f.columns.get_loc('source')]='yahoo'
   _,r=_safe_features(f,info,'2026-02-01T00:00Z')
   self.assertFalse(calculation_allowed(r),kind)
 def test_core_uses_calculation_availability_and_keeps_strategy_rules(self):
  f=self.frame();f.attrs['data_quality']={'state':'UNCONFIRMED'}
  _,r=_safe_features(f,{'isin':'US0378331005'},'2026-02-01T00:00Z')
  contexts={tf:copy.deepcopy(r) for tf in ('1H','4H','1D')}
  self.assertEqual(evaluate(contexts,'1H')['status'],'READY')
  contexts['4H']['ohlcv_valid']=False
  self.assertEqual(evaluate(contexts,'1H')['status'],'DATA BLOCKED')

if __name__=='__main__':unittest.main()
