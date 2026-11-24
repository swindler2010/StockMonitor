"""Full native quantities but two isolated positive prices were not sampled."""
import copy,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class NativePriceSampling(unittest.TestCase):
 def test_current_review_exact_positive_samples_and_conserved_quantities(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив'
  f=tempfile.NamedTemporaryFile(dir=root,prefix='CP price samples isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
  try:
   isin='DE0007236101';info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');s.save_instrument(info);now=pd.Timestamp.now(tz='UTC')
   m=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='SIEGn.GTX',timezone='Europe/Berlin');om={**m,'source':'gettex','symbol':isin}
   a='2026-11-13 09:00:00';end='2026-11-13 09:15:00'
   ticks=[('09:00:01.000001',100.,10.),('09:00:02.000001',99.,5.),('09:03:49.000001',98.,1.),('09:03:50.000001',99.,2.),('09:05:01.000001',97.,3.),('09:10:01.000001',96.,2.)];ticks=[('2026-11-13 '+t,p,v) for t,p,v in ticks]
   raw=pd.DataFrame(ticks,columns=['ts','price','volume']);raw.index=pd.to_datetime(raw.pop('ts'))
   rows={b:[(str(t),float(g.price.iloc[0]),float(g.price.max()),float(g.price.min()),float(g.price.iloc[-1]),float(g.volume.sum())) for t,g in raw.groupby(pd.Grouper(freq=b.replace('m','min'))) if not g.empty] for b in ('5m','15m')}
   native=copy.deepcopy(rows);native['5m'][0]=(a,99.,99.,99.,99.,18.);native['15m'][0]=(a,99.,99.,96.,96.,23.)
   proof=dict(isin=isin,identity=om,basis='complete_published_trade_files',checked_at=now.isoformat(),no_trades=[],trade_intervals=[[str(t),str(t+pd.Timedelta(minutes=5))] for t in pd.date_range(a,end,freq='5min',inclusive='left')],zero_quantity_intervals=[],reference={'GETTEX:20261113':{}})
   for board in ('munc','mund'):proof['reference']['GETTEX:20261113']['posttrade.20261113.08.15.'+board+'.csv.gz']='a'*64
   policy=dict(version=rw.RETIME_VERSION,approved=True);sp=dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX');s.set_setting(rw.RETIME_POLICY,policy);s.set_setting(rv.SOURCE_POLICY,sp);s.set_setting('interval_evidence:'+isin,proof)
   for b in rows:s.set_setting('gettex_chart:'+isin+':'+b,dict(state='SAVED',identity=m,listing_proof={**m,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
   with s.connect() as d:
    d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*t) for t in ticks])
    for b in rows:
     d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',b,*r) for r in rows[b]])
     d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[m[k] for k in rw.IDENTITY[:4]],b,*r) for r in native[b]])
    j,_=rv._enqueue(d,rv._spec(isin,m,'15m',a,a),{'kind':'fixture'},now);n=rv._input(d,j)
    self.assertIsNone(rw.retime_plan(d,j,n,now));old=dict(j);j['input_fingerprint']=n['fingerprint'];rv._save_job(d,j,old)
   rv.observe(s,j['id'],isin,rv.digest(s.setting(rv.key(j))),'DIFFERENT','GETTEX:SIE',True)
   j=s.setting(rv.key(j))
   with s.connect() as d:
    n=rv._input(d,j);plan=rw.retime_plan(d,j,n,now)
    self.assertIsNotNone(plan);self.assertEqual(plan['mode'],'VERIFIED_NATIVE_PRICE_SAMPLES');self.assertEqual(len(plan['omitted_price_ticks']),2)
    d.execute("UPDATE series SET volume=volume+1 WHERE base_tf='5m' AND ts=?",(a,))
    self.assertIsNone(rw.retime_plan(d,j,rv._input(d,j),now))
    with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,j,n,plan,now)
    d.rollback()
   for key,value in [(rv.SOURCE_POLICY,{**sp,'approved':False}),('interval_evidence:'+isin,{**proof,'reference':{}})]:
    prior=s.setting(key);s.set_setting(key,value)
    with s.connect() as d:
     try:rejected=rw.retime_plan(d,j,rv._input(d,j),now)
     except ValueError:rejected=None
     self.assertIsNone(rejected)
    s.set_setting(key,prior)
   now=pd.Timestamp.now(tz='UTC')
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,now)
   receipt=s.setting(rw.RETIME_PREFIX+isin+':'+j['id']);self.assertIsNotNone(receipt);self.assertEqual((receipt['added'],receipt['changed'],receipt['relocated']),(0,2,0))
   with s.connect() as d:
    for b in rows:self.assertEqual(rw._native(d,isin,m,b,a,end),rows[b])
    self.assertTrue(rw.retime_current(d,receipt))
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,now+pd.Timedelta(seconds=1))
   self.assertEqual(s.setting(rv.key(j))['status'],'RESOLVED_NATIVE')
   with s.connect() as d:self.assertEqual(d.execute('select count(*) from candle_revision_history').fetchone()[0],2)
   self.assertIsNone(s.setting('cp_manual_risk:'+isin));s.read_cache.close();s=FlatStore(path)
   with s.connect() as d:self.assertTrue(rw.retime_current(d,receipt))
   s.set_setting(rv.SOURCE_POLICY,{**sp,'approved':False})
   with s.connect() as d:self.assertFalse(rw.retime_current(d,receipt))
  finally:
   s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
