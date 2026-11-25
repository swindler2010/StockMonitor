"""Current-session evidence only. In-memory SQLite/files; no network or user DB."""
import fnmatch,gzip,json,sqlite3,sys,unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import gettex_history as gh
import data_quality as dq
from app import StockMonitor

class MemoryPath:
 def __init__(self,fs,name='root'):self.fs,self.namepath=fs,name
 @property
 def name(self):return self.namepath.rsplit('/',1)[-1]
 def __truediv__(self,name):return MemoryPath(self.fs,self.namepath+'/'+name)
 def glob(self,pattern):return [MemoryPath(self.fs,n) for n in list(self.fs) if n.startswith(self.namepath+'/') and fnmatch.fnmatch(n.rsplit('/',1)[-1],pattern)]
 def exists(self):return self.namepath in self.fs
 def stat(self):
  data,rev=self.fs[self.namepath];return SimpleNamespace(st_size=len(data),st_mtime_ns=rev)
 def read_bytes(self):return self.fs[self.namepath][0]
 def read_text(self,encoding='utf-8'):return self.read_bytes().decode(encoding)
 def write_bytes(self,data):self.fs[self.namepath]=(data,self.fs.get(self.namepath,(b'',0))[1]+1)
 def write_text(self,data,encoding='utf-8'):self.write_bytes(data.encode(encoding))
 def with_suffix(self,suffix):return MemoryPath(self.fs,self.namepath.rsplit('.',1)[0]+suffix)
 def replace(self,target):self.fs[target.namepath]=self.fs.pop(self.namepath)
 def unlink(self):del self.fs[self.namepath]

class MemoryStore:
 def __init__(self):
  self.directory=MemoryPath({});self.db=sqlite3.connect(':memory:')
  self.db.executescript('CREATE TABLE instruments(isin TEXT,payload TEXT);CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE candles(isin TEXT,source TEXT);')
 @contextmanager
 def connect(self):
  try:yield self.db;self.db.commit()
  except Exception:self.db.rollback();raise
 def setting(self,key,default=None):
  row=self.db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone();return json.loads(row[0]) if row else default
 def instruments(self):return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM instruments')]

