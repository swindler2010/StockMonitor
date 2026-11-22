"""A real15m parent can contain a proved empty5m slot; no synthetic rows."""
import copy,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class FiveMinuteEmpty(unittest.TestCase):
 def test_full_paired_tape_and_both_native_tfs_confirm_only_exact_empty_five_minutes(self):
  root=Path(os.environ.get('SM_TEST_ARCHIVE',str(Path(__file__).parent.parent.parent/'СМ-Архив')))
  f=tempfile.NamedTemporaryFile(dir=root,prefix='CP empty5m isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
  try:
   isin='US0378331005';info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');s.save_instrument(info);now=pd.Timestamp.now(tz='UTC')
   m=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='APC.GTX',timezone='Europe/Berlin');om={**m,'source':'gettex','symbol':isin}
   at='2026-11-13 21:15:00';first='2026-11-13 21:00:00';end='2026-11-13 21:30:00'
   children=[('2026-11-13 21:10:00',100.,100.,100.,100.,2.),('2026-11-13 21:20:00',101.,101.,101.,101.,3.)]
   parents=[(first,*children[0][1:]),('2026-11-13 21:15:00',*children[1][1:])];rows={'5m':children,'15m':parents}
   proof=dict(isin=isin,identity=om,basis='complete_published_trade_files',checked_at=now.isoformat(),no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261113':{}})
   for t in pd.date_range(first,end,freq='5min',inclusive='left'):
    field='trade_intervals' if str(t) in {x[0] for x in children} else 'no_trades';proof[field].append([str(t),str(t+pd.Timedelta(minutes=5))])
   for tm in ('20.15','20.30'):
    for board in ('munc','mund'):proof['reference']['GETTEX:20261113']['posttrade.20261113.'+tm+'.'+board+'.csv.gz']='a'*64
   policy=dict(version=rw.RETIME_VERSION,approved=True);sp=dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX')
   s.set_setting(rw.RETIME_POLICY,policy);s.set_setting(rv.SOURCE_POLICY,sp);s.set_setting('interval_evidence:'+isin,proof)
   for b in rows:s.set_setting('gettex_chart:'+isin+':'+b,dict(state='SAVED',identity=m,listing_proof={**m,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
   with s.connect() as d:
    for b in rows:
     d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',b,*r) for r in rows[b]])
     d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[m[k] for k in rw.IDENTITY[:4]],b,*r) for r in rows[b]])
    d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,str(pd.Timestamp(r[0])+pd.Timedelta(minutes=1)),r[1],r[5]) for r in children])
    job,_=rv._enqueue(d,rv._spec(isin,m,'5m',at,at),{'kind':'fixture'},now);inputs=rv._input(d,job)
    d.commit()
    plan=rw.retime_plan(d,job,inputs,now);self.assertIsNotNone(plan);self.assertEqual(plan['mode'],'VERIFIED_NATIVE_EMPTY');self.assertEqual(plan['reviewed_tf'],'5m')
    d.execute("UPDATE series SET volume=volume+1 WHERE base_tf='15m'")
    with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,job,inputs,plan,now)
    d.rollback()
   for key,value in [(rv.SOURCE_POLICY,{**sp,'approved':False}),('interval_evidence:'+isin,{**proof,'reference':{}})]:
    saved=s.setting(key);s.set_setting(key,value)
    with s.connect() as d:
     try:p=rw.retime_plan(d,job,rv._input(d,job),now)
     except ValueError:p=None
     self.assertIsNone(p)
    s.set_setting(key,saved)
   with s.connect() as d:
    conflict={**job,'observation':dict(result='PRESENT',input_fingerprint=rv._input(d,job)['fingerprint'])}
    with self.assertRaisesRegex(ValueError,'VISUAL_REFERENCE_CONFLICT'):rw.retime_plan(d,conflict,rv._input(d,job),now)
   with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No network in saved-evidence batch')):
    # The selector starts only from an existing unresolved native finding.
    with s.connect() as d:
     current=rv.read(d,rv.key(job));old=copy.deepcopy(current);current.update(finding=rv._classify(current,rv._input(d,current),now),status='AWAITING_SOURCE');rv._save_job(d,current,old)
    with s.connect() as d:self.assertEqual([i['isin'] for i in rv.native_empty_batches(d,[info])],[isin])
    result=rv.turn(s,info,pd.Timestamp.now(tz='UTC'),native_empty_only=True)
    self.assertEqual(result['checked'],1)
   receipt=s.setting(rw.RETIME_PREFIX+isin+':'+job['id']);self.assertIsNotNone(receipt);self.assertEqual((receipt['added'],receipt['changed']),(0,0));self.assertEqual(s.setting(rv.key(job))['finding']['state'],'NO_TRADES')
   from cp_problem_view import native_empty_resolution,confirmed_native_absence
   problem=dict(isin=isin,identity=m,tf='5m',kind='archive',state='UNCONFIRMED',range=[at,at])
   with s.connect() as d:
    self.assertTrue(rw.retime_current(d,receipt));resolution=native_empty_resolution(d,problem,[s.setting(rv.key(job))]);self.assertIsNotNone(resolution);self.assertTrue(confirmed_native_absence({**problem,'native_resolution':resolution}))
    for b in rows:self.assertEqual(rw._native(d,isin,m,b,first,end),rows[b])
    self.assertEqual(d.execute('select count(*) from candle_revision_history').fetchone()[0],0)
    d.execute("UPDATE series SET high=high+1 WHERE base_tf='15m'");self.assertFalse(rw.retime_current(d,receipt));d.rollback()
   self.assertIsNone(s.setting('cp_manual_risk:'+isin));s.read_cache.close();s=FlatStore(path)
   with s.connect() as d:
    self.assertTrue(rw.retime_current(d,receipt));self.assertEqual(rv.native_empty_batches(d,[info]),[])
  finally:
   s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

 def test_saved_batch_rotates_registered_stocks_once_and_never_runs_provider_or_core(self):
  from unittest.mock import Mock
  from types import SimpleNamespace
  from feed_worker import FeedContext
  infos=[dict(isin=f'X{i}',kind='stock') for i in range(12)]+[dict(isin='BENCH',kind='benchmark')]
  class DB:
   pass
  db=DB();settings={rw.RETIME_POLICY:dict(approved=True,version=rw.RETIME_VERSION),rv.SOURCE_POLICY:dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX')}
  settings.update({rv.PROGRESS+i['isin']:dict(native_empty_batch_at=f'2026-11-{(10+int(i["isin"][1:])):02d}') for i in infos[:-1]})
  with patch.object(rv,'read',side_effect=lambda db,k,default=None:settings.get(k,default)),patch.object(rv,'_native_empty_pending',return_value=[('job','{}')]):
   selected=rv.native_empty_batches(db,infos);self.assertEqual([x['isin'] for x in selected],[f'X{i}' for i in range(8)])
   settings[rv.PROGRESS+'X0']['native_empty_batch_at']='2026-11-30'
   self.assertEqual(rv.native_empty_batches(db,infos)[0]['isin'],'X1')
   settings[rv.SOURCE_POLICY]['approved']=False;self.assertEqual(rv.native_empty_batches(db,infos),[])
  context=SimpleNamespace(store=SimpleNamespace(setting=Mock(return_value={})),_calculate=Mock(side_effect=AssertionError('No acquisition/CORE route')))
  with patch.object(rv,'turn',return_value={'checked':8}) as normal:
   self.assertEqual(FeedContext.calculate(context,dict(isin='X0',_native_empty_only=True)),{'errors':[],'cp_native_empty':{'checked':8}})
   normal.assert_called_once_with(context.store,dict(isin='X0',_native_empty_only=True),native_empty_only=True)
   context._calculate.assert_not_called()

if __name__=='__main__':unittest.main()
