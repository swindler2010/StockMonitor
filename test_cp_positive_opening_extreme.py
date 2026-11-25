"""An observed first tick can be missing from prices while present in volume."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class OpeningExtreme(unittest.TestCase):
 def test_current_positive_first_tick_repairs_prices_without_volume_or_grant_change(self):
  with tempfile.NamedTemporaryFile(dir=Path(__file__).parent.parent.parent/'СМ-Архив',suffix='.sqlite3',delete=False) as f:path=Path(f.name)
  try:
   s=FlatStore(path);isin='US69608A1088';info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');s.save_instrument(info)
   now=pd.Timestamp.now(tz='UTC');a='2026-10-30 07:30:00';b='2026-10-30 08:00:00'
   m=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='PTXG.GTX',timezone='Europe/Berlin')
   trades=[('07:30:05',172.68,9.),('07:33:11',172.7,9.),('07:35:25.271384',172.42,5.),('07:35:25.494710',172.9,83.),
    ('07:37:56',172.84,29.),('07:38:29',173.54,60.),('07:39:56',172.78,1.),('07:46:00',173.44,50.)]
   trades=[('2026-10-30 '+t,p,v) for t,p,v in trades]
   raw=pd.DataFrame(trades,columns=['ts','price','volume']);raw.index=pd.to_datetime(raw.pop('ts'),format='mixed')
   rows={base:[(str(t),g.price.iloc[0],g.price.max(),g.price.min(),g.price.iloc[-1],g.volume.sum()) for t,g in raw.groupby(pd.Grouper(freq=base.replace('m','min'))) if not g.empty] for base in ('5m','15m')}
   native=copy.deepcopy(rows)
   r=native['5m'][1];native['5m'][1]=(r[0],172.9,r[2],172.78,r[4],r[5])
   r=native['15m'][0];native['15m'][0]=(r[0],r[1],r[2],172.68,r[4],r[5])
   proof=dict(isin=isin,identity={**m,'source':'gettex','symbol':isin},basis='complete_published_trade_files',checked_at=now.isoformat(),no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261030':{}})
   for t in pd.date_range(a,b,freq='5min',inclusive='left'):
    proof['trade_intervals' if str(t) in {r[0] for r in rows['5m']} else 'no_trades'].append([str(t),str(t+pd.Timedelta(minutes=5))])
   for t in ('06.45','07.00'):
    for board in ('munc','mund'):proof['reference']['GETTEX:20261030']['posttrade.20261030.'+t+'.'+board+'.csv.gz']='a'*64
   policy=dict(version=rw.RETIME_VERSION,approved=True);s.set_setting(rw.RETIME_POLICY,policy);s.set_setting('interval_evidence:'+isin,proof)
   for base in rows:s.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=m,listing_proof={**m,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
   with s.connect() as d:
    d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*r) for r in trades])
    for base in rows:
     d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in rows[base]])
     d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[m[k] for k in rw.IDENTITY[:4]],base,*r) for r in native[base]])
    job,_=rv._enqueue(d,rv._spec(isin,m,'15m',a,a),{'kind':'test'},now)
    inputs=rv._input(d,job);self.assertIsNone(rw.retime_plan(d,job,inputs,now));old=dict(job);job['input_fingerprint']=inputs['fingerprint'];rv._save_job(d,job,old)
   rv.observe(s,job['id'],isin,rv.digest(s.setting(rv.key(job))),'DIFFERENT','GETTEX:PTX',True);job=s.setting(rv.key(job))
   with s.connect() as d:
    inputs=rv._input(d,job);plan=rw.retime_plan(d,job,inputs,now);self.assertEqual(plan['mode'],'VERIFIED_NATIVE_POSITIVE_OPENING_EXTREME')
    d.execute("UPDATE series SET open=172.91 WHERE base_tf='5m' AND ts='2026-10-30 07:35:00'")
    self.assertIsNone(rw._reviewed_positive_extreme(d,job,inputs,now,policy))
    with self.assertRaisesRegex(ValueError,'RETIME_INPUT_CHANGED'):rw.publish_retime(d,job,inputs,plan,now)
    d.rollback()
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'))
   receipt=s.setting(rw.RETIME_PREFIX+isin+':'+job['id']);self.assertEqual((receipt['changed'],receipt['added'],receipt['relocated']),(2,0,0))
   with s.connect() as d:
    for base in rows:self.assertEqual(rw._native(d,isin,m,base,a,b),rows[base])
    self.assertTrue(rw.retime_current(d,receipt,now));self.assertEqual(d.execute('select count(*) from candle_revision_history').fetchone()[0],2)
   self.assertIsNone(s.setting('cp_manual_risk:'+isin));self.assertEqual(s.setting(rw.RETIME_POLICY),policy)
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'))
   self.assertEqual(s.setting(rv.key(job))['status'],'RESOLVED_NATIVE')
   s.set_setting(rw.RETIME_POLICY,{**policy,'approved':False})
   with s.connect() as d:self.assertFalse(rw.retime_current(d,receipt,now))
  finally:
   if 's' in locals():s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
