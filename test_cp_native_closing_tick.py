"""Closed native bar omits exactly the final independently published tick."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class ClosingTick(unittest.TestCase):
 def test_current_review_and_complete_tape_repair_only_exact_closing_tick(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив'
  for last_volume in (0.,1.):
   with self.subTest(last_volume=last_volume):
    f=tempfile.NamedTemporaryFile(dir=root,prefix='CP closing tick isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
    try:
     isin='US69608A1088';info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');s.save_instrument(info)
     meta=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='PTXG.GTX',timezone='Europe/Berlin');official={**meta,'source':'gettex','symbol':isin}
     at='2026-11-13 11:30:00';end='2026-11-13 11:45:00';now=pd.Timestamp('2026-11-14T12:00Z')
     ticks=[('2026-11-13 11:30:01.000000',100.,2.),('2026-11-13 11:35:01.000000',101.,3.),('2026-11-13 11:40:01.000000',100.,2. if last_volume==0 else 0.),('2026-11-13 11:44:59.000000',99.,last_volume)]
     def build(tape):
      f=pd.DataFrame(tape,columns=['ts','price','volume']);f.index=pd.to_datetime(f.pop('ts'))
      return {b:[(str(t),float(g.price.iloc[0]),float(g.price.max()),float(g.price.min()),float(g.price.iloc[-1]),float(g.volume.sum())) for t,g in f.groupby(pd.Grouper(freq=b.replace('m','min'))) if not g.empty] for b in ('5m','15m')}
     source=build(ticks);native=build(ticks[:-1]);policy=dict(version=rw.RETIME_VERSION,approved=True)
     source_policy=dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX')
     proof=dict(isin=isin,identity=official,basis='complete_published_trade_files',checked_at=now.isoformat(),no_trades=[],trade_intervals=[[str(t),str(t+pd.Timedelta(minutes=5))] for t in pd.date_range(at,end,freq='5min',inclusive='left')],zero_quantity_intervals=[],reference={'GETTEX:20261113':{}})
     for board in ('munc','mund'):proof['reference']['GETTEX:20261113']['posttrade.20261113.10.45.'+board+'.csv.gz']='a'*64
     s.set_setting(rw.RETIME_POLICY,policy);s.set_setting(rv.SOURCE_POLICY,source_policy);s.set_setting('interval_evidence:'+isin,proof)
     for b in source:s.set_setting('gettex_chart:'+isin+':'+b,dict(state='SAVED',identity=meta,listing_proof={**meta,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
     with s.connect() as d:
      d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*t) for t in ticks])
      for b in source:
       d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',b,*t) for t in source[b]])
       d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[meta[k] for k in rw.IDENTITY[:4]],b,*t) for t in native[b]])
      j,_=rv._enqueue(d,rv._spec(isin,meta,'15m',at,at),{'kind':'fixture'},now);n=rv._input(d,j)
      self.assertIsNone(rw.retime_plan(d,j,n,now));old=dict(j);j['input_fingerprint']=n['fingerprint'];rv._save_job(d,j,old)
     rv.observe(s,j['id'],isin,rv.digest(s.setting(rv.key(j))),'DIFFERENT','GETTEX:PTX',True)
     j=s.setting(rv.key(j))
     with s.connect() as d:
      n=rv._input(d,j);plan=rw.retime_plan(d,j,n,now)
      self.assertIsNotNone(plan);self.assertEqual(plan['mode'],'VERIFIED_NATIVE_CLOSING_TICK')
      d.execute("UPDATE series SET volume=volume+0.1 WHERE base_tf='5m' AND ts=?",(at,))
      with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,j,n,plan,now)
      d.rollback()
     for key,value in [(rv.SOURCE_POLICY,{**source_policy,'approved':False}),('interval_evidence:'+isin,{**proof,'reference':{}})]:
      saved=s.setting(key);s.set_setting(key,value)
      with s.connect() as d:
       try:rejected=rw.retime_plan(d,j,rv._input(d,j),now)
       except ValueError:rejected=None
       self.assertIsNone(rejected)
      s.set_setting(key,saved)
     with s.connect() as d:
      d.execute("UPDATE series SET open=open+0.01 WHERE base_tf='5m' AND ts=?",(at,))
      try:rejected=rw.retime_plan(d,j,rv._input(d,j),now)
      except ValueError:rejected=None
      self.assertIsNone(rejected);d.rollback()
     with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'))
     receipt=s.setting(rw.RETIME_PREFIX+isin+':'+j['id']);self.assertIsNotNone(receipt)
     self.assertEqual((receipt['added'],receipt['changed'],receipt['relocated']),(0,2,0))
     with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'))
     self.assertEqual(s.setting(rv.key(j))['status'],'RESOLVED_NATIVE')
     with s.connect() as d:
      for b in source:self.assertEqual(rw._native(d,isin,meta,b,at,end),source[b])
      self.assertEqual(d.execute('select count(*) from candle_revision_history').fetchone()[0],2)
      self.assertTrue(rw.retime_current(d,receipt,now))
     self.assertIsNone(s.setting('cp_manual_risk:'+isin))
     s.read_cache.close();s=FlatStore(path)
     with s.connect() as d:self.assertTrue(rw.retime_current(d,receipt,now))
     s.set_setting(rv.SOURCE_POLICY,{**source_policy,'approved':False})
     with s.connect() as d:self.assertFalse(rw.retime_current(d,receipt,now))
    finally:
     s.read_cache.close()
     for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
