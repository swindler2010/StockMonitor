import copy,sys,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import chart_worker
from performance_cache import InputRevisionChanged
from test_partial_indicators import frame

class PartialIndependentCore(unittest.TestCase):
 def run_prepare(self,engine=None,approved=True,error=None):
  f=frame();original=copy.deepcopy(f);ctx=Mock()
  ctx.instrument_cfg.return_value={'isin':'TEST'};ctx.user_state.return_value={};ctx.frame_for.return_value=f
  ctx.trading_analysis.return_value=engine;ctx.trading_analysis.side_effect=error
  store=NS(setting=lambda *a:{})
  with patch.object(chart_worker,'SnapshotContext',return_value=ctx),patch('partial_chart.select_display',return_value=f),patch('cp_controls.initialization_policy_for',return_value={'analysis_timeframes':['15m']} if approved else None),patch('cp_controls.reviewed_entry_identity',return_value=None):
   result=chart_worker.prepare(store,{'timeframe':'15m'})
  self.assertEqual(f.attrs,original.attrs)
  self.assertFalse(result['analysis'][1]['ohlcv_valid'])
  self.assertEqual(result['frame'].attrs['data_quality'],original.attrs['data_quality'])
  ctx.analyse_cached.assert_not_called()
  return result,ctx
 def test_approved_independent_core_does_not_clear_chart_dq(self):
  engine={'status':'READY','signal':'WAIT','blockers':[],'benchmark_context':{'state':'UNAVAILABLE'}}
  r,c=self.run_prepare(engine);self.assertEqual(r['trading_engine'],engine)
  c.trading_analysis.assert_called_once_with({'isin':'TEST'},analysis_only=True)
 def test_actual_core_block_and_fault_do_not_enable_display_archive(self):
  engine={'status':'DATA BLOCKED','signal':'WAIT','blockers':['STALE / IDENTITY']}
  r,_=self.run_prepare(engine);self.assertEqual(r['trading_engine'],engine)
  r,_=self.run_prepare(error=ValueError('bad inputs'));self.assertEqual(r['trading_engine']['status'],'DATA BLOCKED')
 def test_no_policy_no_independent_evaluation(self):
  r,c=self.run_prepare(approved=False);c.trading_analysis.assert_not_called();self.assertEqual(r['trading_engine']['status'],'DATA BLOCKED')
 def test_revision_change_rejected_instead_of_published(self):
  with self.assertRaises(InputRevisionChanged):self.run_prepare(error=InputRevisionChanged('changed'))

if __name__=='__main__':unittest.main()
