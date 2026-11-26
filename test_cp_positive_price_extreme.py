"""Current visual discrepancy plus a complete primary interior trade, no imports."""
import copy,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv,revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore
class PriceExtreme(unittest.TestCase):
 def test_omitted_interior_price_repairs_only_extrema_with_unchanged_quantity_and_current_boundaries(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив';f=tempfile.NamedTemporaryFile(dir=root,prefix='CP interior price isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
  try:
   isin='US0378331005';now=pd.Timestamp.now(tz='UTC');info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');s.save_instrument(info)
   meta=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='FIX.GTX',timezone='Europe/Berlin');om={**meta,'source':'gettex','symbol':isin};a='2026-11-13 07:30:00';b='2026-11-13 07:45:00';at='2026-11-13 07:35:00';outside='2026-11-13 07:45:00'
   ticks=[('2026-11-13 07:30:02',101.,3.),('2026-11-13 07:35:02',100.,1.),('2026-11-13 07:36:02',98.,1.),('2026-11-13 07:37:02',100.5,2.)]
   source={'5m':[(a,101.,101.,101.,101.,3.),(at,100.,100.5,98.,100.5,4.)],'15m':[(a,101.,101.,98.,100.5,7.)]};native=copy.deepcopy(source);native['5m'][1]=(at,100.,100.5,100.,100.5,4.);native['15m'][0]=(a,101.,101.,100.,100.5,7.)
   proof=dict(isin=isin,identity=om,basis='complete_published_trade_files',checked_at=now.isoformat(),no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261113':{}})
   for t in pd.date_range(a,'2026-11-13 08:00:00',freq='5min',inclusive='left'):proof['trade_intervals' if str(t) in {r[0] for r in source['5m']} else 'no_trades'].append([str(t),str(t+pd.Timedelta(minutes=5))])
   for tm in ('06.45','07.00'):
    for board in ('munc','mund'):proof['reference']['GETTEX:20261113'][f'posttrade.20261113.{tm}.{board}.csv.gz']='a'*64
   policy=dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX');s.set_setting(rv.SOURCE_POLICY,policy);s.set_setting(rw.RETIME_POLICY,dict(approved=True,version=rw.RETIME_VERSION));s.set_setting('interval_evidence:'+isin,proof)
   for base in source:s.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=meta,listing_proof={**meta,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
   with s.connect() as d:
    for base in source:
     d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in source[base]])
     d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[meta[k] for k in rw.IDENTITY[:4]],base,*r) for r in native[base]+[(outside,102.,102.,102.,102.,2.)]])
    d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*r) for r in ticks]);job,_=rv._enqueue(d,rv._spec(isin,meta,'5m',at,at),{'kind':'fixture'},now);inputs=rv._input(d,job);self.assertIsNone(rw.retime_plan(d,job,inputs,now));old=dict(job);job['input_fingerprint']=inputs['fingerprint'];rv._save_job(d,job,old)
   rv.observe(s,job['id'],isin,rv.digest(s.setting(rv.key(job))),'DIFFERENT','GETTEX:FIX',True);job=s.setting(rv.key(job))
   with s.connect() as d:
    i=rv._input(d,job);plan=rw.retime_plan(d,job,i,now);self.assertEqual(plan['mode'],'VERIFIED_NATIVE_POSITIVE_PRICE_EXTREME');self.assertEqual(plan['first'],a);self.assertEqual(plan['end'],b)
    stale=copy.deepcopy(job);stale['observation']['input_fingerprint']='changed';self.assertIsNone(rw.retime_plan(d,stale,i,now))
    # The neighboring parent is outside the repair but still a captured input.
    d.execute("UPDATE series SET volume=volume+1 WHERE base_tf='5m' AND ts=?",(outside,))
    with self.assertRaisesRegex(ValueError,'RETIME_INPUT_CHANGED'):rw.publish_retime(d,job,i,plan,now)
    d.rollback();d.execute("UPDATE series SET volume=volume+1 WHERE base_tf='15m' AND ts=?",(a,));self.assertIsNone(rw.retime_plan(d,job,rv._input(d,job),now));d.rollback()
   s.set_setting(rv.SOURCE_POLICY,{**policy,'approved':False})
   with s.connect() as d:self.assertIsNone(rw.retime_plan(d,job,rv._input(d,job),now))
   s.set_setting(rv.SOURCE_POLICY,policy)
   with s.connect() as d:
    current=s.setting(rv.key(job));old=dict(current);current.update(status='AWAITING_SOURCE',native_empty_algorithm=rv.NATIVE_EMPTY_ALGORITHM,finding=dict(state='SOURCE_DISCREPANCY'));rv._save_job(d,current,old);d.commit()
    self.assertEqual(len(rv._native_empty_pending(d,isin,8)),1)
    self.assertEqual(len(rv.native_empty_batches(d,[info])),1)
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(s,info,pd.Timestamp.now(tz='UTC'),native_empty_only=True)
   with s.connect() as d:self.assertEqual(rv._native_empty_pending(d,isin,8),[])
   receipt=s.setting(rw.RETIME_PREFIX+isin+':'+job['id']);self.assertEqual((receipt['changed'],receipt['added'],receipt['relocated']),(2,0,0));self.assertEqual(s.setting(rv.key(job))['status'],'RESOLVED_NATIVE')
   with s.connect() as d:
    self.assertTrue(rw.retime_current(d,receipt))
    for base in source:self.assertEqual(rw._native(d,isin,meta,base,a,b),source[base]);self.assertEqual(rw._native(d,isin,meta,base,outside,'2026-11-13 08:00:00'),[(outside,102.,102.,102.,102.,2.)])
    from cp_problem_view import native_source_resolution
    problem=dict(isin=isin,identity=meta,tf='5m',kind='archive',state='UNCONFIRMED',range=[at,at]);self.assertIsNotNone(native_source_resolution(d,problem,[s.setting(rv.key(job))],{}))
    self.assertEqual(d.execute('SELECT count(*) FROM candle_revision_history').fetchone()[0],2)
   self.assertIsNone(s.setting('cp_manual_risk:'+isin))
  finally:
   s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)
if __name__=='__main__':unittest.main()
