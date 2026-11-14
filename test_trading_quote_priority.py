"""Execution quote lanes stay fresh without making 5m a CORE dependency."""
import copy,json,unittest
from unittest.mock import patch
import test_paper_hourly as fixture
import candle_schedule as schedule
import pandas as pd


class QuotePriority(unittest.TestCase):
 def setUp(self):
  self.f=fixture.HourlyTests();self.f.setUp();self.addCleanup(self.f.tearDown)
  from contextlib import contextmanager
  f=self.f
  class Store:
   @contextmanager
   def connect(self):
    with f.db:yield f.db
   def setting(self,k,d=None):return fixture.hourly.read(f.db,k,d)
  self.store=Store();self.now=pd.Timestamp(self.f.now,unit='s',tz='UTC')
  self.f.write('gettex_chart:'+self.f.isin+':5m',dict(identity=self.f.identity,received_at='2026-11-04T08:00Z'))
  self.f.write('gettex_chart:'+self.f.isin+':15m',dict(identity=self.f.identity,received_at='2026-11-04T09:30Z'))
 def planned(self):
  with patch('eur_listings.stored_identity',return_value=self.f.identity),patch('gettex_chart.confirmed_state',side_effect=lambda i,v:bool(v.get('identity'))),patch.object(schedule,'decision',side_effect=lambda s,i,m,b,n:dict(due=m['source']=='gettex_chart' and b in ('5m','15m'),session_open=True)):
   return schedule.fresh_candidate(self.store,[self.f.info],self.now)[0]
 def test_stale_5m_quote_is_prioritized_without_changing_required_analysis_tf(self):
  item=self.planned();self.assertEqual(item['_cp_focus']['timeframe'],'5m')
  self.assertEqual(item['_cp_focus']['required_tf'],['15m','1H','4H','1D'])
  self.assertFalse(item['_cp_focus']['execution_permission'])
  from trading_engine import profile
  self.assertNotIn('5m',profile('1H'))
 def test_different_venue_quote_does_not_inherit_priority(self):
  self.f.write('gettex_chart:'+self.f.isin+':5m',dict(identity=dict(self.f.identity,venue='FRA'),received_at='2026-11-04T08:00Z'))
  self.assertEqual(self.planned()['_cp_focus']['timeframe'],'15m')
 def test_after_5m_receipt_15m_gets_the_next_maintenance_slot(self):
  self.assertEqual(self.planned()['_cp_focus']['timeframe'],'5m')
  self.f.write('gettex_chart:'+self.f.isin+':5m',dict(identity=self.f.identity,received_at=self.now.isoformat()))
  self.assertEqual(self.planned()['_cp_focus']['timeframe'],'15m')
 def test_global_rate_limit_still_prevents_quote_request(self):
  self.f.write('gettex_chart_next_request',(self.now+pd.Timedelta(minutes=1)).isoformat())
  self.assertIsNone(self.planned())
 def test_final_closed_session_bar_waits_publication_then_stops_overnight(self):
  plan={'session_open':False,'eligible_at':'2026-11-10T22:01:00Z'}
  for stamp,allowed in [('2026-11-10T22:00Z',False),('2026-11-10T22:01Z',True),('2026-11-10T22:15Z',True),('2026-11-10T22:16Z',False),('2026-11-14T12:00Z',False)]:
   self.assertEqual(schedule.fresh_window(plan,'15m',pd.Timestamp(stamp)),allowed)

if __name__=='__main__':unittest.main()
