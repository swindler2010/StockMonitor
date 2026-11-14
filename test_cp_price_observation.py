"""Existing zero-quantity prices can be retimed, never turned into turnover."""
import copy, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class PriceObservation(unittest.TestCase):
 def test_exact_published_zero_price_relocation_and_split_keep_quantity_zero(self):
  root=Path(os.environ.get('SM_TEST_ARCHIVE',str(Path(__file__).parent.parent.parent/'СМ-Архив')))
  for kind in ('move','split'):
   with self.subTest(kind=kind):
    with tempfile.NamedTemporaryFile(dir=root,suffix='.sqlite3',delete=False) as f:path=Path(f.name)
    try:
     s=FlatStore(path);isin='US11135F1012';info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX')
     s.save_instrument(info);now=pd.Timestamp.now(tz='UTC')
     m=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='1YD.GTX',timezone='Europe/Berlin')
     om={**m,'source':'gettex','symbol':isin};a='2026-11-13 13:00:00';b='2026-11-13 13:30:00'
     q='2026-11-13 13:10:00';n='2026-11-13 13:15:00'
     trades=[('2026-11-13 13:14:00',325.05,0.)]
     if kind=='split':trades += [('2026-11-13 13:18:00',323.6,1.),('2026-11-13 13:22:00',323.3,1.),('2026-11-13 13:24:00',323.35,1.),('2026-11-13 13:26:00',323.5,3.),('2026-11-13 13:29:00',323.85,20.)]
     raw=pd.DataFrame(trades,columns=['ts','price','volume']);raw.index=pd.to_datetime(raw.pop('ts'))
     rows={base:[(str(t),float(g.price.iloc[0]),float(g.price.max()),float(g.price.min()),float(g.price.iloc[-1]),float(g.volume.sum()))
      for t,g in raw.groupby(pd.Grouper(freq=base.replace('m','min'))) if not g.empty] for base in ('5m','15m')}
     if kind=='move':native={base:[(n,*rows[base][0][1:])] for base in rows}
     else:native={'5m':[(n,325.05,325.05,323.6,323.6,1.),*rows['5m'][2:]],'15m':[(n,325.05,325.05,323.3,323.85,26.)]}
     proof=dict(isin=isin,identity=om,basis='complete_published_trade_files',checked_at=now.isoformat(),
      no_trades=[],trade_intervals=[],zero_quantity_intervals=[[q,n]],reference={'GETTEX:20261113':{}})
     for stamp in pd.date_range(a,b,freq='5min',inclusive='left'):
      if str(stamp)==q:continue
      key='trade_intervals' if str(stamp) in {r[0] for r in rows['5m']} else 'no_trades'
      proof[key].append([str(stamp),str(stamp+pd.Timedelta(minutes=5))])
     for t in ('12.15','12.30'):
      for board in ('munc','mund'):proof['reference']['GETTEX:20261113']['posttrade.20261113.'+t+'.'+board+'.csv.gz']='a'*64
     policy=dict(version=rw.RETIME_VERSION,approved=True)
     s.set_setting(rw.RETIME_POLICY,policy);s.set_setting('interval_evidence:'+isin,proof)
     for base in rows:s.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=m,
      listing_proof={**m,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
     with s.connect() as d:
      d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*r) for r in trades])
      for base in rows:
       d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in rows[base]])
       d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[m[k] for k in rw.IDENTITY[:4]],base,*r) for r in native[base]])
      job,_=rv._enqueue(d,rv._spec(isin,m,'15m',a,a),{'kind':'test'},now)
      inputs=rv._input(d,job)
      with self.assertRaisesRegex(ValueError,'INCOMPLETE_PUBLICATION'):rw.retime_plan(d,job,inputs,now)
      old=dict(job);job['input_fingerprint']=inputs['fingerprint'];rv._save_job(d,job,old)
     rv.observe(s,job['id'],isin,rv.digest(s.setting(rv.key(job))),'ZERO_VOLUME','GETTEX:1YD',True)
     job=s.setting(rv.key(job))
     with s.connect() as d:
      inp=rv._input(d,job);plan=rw.retime_plan(d,job,inp,now)
      self.assertEqual(plan['mode'],'VERIFIED_PRICE_OBSERVATION_TIMEBASE')
      d.execute("UPDATE series SET open=325.04 WHERE base_tf='5m' AND ts=?",(n,))
      with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,job,inp,plan,now)
      d.rollback()
     with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'))
     receipt=s.setting(rw.RETIME_PREFIX+isin+':'+job['id'])
     self.assertEqual(receipt['mode'],'VERIFIED_PRICE_OBSERVATION_TIMEBASE')
     self.assertEqual(receipt['relocated'],2 if kind=='move' else 0)
     self.assertEqual(receipt['added'],0 if kind=='move' else 2)
     with s.connect() as d:
      for base in rows:
       repaired=rw._native(d,isin,m,base,a,b);self.assertEqual(repaired,rows[base])
       self.assertEqual(sum(r[5] for r in repaired),sum(r[5] for r in native[base]))
       self.assertEqual(repaired[0][5],0)
      self.assertTrue(rw.retime_current(d,receipt,now))
      self.assertNotEqual(rv._classify(job,rv._input(d,job),now)['state'],'NO_TRADES')
     self.assertEqual(s.setting('interval_evidence:'+isin),proof)
     self.assertIsNone(s.setting('cp_manual_risk:'+isin))
     bad=copy.deepcopy(proof);bad['incomplete_publication_intervals']=[[a,b]]
     s.set_setting('interval_evidence:'+isin,bad)
     with s.connect() as d:self.assertFalse(rw.retime_current(d,receipt,now))
    finally:
     if 's' in locals():s.read_cache.close()
     for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
