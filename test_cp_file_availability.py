"""No source file is not evidence of absent EUR trades or NO_TRADES."""
import gzip,sys,unittest
from pathlib import Path
from unittest.mock import MagicMock,Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
import gettex_history as history

class FileAvailability(unittest.TestCase):
 def run_empty(self,file_stamp):
  store=Mock();store.directory=MagicMock();store.setting.return_value=None
  cache=store.directory.__truediv__.return_value;cache.glob.return_value=[]
  cache.__truediv__.return_value.exists.return_value=False
  name='posttrade.'+file_stamp+'.mund.csv.gz'
  html=('<a href="https://erdk.bayerische-boerse.de:8000/files/'+name+'">file</a>').encode()
  http=Mock(side_effect=lambda url:html if url==history.PAGE else gzip.compress(b'test-only raw data'))
  parse=Mock(return_value=pd.DataFrame())
  with patch('delivery_observations.outcome') as outcome:
   with self.assertRaises(ValueError) as error:
    history._download('US0378331005',http,parse,store,pd.Timestamp('2026-10-12T08:00Z'),pd.Timestamp('2026-10-12T08:15Z'))
  return str(error.exception),http,parse,outcome
 def test_out_of_window_files_report_missing_coverage_without_trade_claim(self):
  reason,http,parse,outcome=self.run_empty('20261120.08.15')
  self.assertTrue(reason.startswith('SOURCE_LIMITED:'));self.assertNotIn('нет сделок',reason)
  self.assertNotIn('NO_TRADES',reason);parse.assert_not_called();self.assertEqual(http.call_count,1)
 def test_present_file_without_symbol_rows_is_not_called_absent_file(self):
  reason,http,parse,outcome=self.run_empty('20261012.08.15')
  self.assertNotIn('SOURCE_LIMITED',reason);self.assertIn('нет сделок EUR',reason)
  parse.assert_called_once();self.assertEqual(http.call_count,2)

if __name__=='__main__':unittest.main()

