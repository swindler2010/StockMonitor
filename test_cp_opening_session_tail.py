"""Bounded source recovery: price-only opening sampling and missing session tail."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv,revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

def stamp(clock):return '2026-11-13 '+clock+':00'
def parents(rows):
 return [(str(a),*rw._aggregate([r for r in rows if a<=pd.Timestamp(r[0])<a+pd.Timedelta(minutes=15)])) for a in sorted({pd.Timestamp(r[0]).floor('15min') for r in rows})]
def candles(ticks,minutes):
 return [(str(a),*rw._aggregate([(t,p,p,p,p,q) for t,p,q in ticks if a<=pd.Timestamp(t)<a+pd.Timedelta(minutes=minutes)])) for a in sorted({pd.Timestamp(t).floor(str(minutes)+'min') for t,p,q in ticks})]

class OpeningSessionTail(unittest.TestCase):
 def fixture(self,native5,ticks,first,end,review,last,result='DIFFERENT'):
  root=Path(__file__).parent.parent.parent/'СМ-Архив';f=tempfile.NamedTemporaryFile(dir=root,prefix='CP opening tail isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
  self.addCleanup(self.clean,s,path)
  isin='US0378331005';now=pd.Timestamp.now(tz='UTC');info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');s.save_instrument(info)
  m=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='FIX.GTX',timezone='Europe/Berlin');om={**m,'source':'gettex','symbol':isin}
  native={'5m':native5,'15m':parents(native5)};official={b:candles(ticks,int(b[:-1])) for b in native}
  proof=dict(isin=isin,identity=om,basis='complete_published_trade_files',checked_at=now.isoformat(),no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261113':{}})
  for a in pd.date_range(first,end,freq='5min',inclusive='left'):
   rows=[r for r in official['5m'] if r[0]==str(a)];field='no_trades' if not rows else 'zero_quantity_intervals' if rows[0][5]==0 else 'trade_intervals';proof[field].append([str(a),str(a+pd.Timedelta(minutes=5))])
  for a in pd.date_range(first,end,freq='15min',inclusive='left'):
   right=(a+pd.Timedelta(minutes=15)).tz_localize(m['timezone']).tz_convert('UTC')
   for board in ('munc','mund'):proof['reference']['GETTEX:20261113'][f'posttrade.{right:%Y%m%d.%H.%M}.{board}.csv.gz']='a'*64
  s.set_setting(rv.SOURCE_POLICY,dict(approved=True,version=1,scope='registered_stocks',exclusive_venue='GETTEX'));s.set_setting(rw.RETIME_POLICY,dict(approved=True,version=rw.RETIME_VERSION));s.set_setting('interval_evidence:'+isin,proof)
  for base in native:s.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=m,listing_proof={**m,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
  with s.connect() as d:
   for base in native:
    d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[m[k] for k in rw.IDENTITY[:4]],base,*r) for r in native[base]])
    d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in official[base]])
   d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*r) for r in ticks]);job,_=rv._enqueue(d,rv._spec(isin,m,'5m',review,last),dict(kind='fixture'),now);i=rv._input(d,job);old=dict(job);job.update(input_fingerprint=i['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION);rv._save_job(d,job,old)
  if result is not None:rv.observe(s,job['id'],isin,rv.digest(s.setting(rv.key(job))),result,'GETTEX:FIX',True)
  return s,info,m,s.setting(rv.key(job)),native,official,proof,now

 @staticmethod
 def clean(s,path):
  s.read_cache.close()
  for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

 def test_opening_burst_and_disjoint_current_reviews_recompose_positive_single_child_atomically(self):
  t=stamp;ticks=[(t('17:33'),137.,5.),(t('17:37'),134.8,8.),(t('17:38'),134.8,6.),(t('17:39'),134.7,12.),(t('17:39')+'.500000',134.75,5.),(t('17:42'),134.9,4.),(t('17:44'),135.6,0.),(t('18:07'),134.3,30.),(t('18:14'),134.4,0.),(t('18:26'),134.8,5.)]
  native5=[(t('17:30'),137.,137.,137.,137.,5.),(t('17:35'),134.7,134.75,134.7,134.75,31.),(t('17:45'),134.9,135.6,134.9,135.6,4.),(t('18:15'),134.3,134.4,134.3,134.4,30.),(t('18:25'),134.8,134.8,134.8,134.8,5.)]
  s,info,m,j,native,official,proof,now=self.fixture(native5,ticks,t('17:30'),t('18:30'),t('17:35'),t('17:35'))
  isin=info['isin']
  with s.connect() as d:
   other,_=rv._enqueue(d,rv._spec(isin,m,'5m',t('17:50'),t('18:10')),dict(kind='related'),now);i=rv._input(d,other);old=dict(other);other.update(input_fingerprint=i['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION);rv._save_job(d,other,old)
  rv.observe(s,other['id'],isin,rv.digest(s.setting(rv.key(other))),'DIFFERENT','GETTEX:FIX',True)
  with s.connect() as d:
   i=rv._input(d,j);plan=rw.retime_plan(d,j,i,now);self.assertIsNotNone(plan);self.assertEqual(len(plan['sampled_positive_opening_burst']),2);self.assertIn(other['id'],plan['co_review_inputs']);self.assertEqual(plan['end'],t('18:30'))
   positive=next(r for r in plan['relocations'] if r['base_tf']=='15m' and r['old_at']==t('17:45'));self.assertEqual(positive['recomposed_into'],[t('17:30')])
   saved=rv.read(d,rv.key(other));changed={**saved,'observation':{**saved['observation'],'result':'ABSENT'}};rv.write(d,rv.key(other),changed)
   with self.assertRaisesRegex(ValueError,'RETIME_'):rw.retime_plan(d,j,rv._input(d,j),now)
   with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,j,i,plan,now)
   d.rollback()
   d.execute("UPDATE series SET volume=volume+1 WHERE base_tf='15m' AND ts=?",(t('17:45'),));self.assertIsNone(rw.retime_plan(d,j,rv._input(d,j),now));d.rollback()
  bad=list(ticks);bad[2]=(bad[2][0],134.85,bad[2][2]);self.assertIsNone(rw._opening_price_partition(native5,bad));zero=list(ticks);zero[1]=(zero[1][0],zero[1][1],0.);self.assertIsNone(rw._opening_price_partition(native5,zero))
  with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No network')):rv.turn(s,info,now,native_empty_only=True)
  self.assertEqual(s.setting(rv.key(j))['status'],'RESOLVED_NATIVE')
  receipt=s.setting(rw.RETIME_PREFIX+isin+':'+j['id'])
  with s.connect() as d:
   self.assertTrue(rw.retime_current(d,receipt))
   for b in native:self.assertEqual(rw._native(d,isin,m,b,t('17:30'),t('18:30')),official[b])
  self.assertIsNone(s.setting('cp_manual_risk:'+isin))

 def test_53_and_200_new_five_minute_passes_preserve_completed_and_future_jobs(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив'
  for count in (53,200):
   with self.subTest(count=count):
    f=tempfile.NamedTemporaryFile(dir=root,prefix='CP opening lane isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path);self.addCleanup(self.clean,s,path)
    now=pd.Timestamp.now(tz='UTC');isin='US0378331005';m=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='FIX.GTX',timezone='Europe/Berlin')
    with s.connect() as d:
     for n in range(count):
      at=str(pd.Timestamp(stamp('08:00'))+pd.Timedelta(minutes=5*n));j=rv._spec(isin,m,'5m',at,at);j.update(status='RESOLVED_NATIVE' if n%4==2 else 'AWAITING_SOURCE',created_at=(now-pd.Timedelta(days=1)).isoformat(),next_check=(now+pd.Timedelta(hours=1) if n%4==1 else now-pd.Timedelta(minutes=1)).isoformat(),native_empty_algorithm=12 if n%4==3 else 11,positive_price_algorithm=1,finding=dict(state='SOURCE_DISCREPANCY'),observation=dict(result='DIFFERENT',at=now.isoformat()));rv.write(d,rv.key(j),j)
     page=rv._native_empty_pending(d,isin,8);self.assertEqual(len(page),8)
     for key,raw in page:
      j=json.loads(raw);self.assertEqual(j['status'],'AWAITING_SOURCE');self.assertEqual(j['native_empty_algorithm'],11);self.assertLess(pd.Timestamp(j['next_check']),now);rv.write(d,key,{**j,'native_empty_algorithm':12})
     self.assertFalse({k for k,v in page}&{k for k,v in rv._native_empty_pending(d,isin,8)})
     self.assertEqual(d.execute('select count(*) from candles').fetchone()[0],0)

 def test_session_tail_maps_every_native_quantity_and_recovers_only_proved_positive_final_events(self):
  t=stamp;ticks=[(t('22:16'),100.,10.),(t('22:21'),101.,20.),(t('22:28'),102.,2.),(t('22:31'),103.,3.),(t('22:36'),104.,4.),(t('22:41'),105.,5.),(t('22:46'),106.,6.),(t('22:51'),107.,7.),(t('22:57'),108.,8.)]
  native5=[(t('22:15'),100.,100.,100.,100.,10.),(t('22:20'),101.,101.,101.,101.,20.),(t('22:30'),102.,102.,102.,102.,2.),(t('22:35'),103.,103.,103.,103.,3.),(t('22:40'),104.,104.,104.,104.,4.),(t('22:45'),105.,105.,105.,105.,5.),(t('22:50'),106.,106.,106.,106.,6.),(t('22:55'),107.,107.,107.,107.,7.)]
  s,info,m,j,native,official,proof,now=self.fixture(native5,ticks,t('22:15'),t('23:00'),t('22:25'),t('22:25'),'PRESENT');isin=info['isin']
  with s.connect() as d:
   i=rv._input(d,j);plan=rw.retime_plan(d,j,i,now);self.assertIsNotNone(plan);self.assertEqual(plan['mode'],'VERIFIED_NATIVE_SESSION_TAIL');self.assertEqual(plan['recovered_session_tail'],[ticks[-1]]);self.assertEqual(len(plan['native_trade_partition']),8)
   d.execute("UPDATE series SET high=high+1 WHERE base_tf='5m' AND ts=?",(t('22:20'),));self.assertIsNone(rw.retime_plan(d,j,rv._input(d,j),now));d.rollback()
   changed=copy.deepcopy(proof);changed['reference']['GETTEX:20261113'].pop('posttrade.20261113.22.00.mund.csv.gz');rv.write(d,'interval_evidence:'+isin,changed)
   with self.assertRaisesRegex(ValueError,'RETIME_INPUT_CHANGED'):rw.publish_retime(d,j,i,plan,now)
   d.rollback()
   absent={**j,'observation':{**j['observation'],'result':'ABSENT'}}
   self.assertIsNone(rw.retime_plan(d,absent,i,now))
   d.execute("UPDATE trades SET volume=0 WHERE ts=?",(ticks[-1][0],));self.assertIsNone(rw.retime_plan(d,j,rv._input(d,j),now));d.rollback()
  with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No network')):rv.turn(s,info,now,native_empty_only=True)
  self.assertEqual(s.setting(rv.key(j))['status'],'RESOLVED_NATIVE');receipt=s.setting(rw.RETIME_PREFIX+isin+':'+j['id'])
  with s.connect() as d:
   self.assertTrue(rw.retime_current(d,receipt))
   for b in native:self.assertEqual(rw._native(d,isin,m,b,t('22:15'),t('23:00')),official[b])
  self.assertIsNone(s.setting('cp_manual_risk:'+isin))

if __name__=='__main__':unittest.main()
