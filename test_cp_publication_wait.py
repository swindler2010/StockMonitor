"""A partial official pair stays blocked in the existing source-recovery lane."""
import copy,json,os,sqlite3,sys,tempfile,unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import paper_account
from test_cp_tv_legacy_reference import FlatStore

class PublicationWait(unittest.TestCase):
 def test_partial_pair_waits_for_source_then_normal_turn_resolves_without_candle_writes(self):
  with tempfile.NamedTemporaryFile(suffix='.sqlite3',prefix='cp-source-wait-',dir=os.environ.get('SM_TEST_TMP'),delete=False) as f:path=Path(f.name)
  store=FlatStore(path)
  try:
   info=dict(isin='US0378331005',name='Fixture',kind='stock');store.save_instrument(info)
   meta=dict(source='gettex',symbol=info['isin'],venue='GETTEX',currency='EUR',timezone='Europe/Berlin');now=pd.Timestamp('2026-11-23T08:00Z')
   job=rv._spec(info['isin'],meta,'15m','2026-11-20 09:00:00','2026-11-20 09:00:00')
   for at in ('2026-11-20 08:45:00','2026-11-20 09:15:00'):
    with store.connect() as db:db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(info['isin'],'gettex','15m',at,100,101,99,100,10))
   proof=dict(isin=info['isin'],identity=meta,basis='complete_published_trade_files',checked_at=now.isoformat(),reference={'GETTEX:20261120':{'fixture':'a'*64}},no_trades=[],trade_intervals=[],zero_quantity_intervals=[],incomplete_publication_intervals=[['2026-11-20 09:00:00','2026-11-20 09:15:00']])
   store.set_setting('interval_evidence:'+info['isin'],proof)
   with store.connect() as db:
    paper_account.initialize(db)
    rv._enqueue(db,job,dict(kind='fixture'),now);inputs=rv._input(db,job);before=db.execute('SELECT * FROM candles').fetchall()
   finding=rv._classify(job,inputs,now);self.assertEqual(finding['state'],'UNCONFIRMED');self.assertFalse(finding['can_affect_dq']);self.assertTrue(finding['provider_wait'])
   for changed in ('wrong_source','wrong_identity','unrelated_interval','unknown_basis'):
    altered=copy.deepcopy(inputs);candidate=copy.deepcopy(job)
    if changed=='wrong_source':candidate['identity']['source']='gettex_chart'
    elif changed=='wrong_identity':altered['proof']['identity']['currency']='USD'
    elif changed=='unrelated_interval':altered['proof']['incomplete_publication_intervals']=[['2026-11-20 10:00:00','2026-11-20 10:15:00']]
    else:altered['proof']['basis']='unknown'
    self.assertFalse(rv._publication_wait(candidate,altered))
   with store.connect() as db:
    db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(info['isin'],'gettex','15m',job['first'],100,101,99,100,10));partial=rv._input(db,job)
   self.assertEqual(rv._classify(job,partial,now)['state'],'NATIVE_ROWS_PRESENT');self.assertTrue(rv._classify(job,partial,now)['provider_wait'])
   with store.connect() as db:
    db.execute('UPDATE candles SET high=98 WHERE ts=?',(job['first'],));invalid=rv._input(db,job)
   self.assertEqual(rv._classify(job,invalid,now)['state'],'INVALID');self.assertNotIn('provider_wait',rv._classify(job,invalid,now))
   with store.connect() as db:db.execute('DELETE FROM candles WHERE ts=?',(job['first'],))
   with store.connect() as db:self.assertEqual(len(rv._native_empty_pending(db,info['isin'],8)),1)
   rv.turn(store,info,now,native_empty_only=True)
   saved=store.setting(rv.key(job));self.assertEqual(saved['status'],'AWAITING_SOURCE');self.assertFalse(saved['review_required']);self.assertIn('SOURCE_COVERAGE_UNCONFIRMED',saved['finding']['reason']);self.assertEqual(pd.Timestamp(saved['next_check']),now+pd.Timedelta(hours=1))
   self.assertEqual(saved['source_publication_wait_algorithm'],1)
   with store.connect() as db:self.assertEqual(rv._native_empty_pending(db,info['isin'],8),[])
   proof['incomplete_publication_intervals']=[];proof['no_trades']=[[job['first'],job['end']]];store.set_setting('interval_evidence:'+info['isin'],proof)
   rv.turn(store,info,now+pd.Timedelta(hours=1,seconds=1));saved=store.setting(rv.key(job));self.assertEqual(saved['status'],'RESOLVED_NATIVE');self.assertEqual(saved['finding']['state'],'NO_TRADES')
   with store.connect() as db:self.assertEqual(db.execute('SELECT * FROM candles').fetchall(),before);self.assertEqual(db.execute('SELECT count(*) FROM paper_fills').fetchone()[0],0)
  finally:
   store.read_cache.close()
   for candidate in (path,Path(str(path)+'-wal'),Path(str(path)+'-shm')):candidate.unlink(missing_ok=True)

 def test_53_200_partial_requests_have_bounded_one_pass_without_changing_deadlines(self):
  for count in (53,200):
   with self.subTest(count=count),sqlite3.connect(':memory:') as db:
    db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)');isin='US0378331005';now=pd.Timestamp.now(tz='UTC');meta=dict(source='gettex',symbol=isin,venue='GETTEX',currency='EUR',timezone='Europe/Berlin');spans=[]
    for n in range(count):
     a=pd.Timestamp('2026-11-13 07:30:00')+pd.Timedelta(minutes=n*15);j=rv._spec(isin,meta,'15m',str(a),str(a));j.update(status='RESOLVED_NATIVE' if n%5==0 else 'AWAITING_USER',created_at=(now-pd.Timedelta(days=1)).isoformat(),next_check=(now+pd.Timedelta(hours=1) if n%5==1 else now-pd.Timedelta(minutes=1)).isoformat(),source_publication_wait_algorithm=1 if n%5==2 else 0);rv.write(db,rv.key(j),j)
     if n%5!=3:spans.append([j['first'],j['end']])
    rv.write(db,'interval_evidence:'+isin,dict(isin=isin,identity=meta,basis='complete_published_trade_files',incomplete_publication_intervals=spans));before=db.total_changes;selected=rv._native_empty_pending(db,isin,8);self.assertEqual(len(selected),8);self.assertEqual(db.total_changes,before)
    originals={key:json.loads(raw) for key,raw in selected}
    for key,j in originals.items():
     self.assertEqual(j['status'],'AWAITING_USER');self.assertLess(pd.Timestamp(j['next_check']),now);self.assertEqual(j['source_publication_wait_algorithm'],0);self.assertIn([j['first'],j['end']],spans);rv.write(db,key,{**j,'source_publication_wait_algorithm':1})
    after=rv._native_empty_pending(db,isin,8);self.assertFalse(set(originals)&{key for key,_ in after})
    for key,j in originals.items():self.assertEqual(rv.read(db,key)['next_check'],j['next_check'])

if __name__=='__main__':unittest.main()
