import unittest
from unittest.mock import patch
import candle_schedule as s
from test_cp_trade_priority import TradePriority

class FocusBase(unittest.TestCase):
 prepare=TradePriority.prepare
 setup_universe=TradePriority.setup_universe
 complete=TradePriority.complete
 def plan(self,tf='15m',due=('1m','15m','5m'),positions=False):
  meta=self.store.setting('gettex_chart:I001:15m')['identity']
  self.store.set_setting('gettex_chart:I001:1m',dict(identity=meta,received_at='2026-11-02T11:59:00Z'))
  self.store.set_setting('gettex_chart:I001:5m',dict(identity=meta,received_at='2026-11-01T11:59:00Z'))
  with patch('paper_account.ledger',return_value={'positions':[self.position] if positions else []}),patch('gettex_chart.confirmed_state',side_effect=lambda i,v:bool(v.get('identity'))),patch.object(s,'decision',side_effect=lambda st,i,m,b,n:dict(due=m['source']=='gettex_chart' and b in due,session_open=True,key='unused')):
   return s.fresh_candidate(self.store,self.infos,self.now,dict(self.focus,timeframe=tf))[0]
 def test_due_displayed_base_before_forming_minute_after_other_stock(self):
  self.prepare();self.store.set_setting(s.FRESH_ROTATION,{'consecutive':1,'foreground_streak':0,'isin':'I000'})
  self.assertEqual(self.plan()['_cp_focus']['timeframe'],'15m')
 def test_forming_minute_when_closed_base_not_due(self):
  self.prepare();self.assertEqual(self.plan(due=('1m',))['_cp_focus']['timeframe'],'1m')
 def test_display_dependencies_follow_tf_without_optional_5m(self):
  for tf,expected in [('5m','5m'),('10m','5m'),('15m','15m'),('30m','15m'),('1H','15m'),('2H','15m'),('3H','15m'),('4H','15m')]:
   self.prepare()
   with self.subTest(tf=tf):self.assertEqual(self.plan(tf)['_cp_focus']['timeframe'],expected)
 def test_cooldown_preserves_alternative_minute_and_background_slot(self):
  self.prepare();self.assertEqual(self.plan(due=('1m',))['_cp_focus']['timeframe'],'1m')
  self.store.set_setting(s.FRESH_ROTATION,{'consecutive':2,'foreground_streak':0})
  self.assertNotEqual(self.plan()['isin'],'I001')

if __name__=='__main__':unittest.main()
