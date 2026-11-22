"""New reviews reuse the serial short route after its prior algorithm pass."""
import json,sys,tempfile,unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv,revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class ShortReviewQueue(unittest.TestCase):
 def test_53_and_200_due_reviews_take_bounded_slots_without_replaying_done_or_future_jobs(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив'
  for count in (53,200):
   with self.subTest(count=count):
    f=tempfile.NamedTemporaryFile(dir=root,prefix='CP short review isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
    try:
     now=pd.Timestamp.now(tz='UTC');isin='US0378331005';meta=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='FIX.GTX',timezone='Europe/Berlin');info=dict(isin=isin,kind='stock')
     s.set_setting(rw.RETIME_POLICY,dict(approved=True,version=rw.RETIME_VERSION));policy=dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX');s.set_setting(rv.SOURCE_POLICY,policy)
     with s.connect() as db:
      for n in range(count):
       stamp=str(pd.Timestamp('2026-11-13 07:30:00')+pd.Timedelta(minutes=n*5));job=rv._spec(isin,meta,'5m' if n%2 else '15m',stamp,stamp)
       state=('RECHECK_QUEUED','QUEUED','RESOLVED_NATIVE','AWAITING_SOURCE','RECHECK_QUEUED')[n%5]
       job.update(status=state,created_at=(now-pd.Timedelta(hours=2)).isoformat(),next_check=(now+pd.Timedelta(hours=1) if n%5==4 else now-pd.Timedelta(minutes=1)).isoformat(),native_empty_algorithm=9,positive_price_algorithm=1,finding=dict(state='SOURCE_TIMEBASE_UNCONFIRMED'),observation=dict(result='ABSENT',at=now.isoformat()))
       rv.write(db,rv.key(job),job)
     with s.connect() as db:
      page=rv._native_empty_pending(db,isin,8);self.assertEqual(len(page),8)
      selected=[json.loads(raw) for _,raw in page];self.assertTrue(all(j['status']=='RECHECK_QUEUED' and pd.Timestamp(j['next_check'])<now for j in selected))
      self.assertEqual([i['isin'] for i in rv.native_empty_batches(db,[info])],[isin]);self.assertEqual(rv._native_empty_pending(db,'OTHER',8),[])
      self.assertEqual(db.execute('select count(*) from candles').fetchone()[0],0)
      # Completion removes these requests from the short route, while the
      # remaining due reviews continue; no deadline or DQ is renewed by read.
      before=[rv.read(db,key) for key,_ in page]
      for key,_ in page:
       j=rv.read(db,key);j['status']='RESOLVED_NATIVE';rv.write(db,key,j)
      after=rv._native_empty_pending(db,isin,8);self.assertFalse(set(k for k,_ in after)&set(k for k,_ in page))
      for prior,(key,_) in zip(before,page):self.assertEqual(rv.read(db,key)['next_check'],prior['next_check'])
     s.set_setting(rv.SOURCE_POLICY,{**policy,'approved':False})
     with s.connect() as db:self.assertEqual(rv.native_empty_batches(db,[info]),[])
    finally:
     s.read_cache.close()
     for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
