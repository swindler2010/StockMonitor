"""Publication grace applies only to a closed bar still absent from the input."""
import sys,unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from calculation_window import context_deadline

class ObservedDeadline(unittest.TestCase):
 def test_loaded_hour_uses_next_boundary_missing_hour_keeps_publication_grace(self):
  meta=dict(source='gettex',symbol='TEST',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  now=pd.Timestamp('2026-11-25T08:44Z')
  self.assertEqual(context_deadline(meta,'1H',now,pd.Timestamp('2026-11-25T08:30Z')),pd.Timestamp('2026-11-25T09:30Z'))
  self.assertEqual(context_deadline(meta,'1H',now,pd.Timestamp('2026-11-25T07:30Z')),pd.Timestamp('2026-11-25T08:45Z'))
  self.assertEqual(context_deadline(meta,'1H',now),pd.Timestamp('2026-11-25T08:45Z'))
 def test_daily_loaded_close_skips_grace_but_not_the_next_possible_session_close(self):
  meta=dict(source='proxy',symbol='^GSPC',venue='SNP',currency='USD',timezone='America/New_York')
  now=pd.Timestamp('2026-11-24T21:10Z')
  self.assertEqual(context_deadline(meta,'1D',now,pd.Timestamp('2026-11-24T21:00Z')),pd.Timestamp('2026-11-25T21:00Z'))
  self.assertEqual(context_deadline(meta,'1D',now,pd.Timestamp('2026-11-23T21:00Z')),pd.Timestamp('2026-11-24T21:30Z'))
 def test_future_or_unzoned_observation_never_extends_a_permission(self):
  meta=dict(venue='GETTEX',timezone='Europe/Berlin');now=pd.Timestamp('2026-11-25T08:44Z')
  for value in ('2026-11-25T08:45Z','2026-11-25 08:30'):
   self.assertEqual(context_deadline(meta,'1H',now,value),now+pd.Timedelta(seconds=60))

if __name__=='__main__':unittest.main()
