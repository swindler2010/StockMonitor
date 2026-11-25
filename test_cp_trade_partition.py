"""Conserved native timebase repair must reproduce every original tape value."""
import copy,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class TradePartition(unittest.TestCase):
 def test_existing_cp_repartitions_only_uniquely_confirmed_native_tape(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив'
  with tempfile.NamedTemporaryFile(dir=root,suffix='.sqlite3',delete=False) as f:path=Path(f.name)
  try:
   s=FlatStore(path);isin='US69608A1088';info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX')
   s.save_instrument(info);now=pd.Timestamp.now(tz='UTC')
   m=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='PTXG.GTX',timezone='Europe/Berlin')
   a='2026-11-02 11:15:00';b='2026-11-02 11:45:00';official={**m,'source':'gettex','symbol':isin}
   trades=[('11:21:27',166.26,8.),('11:22:17',166.14,5.),('11:23:09',165.88,40.),('11:27:52',166.3,1.),
    ('11:38:35',166.02,50.),('11:44:02',166.12,0.),('11:44:39',166.34,10.),('11:44:56',166.14,1.)]
   trades=[('2026-11-02 '+t,p,v) for t,p,v in trades]
   raw=pd.DataFrame(trades,columns=['ts','price','volume']);raw.index=pd.to_datetime(raw.pop('ts'))
   rows={base:[(str(t),g.price.iloc[0],g.price.max(),g.price.min(),g.price.iloc[-1],g.volume.sum())
    for t,g in raw.groupby(pd.Grouper(freq=base.replace('m','min'))) if not g.empty] for base in ('5m','15m')}
   native={'5m':[('2026-11-02 11:20:00',166.26,166.26,166.26,166.26,8.),('2026-11-02 11:25:00',166.14,166.14,165.88,165.88,45.),
     ('2026-11-02 11:30:00',166.3,166.3,166.3,166.3,1.),*rows['5m'][-2:]],
    '15m':[(a,166.26,166.26,165.88,165.88,53.),('2026-11-02 11:30:00',166.3,166.34,166.02,166.14,62.)]}
   proof=dict(isin=isin,identity=official,basis='complete_published_trade_files',checked_at=now.isoformat(),
    no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261102':{}})
   for t in pd.date_range(a,b,freq='5min',inclusive='left'):
    proof['trade_intervals' if str(t) in {r[0] for r in rows['5m']} else 'no_trades'].append([str(t),str(t+pd.Timedelta(minutes=5))])
   for t in ('10.30','10.45'):
    for board in ('munc','mund'):proof['reference']['GETTEX:20261102']['posttrade.20261102.'+t+'.'+board+'.csv.gz']='a'*64
   policy=dict(version=rw.RETIME_VERSION,approved=True);s.set_setting(rw.RETIME_POLICY,policy);s.set_setting('interval_evidence:'+isin,proof)
   for base in rows:s.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=m,listing_proof={**m,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
   with s.connect() as d:
    d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*r) for r in trades])
    for base in rows:
     d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in rows[base]])
     d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[m[k] for k in rw.IDENTITY[:4]],base,*r) for r in native[base]])
    job,_=rv._enqueue(d,rv._spec(isin,m,'15m',a,a),{'kind':'test'},now)
    inputs=rv._input(d,job);self.assertIsNone(rw.retime_plan(d,job,inputs,now))
    old=dict(job);job['input_fingerprint']=inputs['fingerprint'];rv._save_job(d,job,old)
   rv.observe(s,job['id'],isin,rv.digest(s.setting(rv.key(job))),'DIFFERENT','GETTEX:PTX',True)
   job=s.setting(rv.key(job))
   with s.connect() as d:
    inputs=rv._input(d,job);plan=rw.retime_plan(d,job,inputs,now)
    self.assertEqual(plan['mode'],'VERIFIED_NATIVE_TRADE_PARTITION')
    # A conserved total is insufficient: every individual original value matters.
    d.execute("UPDATE series SET high=166.27 WHERE base_tf='5m' AND ts='2026-11-02 11:20:00'")
    self.assertIsNone(rw._reviewed_partition_plan(d,job,inputs,now,policy))
    with self.assertRaisesRegex(ValueError,'RETIME_INPUT_CHANGED'):rw.publish_retime(d,job,inputs,plan,now)
    d.rollback()
   bad=copy.deepcopy(proof);bad['reference']['GETTEX:20261102'].pop('posttrade.20261102.10.45.mund.csv.gz')
   s.set_setting('interval_evidence:'+isin,bad)
   with s.connect() as d:self.assertIsNone(rw.retime_plan(d,job,rv._input(d,job),now))
   s.set_setting('interval_evidence:'+isin,proof)
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'))
   receipt=s.setting(rw.RETIME_PREFIX+isin+':'+job['id'])
   self.assertEqual(receipt['relocated'],1)
   with s.connect() as d:
    for base in rows:
     self.assertEqual(rw._native(d,isin,m,base,a,b),rows[base])
     self.assertEqual(rw._aggregate(native[base]),rw._aggregate(rows[base]))
    self.assertTrue(rw.retime_current(d,receipt,now))
    self.assertGreater(d.execute('select count(*) from candle_revision_history').fetchone()[0],0)
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'))
   self.assertEqual(s.setting(rv.key(job))['status'],'RESOLVED_NATIVE')
   self.assertIsNone(s.setting('cp_manual_risk:'+isin))
   s.set_setting(rw.RETIME_POLICY,{**policy,'approved':False})
   with s.connect() as d:self.assertFalse(rw.retime_current(d,receipt,now))
  finally:
   if 's' in locals():s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
