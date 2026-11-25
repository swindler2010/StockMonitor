import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from compute_process import diagnostic_error
class ErrorStageTests(unittest.TestCase):
    def test_nested_guard_location_preserves_reason_without_locals(self):
        secret='DO_NOT_LOG_PRIVATE_LOCAL'
        def input_guard():raise RuntimeError('Stale Trading Engine input dependency: market:A:15m 1->2')
        try:input_guard()
        except RuntimeError as error:result=diagnostic_error(error)
        self.assertIn('market:A:15m 1->2',result)
        self.assertIn('test_snapshot_error_stage.py:input_guard:',result)
        self.assertNotIn(secret,result)
        self.assertNotIn(str(Path(__file__).parent),result)
    def test_bounded_trace(self):
        def nested(n):
            if n:return nested(n-1)
            raise RuntimeError('blocked')
        try:nested(20)
        except RuntimeError as error:result=diagnostic_error(error)
        self.assertEqual(result.split('[stage=')[1].count('>'),7)
if __name__=='__main__':unittest.main()
