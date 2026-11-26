"""A real zero event changes parent time; no synthetic observations."""
import copy,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv,revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class ZeroParent(unittest.TestCase):
 def test_complete_review_scope_and_distinct_zero_events_recompose_only_zero_parent(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив';f=tempfile.NamedTemporaryFile(dir=root,prefix='CP zero parent isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
  try:
   isin='US0378331005';now=pd.Timestamp.now(tz='UTC');info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');s.save_instrument(info)
   m=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='FIX.GTX',timezone='Europe/Berlin');om={**m,'source':'gettex','symbol':isin}
   def t(x):return '2026-11-13 '+x+':00'
   ticks=[(t('07:30'),100.,3.),(t('07:35'),101.,2.),(t('07:40'),101.,0.),(t('08:00'),102.,5.)]
   official5=[(at,p,p,p,p,q) for at,p,q in ticks];native5=[official5[0],(t('07:40'),*official5[1][1:]),(t('07:45'),*official5[2][1:]),(t('08:05'),*official5[3][1:])]
   def parents(rows):
    return [(str(at),*rw._aggregate([r for r in rows if at<=pd.Timestamp(r[0])<at+pd.Timedelta(minutes=15)])) for at in sorted({pd.Timestamp(r[0]).floor('15min') for r in rows})]
   native={'5m':native5,'15m':parents(native5)};official={'5m':official5,'15m':parents(official5)}
   proof=dict(isin=isin,identity=om,basis='complete_published_trade_files',checked_at=now.isoformat(),no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261113':{}})
   for at in pd.date_range(t('07:30'),t('08:15'),freq='5min',inclusive='left'):
    rows=[r for r in official5 if r[0]==str(at)];field='no_trades' if not rows else 'zero_quantity_intervals' if rows[0][5]==0 else 'trade_intervals';proof[field].append([str(at),str(at+pd.Timedelta(minutes=5))])
   for clock in ('06.45','07.00','07.15'):
    for board in ('munc','mund'):proof['reference']['GETTEX:20261113'][f'posttrade.20261113.{clock}.{board}.csv.gz']='a'*64
   s.set_setting(rv.SOURCE_POLICY,dict(approved=True,version=1,scope='registered_stocks',exclusive_venue='GETTEX'));s.set_setting(rw.RETIME_POLICY,dict(approved=True,version=rw.RETIME_VERSION));s.set_setting('interval_evidence:'+isin,proof)
   for base in native:s.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=m,listing_proof={**m,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
   with s.connect() as d:
    for base in native:
     d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[m[k] for k in rw.IDENTITY[:4]],base,*r) for r in native[base]])
     d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in official[base]])
    d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*r) for r in ticks]);job,_=rv._enqueue(d,rv._spec(isin,m,'5m',t('07:50'),t('08:00')),dict(kind='fixture'),now);i=rv._input(d,job);old=dict(job);job.update(input_fingerprint=i['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION);rv._save_job(d,job,old)
   rv.observe(s,job['id'],isin,rv.digest(s.setting(rv.key(job))),'DIFFERENT','GETTEX:FIX',True);job=s.setting(rv.key(job))
   with s.connect() as d:
    i=rv._input(d,job);plan=rw.retime_plan(d,job,i,now);self.assertIsNotNone(plan)
    self.assertLessEqual(pd.Timestamp(plan['first']),pd.Timestamp(job['first']));self.assertGreaterEqual(pd.Timestamp(plan['end']),pd.Timestamp(job['end']))
    move=next(r for r in plan['relocations'] if r['base_tf']=='15m' and r['old_at']==t('07:45'));self.assertEqual(move['recomposed_into'],[t('07:30')]);self.assertEqual(move['child_event_ranges'],[dict(old_at=t('07:45'),start=2,stop=3)])
    # A positive or differently priced parent is never discarded under this
    # zero-event exception; changed source evidence also fails publication.
    d.execute("UPDATE series SET volume=1 WHERE base_tf='15m' AND ts=?",(t('07:45'),));self.assertIsNone(rw.retime_plan(d,job,rv._input(d,job),now));d.rollback()
    changed=copy.deepcopy(proof);changed['reference']['GETTEX:20261113'].pop('posttrade.20261113.07.15.mund.csv.gz');rv.write(d,'interval_evidence:'+isin,changed)
    with self.assertRaisesRegex(ValueError,'RETIME_INPUT_CHANGED'):rw.publish_retime(d,job,i,plan,now)
    d.rollback()
   with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No network')):rv.turn(s,info,now,native_empty_only=True)
   self.assertEqual(s.setting(rv.key(job))['status'],'RESOLVED_NATIVE');receipt=s.setting(rw.RETIME_PREFIX+isin+':'+job['id'])
   with s.connect() as d:
    self.assertTrue(rw.retime_current(d,receipt))
    for base in native:self.assertEqual(rw._native(d,isin,m,base,t('07:30'),t('08:15')),official[base])
    self.assertEqual(sum(r[5] for r in rw._native(d,isin,m,'5m',t('07:30'),t('08:15'))),10.)
   self.assertEqual(receipt['before']['15m'],[list(r) for r in native['15m']]);self.assertIsNone(s.setting('cp_manual_risk:'+isin))
  finally:
   s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