class LiveEvidence(unittest.TestCase):
 def setUp(self):
  self.s=MemoryStore();self.addCleanup(self.s.db.close)
  self.info=dict(isin='US0378331005',venue='GETTEX',currency='EUR',name='Apple')
  self.s.db.execute('INSERT INTO instruments VALUES (?,?)',(self.info['isin'],json.dumps(self.info)))
  self.s.db.execute('INSERT INTO candles VALUES (?,?)',(self.info['isin'],'gettex'));self.s.db.commit()
  self.now=pd.Timestamp('2026-11-03T10:30Z')
  self.parse=lambda *a,**k:StockMonitor._parse_gettex_csv(None,*a,**k)
  self.files('2026-11-03',self.now-pd.Timedelta(minutes=15))
 def files(self,day,last):
  from timeframes import schedule
  meta=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=self.info['isin'],timezone='Europe/Berlin')
  session=schedule(meta,pd.Timestamp(day),pd.Timestamp(day)).iloc[0]
  for at in pd.date_range(session.open+pd.Timedelta(minutes=15),min(pd.Timestamp(last),session.close),freq='15min'):
   for mic in ('munc','mund'):
    f=self.file(at,mic)
    if not f.exists():f.write_bytes(gzip.compress(('DE0007236101,'+(at-pd.Timedelta(seconds=1)).strftime('%H:%M:%S')+',EUR,10,1\n').encode()))
 def file(self,at,mic='munc'):return self.s.directory/'gettex_files'/f"posttrade.{pd.Timestamp(at).strftime('%Y%m%d.%H.%M')}.{mic}.csv.gz"
 def classify(self,now=None):return gh.classify_cached_session(self.info,self.s,self.parse,now or self.now)
 def proof(self,start,end,now=None):
  evidence=self.s.setting('interval_evidence:'+self.info['isin'],{})
  return dq.no_trades_proof(dict(evidence.get('identity',{}),absence_evidence=evidence),start,end,now or self.now)
 def test_current_session_closed_pair_is_used_after_publication_grace(self):
  result=self.classify();self.assertEqual(result['state'],'DONE')
  self.assertIsNotNone(self.proof('2026-11-03 11:00','2026-11-03 11:15'))
  self.assertIsNone(self.proof('2026-11-03 11:15','2026-11-03 11:20'))
 def test_no_early_publication_and_summary_prefix_changes_without_file_changes(self):
  early=self.now-pd.Timedelta(seconds=1);self.classify(early)
  self.assertIsNone(self.proof('2026-11-03 11:00','2026-11-03 11:05',early))
  self.classify();self.assertIsNotNone(self.proof('2026-11-03 11:00','2026-11-03 11:15'))
 def test_unchanged_restart_skips_and_does_not_mutate_candles(self):
  self.classify();before=self.s.db.execute('SELECT * FROM candles').fetchall()
  with patch.object(gh,'_cached_session_summary',side_effect=AssertionError('repeat')):
   self.assertEqual(self.classify(self.now+pd.Timedelta(minutes=1))['state'],'IDLE')
  self.assertEqual(before,self.s.db.execute('SELECT * FROM candles').fetchall())
 def test_missing_empty_and_malformed_pair_stay_unconfirmed(self):
  file=self.file('2026-11-03T10:15Z')
  for content in (None,b'',b'bad-format\n'):
   if content is None:file.unlink()
   else:file.write_bytes(gzip.compress(content))
   result=self.classify();self.assertEqual(result['state'],'PARTIAL')
   self.assertIsNone(self.proof('2026-11-03 11:00','2026-11-03 11:15'))
   self.assertIsNotNone(self.proof('2026-11-03 10:45','2026-11-03 11:00'))
 def test_zero_quantity_and_real_trade_never_prove_absence(self):
  file=self.file('2026-11-03T10:15Z')
  for quantity in (0,3):
   file.write_bytes(gzip.compress(f"{self.info['isin']},10:02:00,EUR,250,{quantity}\n".encode()))
   self.classify();self.assertIsNone(self.proof('2026-11-03 11:00','2026-11-03 11:05'))
   evidence=self.s.setting('interval_evidence:'+self.info['isin'])
   meta=dict(evidence['identity'],absence_evidence=evidence)
   self.assertEqual(dq.known_trade(meta,'2026-11-03 11:00','2026-11-03 11:05',self.now),quantity>0)
 def test_changed_invalid_source_withdraws_old_current_day_proof(self):
  self.classify();self.file('2026-11-03T10:15Z').write_bytes(gzip.compress(f"{self.info['isin']},10:02:00,USD,250,2\n".encode()))
  self.assertEqual(self.classify()['state'],'UNCONFIRMED')
  self.assertIsNone(self.proof('2026-11-03 11:00','2026-11-03 11:15'))
 def test_calendar_dst_session_open(self):
  for day,opening in (('2026-10-21','05:30'),('2026-11-03','06:30')):
   clock=pd.Timestamp(day+'T'+opening+'Z');self.files(day,clock+pd.Timedelta(minutes=30))
   # A complete pair exists but has not yet passed its publication grace.
   with patch.object(self.s.directory.__class__,'glob',return_value=[p for p in (self.s.directory/'gettex_files').glob('posttrade.*') if day.replace('-','') in p.name]):
    self.classify(clock+pd.Timedelta(minutes=29,seconds=59))
    self.assertIsNone(self.proof(day+' 07:30',day+' 07:35',clock+pd.Timedelta(minutes=29,seconds=59)))
    self.classify(clock+pd.Timedelta(minutes=30))
    self.assertIsNotNone(self.proof(day+' 07:30',day+' 07:45',clock+pd.Timedelta(minutes=30)))
    self.assertIsNone(self.proof(day+' 07:25',day+' 07:30',clock+pd.Timedelta(minutes=30)))
 def test_session_close_and_next_day_do_not_repeat_completed_work(self):
  self.files('2026-11-03',pd.Timestamp('2026-11-03T22:00Z'))
  early=pd.Timestamp('2026-11-03T22:14:59Z');self.classify(early)
  self.assertIsNone(self.proof('2026-11-03 22:45','2026-11-03 23:00',early))
  closed=pd.Timestamp('2026-11-03T22:15Z');result=self.classify(closed)
  self.assertIsNotNone(self.proof('2026-11-03 22:45','2026-11-03 23:00',closed))
  self.assertIsNone(self.proof('2026-11-03 23:00','2026-11-03 23:05',closed))
  with patch.object(gh,'_cached_session_summary',side_effect=AssertionError('repeat completed session')):
   self.assertEqual(self.classify(pd.Timestamp('2026-11-04T07:00Z'))['state'],'IDLE')
 def test_weekend_and_future_files_do_not_certify_trading(self):
  self.file('2026-11-07T10:15Z').write_bytes(gzip.compress(b'DE0007236101,10:14:59,EUR,10,1\n'))
  self.classify();self.assertIsNone(self.proof('2026-11-07 11:00','2026-11-07 11:15'))
  result=self.classify(pd.Timestamp('2026-11-07T10:30Z'))
  self.assertEqual(result['state'],'MARKET_CLOSED')
  self.assertIsNone(self.proof('2026-11-07 11:00','2026-11-07 11:15',pd.Timestamp('2026-11-07T10:30Z')))
 def test_live_work_does_not_starve_pending_history(self):
  self.files('2026-11-02',pd.Timestamp('2026-11-02T22:00Z'))
  self.assertEqual(self.classify()['date'],'20261103')
  later=self.now+pd.Timedelta(minutes=15);self.files('2026-11-03',later-pd.Timedelta(minutes=15))
  self.assertEqual(self.classify(later)['date'],'20261102')
  self.assertEqual(self.classify(later)['date'],'20261103')
 def test_identity_and_source_revision_races_reject_publication(self):
  original=gh._cached_session_summary
  def changed(*a,**k):
   result=original(*a,**k);self.file('2026-11-03T10:15Z').write_bytes(gzip.compress(b'changed'))
   return result
  with patch.object(gh,'_cached_session_summary',side_effect=changed):
   with self.assertRaisesRegex(ValueError,'files changed'):self.classify()
  self.assertIsNone(self.s.setting('interval_evidence:'+self.info['isin']))
  self.s.db.execute('UPDATE instruments SET payload=?',(json.dumps(dict(self.info,currency='USD')),));self.s.db.commit()
  with self.assertRaisesRegex(ValueError,'instrument changed'):self.classify()

 def test_current_only_poll_never_runs_old_session_work(self):
  self.files('2026-11-02',pd.Timestamp('2026-11-02T22:00Z'))
  self.classify()
  with patch.object(gh,'_cached_session_summary',side_effect=AssertionError('historical work in fresh poll')):
   result=gh.classify_cached_session(self.info,self.s,self.parse,self.now,current_only=True)
  self.assertEqual(result['state'],'IDLE')

