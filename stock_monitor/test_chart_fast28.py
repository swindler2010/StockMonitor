import copy,importlib.util,unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import pandas as pd
import analytics as a
import data_quality as q

BEFORE=Path(__file__).resolve().parent.parent/'history64-evidence/chart-fast28/before'
def legacy(name):
 spec=importlib.util.spec_from_file_location('legacy_fast28_'+name,BEFORE/(name+'.py'))
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
oldq=legacy('data_quality');olda=legacy('analytics')
TF=('5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M')
NOW=pd.Timestamp('2026-10-28T16:00:00Z')
META=dict(source='proxy',symbol='APC.F',venue='FRA',currency='EUR',timezone='Europe/Berlin')

class CalendarGrid(unittest.TestCase):
 def setUp(self):q._intraday_grid.cache_clear()
 def test_all_timeframes_calendar_dst_holidays_equal_reference(self):
  for venue in ('GETTEX','GER','FRA'):
   for start,end in [('2026-03-27','2026-04-07'),('2026-10-23','2026-10-28')]:
    for tf in TF:
     meta=dict(META,venue=venue)
     with self.subTest(venue=venue,start=start,tf=tf):
      expected=oldq.expected_index(meta,tf,start,end,NOW)
      pd.testing.assert_index_equal(q.expected_index(meta,tf,start,end,NOW),expected)
      pd.testing.assert_index_equal(q.expected_index(meta,tf,start,end,NOW),expected)
 def test_publication_time_freshness_and_invalid_timezone_rechecked(self):
  meta=dict(META,venue='GETTEX');first=pd.Timestamp('2026-10-28 07:30');last=first+pd.Timedelta(minutes=15)
  for now,count in [('2026-10-28T06:59:59Z',0),('2026-10-28T07:00:00Z',1),('2026-10-28T07:15:00Z',2)]:
   result=q.expected_index(meta,'15m',first,last,pd.Timestamp(now));self.assertEqual(len(result),count)
  self.assertEqual(q._intraday_grid.cache_info().misses,1)
  for bad in [dict(meta,timezone='UTC'),dict(meta,venue='UNKNOWN')]:
   with self.assertRaises(ValueError):q.expected_index(bad,'15m',first,last,NOW)
  with self.assertRaises(ValueError):q.expected_index(meta,'15m','2025-01-01','2025-01-02',NOW)
 def test_breaks_early_close_and_return_value_cannot_corrupt_cache(self):
  schedule=pd.DataFrame(dict(open=[pd.Timestamp('2026-10-28T08:00Z')],close=[pd.Timestamp('2026-10-28T12:30Z')],
   break_start=[pd.Timestamp('2026-10-28T10:00Z')],break_end=[pd.Timestamp('2026-10-28T11:00Z')]),index=pd.DatetimeIndex(['2026-10-28']))
  with patch.object(q,'calendar',return_value=SimpleNamespace(schedule=schedule)),patch.object(oldq,'calendar',return_value=SimpleNamespace(schedule=schedule)):
   for tf in TF[:8]:
    expected=oldq.expected_index(dict(META,venue='GER'),tf,'2026-10-28','2026-10-29',NOW)
    result=q.expected_index(dict(META,venue='GER'),tf,'2026-10-28','2026-10-29',NOW)
    pd.testing.assert_index_equal(result,expected)
    if len(result):
     try:result.values[0]=np.datetime64('2000-01-01')
     except ValueError:pass # pandas may expose a read-only view; also safe.
    pd.testing.assert_index_equal(q.expected_index(dict(META,venue='GER'),tf,'2026-10-28','2026-10-29',NOW),expected)
 def test_cached_calendar_does_not_cache_ohlcv_or_no_trades_decisions(self):
  f=sample();f=f.iloc[:34].copy();f.attrs.update(timeframe='15m',classify_absence=True)
  f.index=pd.date_range('2026-10-27 09:00',periods=34,freq='15min');f.attrs.update(venue='GER')
  q.audit(f,'15m',now=NOW)
  bad=f.copy();bad.iloc[4,bad.columns.get_loc('high')]=1
  self.assertEqual(q.audit(bad,'15m',now=NOW)['state'],'INVALID')
  hole=f.drop(f.index[5]);report=q.audit(hole,'15m',now=NOW)
  self.assertEqual(report['state'],'UNCONFIRMED')
  self.assertGreater(report['missing_count'],0)
  hole.attrs['absence_evidence']=dict(identity={k:hole.attrs[k] for k in q.IDENTITY},basis='complete_published_trade_files',
   reference='isolated proof',checked_at=NOW.isoformat(),no_trades=[[str(f.index[5]),str(f.index[6])]],trade_intervals=[])
  self.assertEqual(q.audit(hole,'15m',now=NOW)['missing_count'],report['missing_count']-1)
  hole.attrs['absence_evidence']['identity']['currency']='USD'
  self.assertEqual(q.audit(hole,'15m',now=NOW)['missing_count'],report['missing_count'])

