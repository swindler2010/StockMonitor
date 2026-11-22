"""One real interior price omitted from sampling, never from quantity."""
import copy,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv,revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class SampledPartition(unittest.TestCase):
 def test_unique_interior_price_keeps_all_quantities_final_guards_and_normal_queue(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив';f=tempfile.NamedTemporaryFile(dir=root,prefix='CP sampled price isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
  try:
   isin='US0378331005';now=pd.Timestamp.now(tz='UTC');info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');s.save_instrument(info)
   m=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='FIX.GTX',timezone='Europe/Berlin');om={**m,'source':'gettex','symbol':isin}
   def t(x):return str(pd.Timestamp('2026-11-13 '+x+':00')+pd.Timedelta(hours=1))
   ticks=[(t('07:20'),100.,2.),(t('07:24'),99.,12.),(t('07:30'),100.,12.),(t('07:35'),98.,7.),(t('07:38'),99.,81.),(t('07:45'),101.,5.),(t('08:00'),97.,10.),(t('08:13'),100.,0.)]
   def candles(events,tf):
    out=[]
    for at in sorted({pd.Timestamp(r[0]).floor(tf+'min') for r in events}):
     part=[r for r in events if at<=pd.Timestamp(r[0])<at+pd.Timedelta(minutes=int(tf))];prices=[r[1] for r in part];out.append((str(at),prices[0],max(prices),min(prices),prices[-1],sum(r[2] for r in part)))
    return out
   native5=[(t('07:20'),100.,100.,100.,100.,2.),(t('07:55'),99.,99.,99.,99.,12.),(t('08:00'),100.,100.,99.,99.,100.),(t('08:05'),101.,101.,101.,101.,5.),(t('08:10'),97.,100.,97.,100.,10.)]
   def parents(rows):
    return [(str(at),*rw._aggregate([r for r in rows if at<=pd.Timestamp(r[0])<at+pd.Timedelta(minutes=15)])) for at in sorted({pd.Timestamp(r[0]).floor('15min') for r in rows})]
   native={'5m':native5,'15m':parents(native5)};official={b:candles(ticks,b[:-1]) for b in native}
   self.assertIsNone(rw._trade_partition(native5,ticks,max_delay=45));sampled=rw._sampled_price_partition(native5,ticks);self.assertIsNotNone(sampled);self.assertEqual(sampled[1][0]['event'],ticks[3])
   wrong=copy.deepcopy(native5);wrong[2]=(*wrong[2][:-1],101.);self.assertIsNone(rw._sampled_price_partition(wrong,ticks))
   # Opening/final prices and zero-quantity extrema are outside this rule.
   opening=[(t('08:00'),99.,99.,99.,99.,3.)];self.assertIsNone(rw._sampled_price_partition(opening,[(t('07:30'),98.,1.),(t('07:31'),99.,2.)]));self.assertIsNone(rw._sampled_price_partition(opening,[(t('07:30'),99.,2.),(t('07:31'),98.,1.)]))
   zero=list(ticks);zero[3]=(ticks[3][0],98.,0.);self.assertIsNone(rw._sampled_price_partition(native5,zero))
   proof=dict(isin=isin,identity=om,basis='complete_published_trade_files',checked_at=now.isoformat(),no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261113':{}})
   for at in pd.date_range(t('07:15'),t('08:15'),freq='5min',inclusive='left'):
    rows=[r for r in official['5m'] if r[0]==str(at)];field='no_trades' if not rows else 'zero_quantity_intervals' if rows[0][5]==0 else 'trade_intervals';proof[field].append([str(at),str(at+pd.Timedelta(minutes=5))])
   for clock in ('07.30','07.45','08.00','08.15'):
    for board in ('munc','mund'):proof['reference']['GETTEX:20261113'][f'posttrade.20261113.{clock}.{board}.csv.gz']='a'*64
   s.set_setting(rv.SOURCE_POLICY,dict(approved=True,version=1,scope='registered_stocks',exclusive_venue='GETTEX'));s.set_setting(rw.RETIME_POLICY,dict(approved=True,version=rw.RETIME_VERSION));s.set_setting('interval_evidence:'+isin,proof)
   for base in native:s.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=m,listing_proof={**m,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
   with s.connect() as d:
    for base in native:
     d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[m[k] for k in rw.IDENTITY[:4]],base,*r) for r in native[base]])
     d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in official[base]])
    d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*r) for r in ticks]);job,_=rv._enqueue(d,rv._spec(isin,m,'5m',t('07:25'),t('07:50')),dict(kind='fixture'),now);i=rv._input(d,job);old=dict(job);job.update(input_fingerprint=i['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION);rv._save_job(d,job,old)
   rv.observe(s,job['id'],isin,rv.digest(s.setting(rv.key(job))),'DIFFERENT','GETTEX:FIX',True);job=s.setting(rv.key(job))
   with s.connect() as d:
    i=rv._input(d,job);plan=rw.retime_plan(d,job,i,now);self.assertIsNotNone(plan);self.assertEqual(plan['sampled_positive_interior_price'][0]['event'],ticks[3]);self.assertEqual(plan['before'],native)
    self.assertEqual(rw._aggregate(native5),rw._aggregate(official['5m']));self.assertEqual(sum(r[5] for r in native5),129.)
    present={**job,'observation':{**job['observation'],'result':'PRESENT'}};self.assertIsNone(rw.retime_plan(d,present,i,now))
    d.execute("UPDATE series SET volume=volume+1 WHERE base_tf='15m' AND ts=?",(t('08:00'),));self.assertIsNone(rw.retime_plan(d,job,rv._input(d,job),now));d.rollback()
    changed=copy.deepcopy(proof);changed['reference']['GETTEX:20261113'].pop('posttrade.20261113.08.15.mund.csv.gz');rv.write(d,'interval_evidence:'+isin,changed)
    with self.assertRaisesRegex(ValueError,'RETIME_INPUT_CHANGED'):rw.publish_retime(d,job,i,plan,now)
    d.rollback()
   with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No network')):rv.turn(s,info,now,native_empty_only=True)
   self.assertEqual(s.setting(rv.key(job))['status'],'RESOLVED_NATIVE');receipt=s.setting(rw.RETIME_PREFIX+isin+':'+job['id'])
   with s.connect() as d:
    self.assertTrue(rw.retime_current(d,receipt))
    for base in native:self.assertEqual(rw._native(d,isin,m,base,t('07:15'),t('08:15')),official[base])
   self.assertIsNone(s.setting('cp_manual_risk:'+isin))
  finally:
   s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
