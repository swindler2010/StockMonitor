"""A reviewed zero-price interval has delayed native ticks and one lost close."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class DelayedClosing(unittest.TestCase):
 def test_complete_tape_unique_native_prefix_and_review_restore_price_and_close(self):
  aroot=Path(__file__).parent.parent.parent/'СМ-Архив';f=tempfile.NamedTemporaryFile(dir=aroot,prefix='CP delayed closing isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
  try:
   isin='US69608A1088';info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');s.save_instrument(info);now=pd.Timestamp.now(tz='UTC')
   m=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='PTXG.GTX',timezone='Europe/Berlin');om={**m,'source':'gettex','symbol':isin}
   a='2026-11-13 11:30:00';end='2026-11-13 12:00:00';ticks=[('11:44:12.000001',100.,0.),('11:49:59.000001',99.,6.),('11:50:06.000001',99.,1.),('11:55:09.000001',98.,100.),('11:55:21.000001',99.2,100.),('11:56:39.000001',97.5,12.)];ticks=[('2026-11-13 '+t,p,v) for t,p,v in ticks]
   raw=pd.DataFrame(ticks,columns=['ts','price','volume']);raw.index=pd.to_datetime(raw.pop('ts'))
   rows={b:[(str(t),float(g.price.iloc[0]),float(g.price.max()),float(g.price.min()),float(g.price.iloc[-1]),float(g.volume.sum())) for t,g in raw.groupby(pd.Grouper(freq=b.replace('m','min'))) if not g.empty] for b in ('5m','15m')}
   native={'5m':[('2026-11-13 11:50:00',100.,100.,100.,100.,0.),('2026-11-13 11:55:00',99.,99.2,98.,99.2,207.)],'15m':[('2026-11-13 11:45:00',100.,100.,98.,99.2,207.)]}
   proof=dict(isin=isin,identity=om,basis='complete_published_trade_files',checked_at=now.isoformat(),no_trades=[],trade_intervals=[],zero_quantity_intervals=[['2026-11-13 11:40:00','2026-11-13 11:45:00']],reference={'GETTEX:20261113':{}})
   for t in pd.date_range(a,end,freq='5min',inclusive='left'):
    if str(t)=='2026-11-13 11:40:00':continue
    field='trade_intervals' if str(t) in {r[0] for r in rows['5m']} else 'no_trades';proof[field].append([str(t),str(t+pd.Timedelta(minutes=5))])
   for at in ('10.45','11.00'):
    for board in ('munc','mund'):proof['reference']['GETTEX:20261113']['posttrade.20261113.'+at+'.'+board+'.csv.gz']='a'*64
   policy=dict(version=rw.RETIME_VERSION,approved=True);sp=dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX');s.set_setting(rw.RETIME_POLICY,policy);s.set_setting(rv.SOURCE_POLICY,sp);s.set_setting('interval_evidence:'+isin,proof)
   for b in rows:s.set_setting('gettex_chart:'+isin+':'+b,dict(state='SAVED',identity=m,listing_proof={**m,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
   with s.connect() as d:
    d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*t) for t in ticks])
    for b in rows:
     d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',b,*r) for r in rows[b]])
     d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[m[k] for k in rw.IDENTITY[:4]],b,*r) for r in native[b]])
    j,_=rv._enqueue(d,rv._spec(isin,m,'15m',a,a),{'kind':'test'},now);n=rv._input(d,j)
    with self.assertRaisesRegex(ValueError,'INCOMPLETE_PUBLICATION'):rw.retime_plan(d,j,n,now)
    old=dict(j);j['input_fingerprint']=n['fingerprint'];rv._save_job(d,j,old)
   rv.observe(s,j['id'],isin,rv.digest(s.setting(rv.key(j))),'ZERO_VOLUME','GETTEX:PTX',True)
   j=s.setting(rv.key(j))
   with s.connect() as d:
    n=rv._input(d,j);plan=rw.retime_plan(d,j,n,now);self.assertEqual(plan['mode'],'VERIFIED_PRICE_CLOSING_TICK_PARTITION')
    d.execute("UPDATE series SET volume=208 WHERE base_tf='5m' AND ts='2026-11-13 11:55:00'")
    with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,j,n,plan,now)
    d.rollback()
   s.set_setting(rv.SOURCE_POLICY,{**sp,'approved':False})
   with s.connect() as d:
    with self.assertRaisesRegex(ValueError,'RETIME_'):rw.retime_plan(d,j,rv._input(d,j),now)
   s.set_setting(rv.SOURCE_POLICY,sp)
   bad=copy.deepcopy(proof);bad['reference']['GETTEX:20261113'].pop('posttrade.20261113.11.00.mund.csv.gz');s.set_setting('interval_evidence:'+isin,bad)
   with s.connect() as d:
    with self.assertRaisesRegex(ValueError,'RETIME_'):rw.retime_plan(d,j,rv._input(d,j),now)
   s.set_setting('interval_evidence:'+isin,proof)
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'))
   receipt=s.setting(rw.RETIME_PREFIX+isin+':'+j['id']);self.assertIsNotNone(receipt);self.assertEqual((receipt['added'],receipt['changed'],receipt['relocated']),(3,3,0))
   with s.connect() as d:
    for b in rows:self.assertEqual(rw._native(d,isin,m,b,a,end),rows[b])
    self.assertTrue(rw.retime_current(d,receipt));self.assertNotEqual(rv._classify(j,rv._input(d,j),now)['state'],'NO_TRADES')
    count=d.execute('select count(*) from candle_revision_history').fetchone()[0]
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'))
   with s.connect() as d:self.assertEqual(d.execute('select count(*) from candle_revision_history').fetchone()[0],count)
   self.assertEqual(s.setting('interval_evidence:'+isin),proof);self.assertIsNone(s.setting('cp_manual_risk:'+isin))
   s.read_cache.close();s=FlatStore(path)
   with s.connect() as d:self.assertTrue(rw.retime_current(d,receipt))
   s.set_setting(rv.SOURCE_POLICY,{**sp,'approved':False})
   with s.connect() as d:self.assertFalse(rw.retime_current(d,receipt))
  finally:
   s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
