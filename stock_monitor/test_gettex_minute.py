"""Only the new auxiliary minute route; no network or production DB."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import pandas as pd
import gettex_chart as chart
import data_quality as quality
from storage import Store
from history_service import recover_from_children
from timeframes import aggregate

ISIN='US0378331005'
META=chart.identity(ISIN,'AAPL.GTX')
START=pd.Timestamp('2026-10-26T00:00:00Z')
NOW=pd.Timestamp('2026-10-27T00:00:00Z')
ROOT=Path(__file__).resolve().parents[1]

def payload():
 rows=[]
 for i,t in enumerate(pd.date_range('2026-10-26 07:30',periods=15,freq='min',tz='Europe/Berlin')):
  rows.append(dict(TIME=str(int(t.timestamp())),OPEN=str(100+i),HIGH=str(101+i),LOW=str(99+i),CLOSE=str(100+i),VOLUME='2'))
 return dict(status='OK',request=dict(ric='AAPL.GTX',interval=1,intervalUnit='m'),data=rows)

def decode(p,meta=META,now=NOW):
 return chart.decode(p,meta,ISIN,'1m',START,NOW,now)

class MinuteTests(unittest.TestCase):
 def test_real_minute_receipt_keeps_unknowns_and_identity(self):
  p=json.loads((ROOT/'history64-evidence/apple-minute30/raw-2026-10-26.json').read_text())
  f=decode(p)
  self.assertEqual(len(f),177);self.assertEqual(f.attrs['timeframe'],'1m')
  self.assertTrue(all(f.attrs[k]==v for k,v in META.items()))
  self.assertEqual(f.attrs['data_quality']['state'],'UNCONFIRMED')
  for tf in ('5m','15m','30m','1H','2H','3H','4H'):
   self.assertTrue(aggregate(f,'1m',tf,now=NOW).empty)

 def test_fetch_uses_one_minute_and_preserves_regular_feed_bases(self):
  calls=[]
  class Reply:
   def __init__(self,data=None,text=''):self.data=data;self.text=text
   def json(self):return self.data
  class Client(chart.Client):
   def request(self,method,path,**kw):
    calls.append((path,kw))
    if path.startswith('https://www.gettex.de/'):return Reply(text='const samlRequest=`public-test`;')
    if '/samllogin' in path:return Reply(dict(token='test-public-session'))
    if path.endswith('/find/securities'):return Reply(dict(status='OK',data=[{'x.RIC':'AAPL.GTX'}]))
    if path.endswith('/quote/info'):return Reply(dict(status='OK',data=[{'x._ISIN':ISIN,'x._CURRENCY':'EUR'}]))
    return Reply(payload())
  Client().fetch(ISIN,'1m',START,NOW)
  self.assertEqual(calls[-1][1]['params']['interval'],1)
  self.assertEqual(chart.BASES,('15m','5m','1D'))
  n=len(calls)
  with self.assertRaises(ValueError):Client().fetch(ISIN,'invalid',START,NOW)
  self.assertEqual(len(calls),n)

 def test_wrong_identity_or_interval_is_rejected(self):
  for meta in (dict(META,currency='USD'),dict(META,venue='FRA'),dict(META,source='proxy')):
   with self.subTest(meta=meta),self.assertRaises(ValueError):decode(payload(),meta)
  p=payload();p['request']['interval']=15
  with self.assertRaises(ValueError):decode(p)
  p=payload();p['request']['ric']='OTHER.GTX'
  with self.assertRaises(ValueError):decode(p)

 def test_invalid_duplicate_and_off_session_rejected(self):
  for mode in ('ohlcv','duplicate','session'):
   p=payload();p['data']=p['data'][:1]
   if mode=='ohlcv':p['data'][0]['HIGH']='1'
   elif mode=='duplicate':p['data']*=2
   else:p['data'][0]['TIME']=str(int(pd.Timestamp('2026-10-26T03:00:00Z').timestamp()))
   with self.subTest(mode=mode),self.assertRaises(ValueError):decode(p)

 def test_forming_minute_excluded_and_empty_not_no_trades(self):
  p=payload();p['data']=p['data'][:1]
  f=decode(p,now=pd.Timestamp('2026-10-26T06:30:30Z'))
  self.assertTrue(f.empty)
  p['data']=[];f=decode(p)
  self.assertTrue(f.empty);self.assertFalse(f.attrs.get('absence_evidence'))

 def test_existing_recovery_requires_complete_real_minutes(self):
  with tempfile.TemporaryDirectory(prefix='minute-only-') as folder:
   s=Store(folder)
   try:
    info=dict(isin=ISIN,name='Apple',history_venue_lock='GETTEX');s.save_watchlist([info])
    f=decode(payload());s.save_series(info,f,'1m')
    s.set_setting(chart.state_key(ISIN,'15m'),dict(state='SAVED',identity=META,listing_proof=f.attrs['listing_proof']))
    stamp=pd.Timestamp('2026-10-26 07:30')
    result=recover_from_children(info,s,META,'15m',stamp,stamp,NOW)
    self.assertIsNotNone(result);self.assertTrue(result.bar_complete.all())
    self.assertEqual(list(result.iloc[0][list(quality.FIELDS)]),[100.,115.,99.,114.,30.])
    self.assertEqual(result.attrs['parent_recovery_proofs'][str(stamp)]['inputs'][0]['base_tf'],'1m')
    with s.connect() as db:db.execute("DELETE FROM series WHERE base_tf='1m' AND ts=?",('2026-10-26 07:35:00',))
    self.assertIsNone(recover_from_children(info,s,META,'15m',stamp,stamp,NOW))
   finally:s.read_cache.close()

if __name__=='__main__':unittest.main()
