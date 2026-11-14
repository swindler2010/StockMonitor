"""An existing reviewed native bar misses a positively traded official extreme."""
import copy, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class NativeExtrema(unittest.TestCase):
 def test_only_current_isolated_positive_trade_extreme_is_repaired_by_cp(self):
  root=Path(os.environ.get('SM_TEST_ARCHIVE',str(Path(__file__).parent.parent.parent/'СМ-Архив')))
  with tempfile.NamedTemporaryFile(dir=root,suffix='.sqlite3',delete=False) as f:path=Path(f.name)
  try:
   s=FlatStore(path);isin='US69608A1088';info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX')
   s.save_instrument(info);now=pd.Timestamp('2026-11-14T12:00Z')
   meta=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='PTXG.GTX',timezone='Europe/Berlin')
   official={**meta,'source':'gettex','symbol':isin};first='2026-11-13 11:15:00';at='2026-11-13 11:30:00';end='2026-11-13 11:45:00'
   trades=[('11:17:28',155.5,60.),('11:22:55',155.52,14.),('11:26:21',155.06,1.),
    ('11:30:11',155.2,2.),('11:34:57.540583',155.48,6.),('11:34:57.750728',155.1,20.),
    ('11:39:50',155.2,1.),('11:41:13',155.,50.),('11:43:27',155.1,0.)]
   trades=[('2026-11-13 '+t+('' if '.' in t else '.000000'),p,v) for t,p,v in trades]
   raw=pd.DataFrame(trades,columns=['ts','price','volume']);raw.index=pd.to_datetime(raw.pop('ts'),format='mixed')
   rows={base:[(str(t),float(g.price.iloc[0]),float(g.price.max()),float(g.price.min()),float(g.price.iloc[-1]),float(g.volume.sum()))
    for t,g in raw.groupby(pd.Grouper(freq=base.replace('m','min'))) if not g.empty] for base in ('5m','15m')}
   native={base:[tuple([r[0],r[1],155.2,r[3],r[4],r[5]]) if r[0]==at else r for r in items] for base,items in rows.items()}
   proof=dict(isin=isin,identity=official,basis='complete_published_trade_files',checked_at=now.isoformat(),
    no_trades=[],trade_intervals=[[str(t),str(t+pd.Timedelta(minutes=5))] for t in pd.date_range(first,end,freq='5min',inclusive='left')],
    zero_quantity_intervals=[],reference={'GETTEX:20261113':{}})
   for t in ('10.30','10.45'):
    for board in ('munc','mund'):proof['reference']['GETTEX:20261113']['posttrade.20261113.'+t+'.'+board+'.csv.gz']='a'*64
   policy=dict(version=rw.RETIME_VERSION,approved=True)
   s.set_setting(rw.RETIME_POLICY,policy);s.set_setting('interval_evidence:'+isin,proof)
   for base in rows:s.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=meta,
    listing_proof={**meta,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
   with s.connect() as d:
    d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*r) for r in trades])
    for base in rows:
     d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in rows[base]])
     d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[meta[k] for k in rw.IDENTITY[:4]],base,*r) for r in native[base]])
    job,_=rv._enqueue(d,rv._spec(isin,meta,'15m',at,at),{'kind':'test'},now)
    inputs=rv._input(d,job)
    self.assertIsNone(rw.retime_plan(d,job,inputs,now))
    old=dict(job);job['input_fingerprint']=inputs['fingerprint'];rv._save_job(d,job,old)
   rv.observe(s,job['id'],isin,rv.digest(s.setting(rv.key(job))),'DIFFERENT','GETTEX:PTX',True)
   job=s.setting(rv.key(job))
   with s.connect() as d:
    inputs=rv._input(d,job);plan=rw.retime_plan(d,job,inputs,now)
    self.assertEqual(plan['mode'],'VERIFIED_NATIVE_EXTREMA')
    d.execute("UPDATE series SET close=155.11 WHERE base_tf='5m' AND ts=?",(at,))
    with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,job,inputs,plan,now)
    d.rollback()
   bad=copy.deepcopy(proof);bad['reference']['GETTEX:20261113'].pop('posttrade.20261113.10.45.mund.csv.gz')
   s.set_setting('interval_evidence:'+isin,bad)
   with s.connect() as d:
    changed=rv._input(d,job)
    # A changed source proof also revokes the current visual binding.
    self.assertIsNone(rw.retime_plan(d,job,changed,now))
   s.set_setting('interval_evidence:'+isin,proof)
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'))
   receipt=s.setting(rw.RETIME_PREFIX+isin+':'+job['id'])
   self.assertEqual((receipt['added'],receipt['changed'],receipt['relocated']),(0,2,0))
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'))
   self.assertEqual(s.setting(rv.key(job))['status'],'RESOLVED_NATIVE')
   with s.connect() as d:
    for base in rows:self.assertEqual(rw._native(d,isin,meta,base,first,end),rows[base])
    saved=d.execute('SELECT old_ohlcv,new_ohlcv FROM candle_revision_history').fetchall()
    self.assertEqual(len(saved),2)
    for old,new in saved:
     old,new=json.loads(old),json.loads(new)
     self.assertEqual(old['high'],155.2);self.assertEqual(new['high'],155.48)
     self.assertEqual({k:v for k,v in old.items() if k!='high'},{k:v for k,v in new.items() if k!='high'})
    self.assertTrue(rw.retime_current(d,receipt,now))
   s.read_cache.close();s=FlatStore(path)
   with s.connect() as d:self.assertTrue(rw.retime_current(d,receipt,now))
   s.set_setting(rw.RETIME_POLICY,{**policy,'approved':False})
   with s.connect() as d:self.assertFalse(rw.retime_current(d,receipt,now))
  finally:
   if 's' in locals():s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