def sample():
 close=100+np.sin(np.arange(140)/3)+np.arange(140)/10
 f=pd.DataFrame(dict(open=close-.1,high=close+1,low=close-1,close=close,volume=100+np.arange(140),source='proxy'),index=pd.bdate_range('2026-04-01',periods=140))
 f.attrs={**META,'timeframe':'1D','data_quality':{'state':'READY'},'evidence':[{'range':[str(i),str(i+1)]} for i in range(800)]}
 return f

class NumericMetadata(unittest.TestCase):
 def test_indexed_evidence_exact_reference_with_overlap_and_revocation(self):
  import random
  rng=random.Random(28);start=pd.Timestamp('2026-10-27 09:00')
  def spans(n):
   result=[]
   for _ in range(n):
    left=rng.randrange(0,300);right=left+rng.randrange(1,30)
    result.append([str(start+pd.Timedelta(minutes=left)),str(start+pd.Timedelta(minutes=right))])
   return result
  meta=dict(META,absence_evidence=dict(identity=META,basis='complete_published_trade_files',reference='test',checked_at=NOW.isoformat(),no_trades=spans(150),trade_intervals=spans(8)))
  for i in range(310):
   left=start+pd.Timedelta(minutes=i);right=left+pd.Timedelta(minutes=5)
   self.assertEqual(q.no_trades_proof(meta,left,right,NOW),oldq.no_trades_proof(meta,left,right,NOW))
  for change in ('currency','checked_at','reference'):
   bad=copy.deepcopy(meta)
   if change=='currency':bad['absence_evidence']['identity']['currency']='USD'
   elif change=='checked_at':bad['absence_evidence']['checked_at']=(NOW+pd.Timedelta(days=1)).isoformat()
   else:bad['absence_evidence']['reference']=''
   self.assertIsNone(q.no_trades_proof(bad,start,start+pd.Timedelta(minutes=5),NOW))
 def test_indicators_exact_values_and_independent_metadata(self):
  f=sample();saved=copy.deepcopy(f.attrs);expected=olda.indicators(f);out=a.indicators(f)
  pd.testing.assert_frame_equal(out,expected);self.assertEqual(out.attrs,expected.attrs)
  out.attrs['evidence'][0]['range'][0]='changed';out.iloc[0,0]=1
  self.assertEqual(f.attrs,saved);self.assertNotEqual(f.iloc[0,0],1)
 def test_features_core_facts_and_protective_blockers_equal_reference(self):
  for state in ('READY','INVALID','GAP','UNCONFIRMED','STALE','SOURCE_UNAVAILABLE'):
   for short in (False,True):
    f=sample();f.attrs['data_quality']={'state':state}
    if short:f=f.iloc[:20].copy()
    with self.subTest(state=state,short=short):
     oldf,oldr=olda._safe_features(f,{},NOW,prove_scope=True);newf,newr=a._safe_features(f,{},NOW,prove_scope=True)
     pd.testing.assert_frame_equal(newf,oldf);self.assertEqual(newf.attrs,oldf.attrs);self.assertEqual(newr,oldr)
     if state!='READY' or short:self.assertFalse(newr.get('sufficient_history'))
 def test_real_saved_chart_analysis_and_revision_guard_unchanged(self):
  import pickle
  from performance_cache import ReadCache,InputRevisionChanged
  import sqlite3,tempfile
  f=pickle.loads((BEFORE.parent/'before-result.pickle').read_bytes())['frame']
  before,old=olda._safe_features(f,{},NOW);after,new=a._safe_features(f,{},NOW)
  pd.testing.assert_frame_equal(after,before);self.assertEqual(after.attrs,before.attrs);self.assertEqual(new,old)
  with tempfile.TemporaryDirectory(prefix='chart-fast28-') as folder:
   path=Path(folder)/'test.sqlite3'
   with closing(sqlite3.connect(path)) as db,db:db.execute('create table input_revisions(scope text primary key,revision int)');db.execute("insert into input_revisions values('market:test:15m',1)")
   cache=ReadCache(path)
   try:
    token=cache.token_for(['market:test:15m'])
    with closing(sqlite3.connect(path)) as db,db:db.execute('update input_revisions set revision=2')
    with self.assertRaises(InputRevisionChanged):cache.assert_current(token)
   finally:cache.close()

if __name__=='__main__':unittest.main()