class PollEvidence(unittest.TestCase):
 def feed(self,source='gettex',closing=False,error=None):
  from desktop014 import Desktop014
  from contextlib import ExitStack,nullcontext
  from unittest.mock import Mock
  import queue
  events=[];store=SimpleNamespace(setting=lambda k,d=None:d,set_setting=Mock(),event=Mock(),load_official=lambda *a:pd.DataFrame())
  ctx=SimpleNamespace(store=store,closing=closing,feed_slots=nullcontext(),events=queue.Queue(),feed_done=lambda *a:None,
   _download_gettex_trades=lambda *a:events.append('official') or pd.DataFrame(),_recover_gettex_range=Mock(),_parse_gettex_csv=Mock())
  with ExitStack() as stack:
   classify=stack.enter_context(patch('gettex_history.classify_cached_session',side_effect=error or (lambda *a,**k:events.append('evidence'))))
   for name in ('gettex_history.acquire_missing_evidence','history_service.recover','history_service.inspect_saved_archive','revision_watcher.check'):
    stack.enter_context(patch(name,side_effect=AssertionError('unrelated history request')))
   stack.enter_context(patch('gettex_chart.turn',return_value={'state':'UNCHANGED'}))
   stack.enter_context(patch('candle_schedule.fresh_finished'))
   Desktop014._run_feed(ctx,dict(isin='US0378331005',_gettex_poll_only=True,_cp_focus=dict(isin='US0378331005',source=source,timeframe='15m')))
  return classify,events,ctx
 def test_official_poll_classifies_only_current_files_without_history_requests(self):
  call,events,ctx=self.feed();self.assertEqual(events,['official','evidence'])
  call.assert_called_once();self.assertIs(call.call_args.kwargs['current_only'],True)
 def test_chart_poll_does_not_cross_providers(self):
  call,events,ctx=self.feed('gettex_chart');call.assert_not_called();self.assertEqual(events,[])
 def test_classification_failure_remains_visible_and_feed_finishes(self):
  call,events,ctx=self.feed(error=ValueError('evidence changed concurrently'))
  self.assertIn('GETTEX evidence: evidence changed concurrently',ctx.events.get()[1][1])
 def test_closing_does_not_start_evidence_work(self):
  call,events,ctx=self.feed(closing=True);call.assert_not_called()

if __name__=='__main__':unittest.main()
