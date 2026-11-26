"""New bounded publication recovery regressions; memory only, no live writes."""
import gzip,json,sqlite3,unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.error import HTTPError
from test_gettex_live_evidence import MemoryPath,MemoryStore,LiveEvidence,pd,gh

class PublicationRecheck(unittest.TestCase):
 def setUp(self):
  self.f=LiveEvidence();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.s=self.f.s;self.info=self.f.info;self.now=self.f.now
  mkdir=patch.object(MemoryPath,'mkdir',lambda *a,**k:None,create=True);mkdir.start();self.addCleanup(mkdir.stop)
  def setting(k,v):
   with self.s.connect() as db:db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(k,json.dumps(v)))
  self.s.set_setting=setting
  self.s.db.execute('CREATE TABLE series(isin TEXT,source TEXT,venue TEXT,currency TEXT,symbol TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL)')
  self.bad=self.f.file('2026-11-03T10:15Z','mund')
  self.old=gzip.compress((self.info['isin']+',10:01:00,EUR,250,2\n').encode())
  self.bad.write_bytes(self.old)
  meta=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='AAPL.GTX',timezone='Europe/Berlin')
  self.meta=meta
  self.s.set_setting('gettex_chart:'+self.info['isin']+':5m',dict(state='SAVED',identity=meta,listing_proof=dict(meta,isin=self.info['isin'],status='CONFIRMED')))
  self.s.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(self.info['isin'],'gettex_chart','GETTEX','EUR','AAPL.GTX','5m','2026-11-03 11:10:00',251,251,251,251,3))
  self.s.db.commit()
  self.url='https://erdk.bayerische-boerse.de:8000/delayed-data/MUNC-MUND/posttrade/'+self.bad.name
  self.calls=[];self.response=self.old
 def http(self,url):
  self.calls.append(url)
  if url==gh.PAGE:return ('<a href="'+self.url+'">file</a>').encode()
  if isinstance(self.response,Exception):raise self.response
  return self.response
 def classify(self):return gh.classify_cached_session(self.info,self.s,self.f.parse,self.now,current_only=True)
 def acquire(self,now=None):return gh.acquire_missing_evidence(self.info,self.s,self.http,now or self.now)
 def test_confirmed_late_reference_prevents_false_no_trades(self):
  r=self.classify()
  self.assertEqual(r['state'],'PARTIAL')
  self.assertIn('SOURCE_COVERAGE_CONFLICT',r['unconfirmed_files'][self.bad.name])
  self.assertIsNone(self.f.proof('2026-11-03 11:10','2026-11-03 11:15'))
  self.assertIsNotNone(self.f.proof('2026-11-03 10:45','2026-11-03 10:50'))
 def test_current_session_conflict_rechecks_actual_published_url(self):
  self.classify();self.response=gzip.compress(gzip.decompress(self.old)+(self.info['isin']+',10:11:00,EUR,251,3\n').encode())
  r=self.acquire()
  self.assertEqual(r['replaced_invalid_files'],1)
  self.assertEqual(self.calls,[gh.PAGE,self.url])
  self.assertEqual(self.bad.read_bytes(),self.response)
  self.assertEqual(self.classify()['state'],'DONE')
 def test_existing_history_turn_discovers_and_checks_without_next_rotation(self):
  result=gh.maintain_evidence(self.info,self.s,self.http,self.f.parse,self.now)
  self.assertEqual(result['classification']['state'],'PARTIAL')
  self.assertEqual(result['acquisition']['state'],'UNCHANGED_UNCONFIRMED')
  self.assertEqual(self.calls,[gh.PAGE,self.url])
 def test_short_file_alone_does_not_invent_missing_trades(self):
  self.s.db.execute('DELETE FROM series');self.s.db.commit()
  self.assertEqual(self.classify()['state'],'DONE')
 def test_wrong_reference_identity_or_currency_cannot_reject_file(self):
  self.s.db.execute("UPDATE series SET currency='USD'");self.s.db.commit()
  self.assertEqual(self.classify()['state'],'DONE')
 def test_unchanged_response_keeps_conflict_and_backoff_on_restart(self):
  self.classify();r=self.acquire()
  self.assertEqual(r['unchanged_files'],1)
  self.assertEqual(pd.Timestamp(r['retry_after']),self.now+pd.Timedelta(days=1))
  self.assertEqual(self.bad.read_bytes(),self.old)
  self.assertEqual(self.classify()['state'],'IDLE')
  self.assertEqual(self.acquire(self.now+pd.Timedelta(minutes=16))['state'],'IDLE')
  self.assertEqual(len(self.calls),2)
 def test_non_monotonic_replacement_is_retained_for_review(self):
  self.classify();self.response=gzip.compress((self.info['isin']+',10:01:00,EUR,249,2\n').encode())
  r=self.acquire();self.assertEqual(r['state'],'UNCONFIRMED')
  self.assertIn('REQUIRES_TRADE_CORRECTION_ID',r['reason'])
  self.assertEqual(self.bad.read_bytes(),self.old)
 def test_retained_unrelated_bad_row_does_not_reject_identical_publication(self):
  # Real 10Nov MUND contains an unrelated negative-price zero-volume row.
  self.old=gzip.compress(gzip.decompress(self.old)+b'DE000GTX2RK1,10:01:00.001132,EUR,-0.07974,0\n')
  self.bad.write_bytes(self.old);self.response=self.old;self.classify()
  r=self.acquire()
  self.assertEqual(r['state'],'UNCHANGED_UNCONFIRMED')
  self.assertEqual(self.bad.read_bytes(),self.old)
  self.assertIsNone(self.f.proof('2026-11-03 11:10','2026-11-03 11:15'))
 def test_replacement_validates_additions_without_recertifying_retained_rows(self):
  self.old=gzip.compress(gzip.decompress(self.old)+b'DE000GTX2RK1,10:01:00.001132,EUR,-0.07974,0\n')
  self.bad.write_bytes(self.old);self.classify()
  self.response=gzip.compress(gzip.decompress(self.old)+(self.info['isin']+',10:11:00,EUR,251,3\n').encode())
  self.assertEqual(self.acquire()['state'],'FILES_ACQUIRED')
  self.assertEqual(gzip.decompress(self.bad.read_bytes()),gzip.decompress(self.response))
  for extra in (b'DE000GTX2RK1,10:11:00,EUR,-0.07974,0\n',b'DE000GTX2RK1,10:01:00.001132,EUR,-0.07974,0\n'):
   with self.assertRaisesRegex(ValueError,'INVALID_REPLACEMENT'):
    gh._replacement_lines(gzip.decompress(self.response)+extra,self.bad.name,retained=gzip.decompress(self.old).decode().splitlines())
 def test_fixed_validator_retries_once_after_source_cooldown(self):
  self.classify()
  key='candlepilot_acquisition:20261103'
  self.s.set_setting(key,dict(state='UNCONFIRMED',attempts=1,reason='ValueError: INVALID_REPLACEMENT_TIME_OR_VALUE: '+self.bad.name,retry_after=(self.now+pd.Timedelta(days=1)).isoformat()))
  self.s.set_setting('candlepilot_source_cooldown',(self.now+pd.Timedelta(minutes=15)).isoformat())
  self.assertEqual(self.acquire()['state'],'COOLDOWN');self.assertEqual(self.calls,[])
  r=gh.acquire_missing_evidence(self.info,self.s,self.http,self.now+pd.Timedelta(minutes=16),budget=1)
  self.assertEqual(r['state'],'UNCHANGED_UNCONFIRMED');self.assertEqual(r['attempts'],2)
  self.assertEqual(r['validation_policy'],2)
  self.assertEqual(self.acquire(self.now+pd.Timedelta(minutes=32))['state'],'IDLE')
  self.assertEqual(len(self.calls),2)
 def test_429_obeys_shared_retry_after(self):
  self.classify();self.response=HTTPError(self.url,429,'limit',{'Retry-After':'172800'},None)
  r=self.acquire();self.assertEqual(pd.Timestamp(r['retry_after']),self.now+pd.Timedelta(days=2))
  self.assertEqual(self.acquire(self.now+pd.Timedelta(hours=25))['state'],'COOLDOWN')
  self.assertEqual(len(self.calls),2)
 def test_recovery_does_not_invent_root_url_for_cached_file(self):
  # Requested archive disappeared from index: reuse only the observed origin/path.
  other='https://erdk.bayerische-boerse.de:8000/delayed-data/MUNC-MUND/posttrade/posttrade.20261103.10.30.mund.csv.gz'
  calls=[]
  def http(url):
   calls.append(url)
   if url==gh.PAGE:return ('<a href="'+other+'">file</a>').encode()
   return self.old
  with patch('delivery_observations.observe'),patch('delivery_observations.outcome'):
   gh.download(self.info['isin'],http,self.f.parse,self.s,pd.Timestamp('2026-11-03T10:00Z'),pd.Timestamp('2026-11-03T10:15Z'))
  self.assertTrue(all(u==gh.PAGE or ':8000/delayed-data/MUNC-MUND/posttrade/' in u for u in calls))
 def test_cached_published_file_is_parsed_once(self):
  count=[]
  def parse(*a,**k):
   if k.get('file_time')==pd.Timestamp('2026-11-03T10:15Z'):count.append(1)
   return self.f.parse(*a,**k)
  # Leave only one candidate so that the assertion is a file, not an ISIN count.
  fs=self.s.directory.fs
  for name in list(fs):
   if name!=self.bad.namepath:del fs[name]
  with patch('delivery_observations.observe'),patch('delivery_observations.outcome'):
   gh.download(self.info['isin'],self.http,parse,self.s,pd.Timestamp('2026-11-03T10:00Z'),pd.Timestamp('2026-11-03T10:15Z'))
  self.assertEqual(count,[1])
 def test_changed_reference_during_publication_rejects_stale_proof(self):
  original=gh._cached_session_summary
  def race(*a,**k):
   result=original(*a,**k)
   self.s.db.execute('UPDATE series SET volume=5');self.s.db.commit()
   return result
  with patch.object(gh,'_cached_session_summary',side_effect=race):
   with self.assertRaisesRegex(ValueError,'reference changed'):self.classify()
  self.assertIsNone(self.s.setting('interval_evidence:'+self.info['isin']))
 def test_legacy_current_done_is_reviewed_once_without_old_archive_audit(self):
  self.s.db.execute('DELETE FROM series');self.s.db.commit();self.classify()
  key='history_evidence_progress:'+self.info['isin'];p=self.s.setting(key);p['20261103'].pop('coverage_review_version')
  self.s.set_setting(key,p)
  self.assertEqual(self.classify()['coverage_review_version'],1)
  with patch.object(gh,'_cached_session_summary',side_effect=AssertionError('duplicate')):
   self.assertEqual(self.classify()['state'],'IDLE')
 def test_53_and_200_queue_turns_share_one_retry(self):
  self.classify();self.acquire()
  for size in (53,200):
   for index in range(size):
    result=self.acquire(self.now+pd.Timedelta(seconds=index))
    self.assertEqual(result['state'],'COOLDOWN')
  self.assertEqual(len(self.calls),2)
 def test_malformed_replacement_keeps_original_and_no_ready(self):
  self.classify();self.response=gzip.compress(b'<html>server failed</html>')
  r=self.acquire();self.assertEqual(r['state'],'UNCONFIRMED');self.assertEqual(self.bad.read_bytes(),self.old)
  self.assertIsNone(self.f.proof('2026-11-03 11:10','2026-11-03 11:15'))
 def test_invalid_or_outside_slice_additions_cannot_poison_original(self):
  for row in ('10:11:00,EUR,nan,3','10:11:00,EUR,251,-1','10:16:00,EUR,251,3'):
   with self.subTest(row=row):
    content=gzip.decompress(self.old)+(self.info['isin']+','+row+'\n').encode()
    with self.assertRaisesRegex(ValueError,'INVALID_REPLACEMENT'):
     gh._replacement_lines(content,self.bad.name)
 def test_corrected_file_is_consumed_by_existing_download(self):
  self.classify();self.response=gzip.compress(gzip.decompress(self.old)+(self.info['isin']+',10:11:00,EUR,251,3\n').encode())
  self.acquire()
  self.s.set_setting('cp_source_retry:gettex',None)
  with patch('delivery_observations.observe'),patch('delivery_observations.outcome'):
   frame=gh.download(self.info['isin'],self.http,self.f.parse,self.s,pd.Timestamp('2026-11-03T10:00Z'),pd.Timestamp('2026-11-03T10:15Z'))
  self.assertEqual(list(frame.price),[250,251]);self.assertEqual(list(frame.volume),[2,3])
 def test_existing_15m_row_is_not_ready_when_publication_is_incomplete(self):
  import data_quality as dq
  self.classify()
  frame=pd.DataFrame([[250,250,250,250,2]],index=pd.DatetimeIndex(['2026-11-03 11:00:00']),columns=dq.FIELDS)
  proof=self.s.setting('interval_evidence:'+self.info['isin']);frame.attrs=dict(proof['identity'],absence_evidence=proof,classify_absence=True)
  r=dq.audit(frame,'15m',now=self.now,check_stale=False)
  self.assertEqual(r['state'],'UNCONFIRMED');self.assertEqual(r['missing_count'],0)
  self.assertEqual(r['source_incomplete_count'],1);self.assertFalse(r['repairable'])
  self.assertEqual(frame.iloc[0].tolist(),[250,250,250,250,2])
 def test_publication_scope_does_not_block_independent_source_or_window(self):
  import data_quality as dq
  self.classify();proof=self.s.setting('interval_evidence:'+self.info['isin'])
  frame=pd.DataFrame([[250,250,250,250,2]],index=pd.DatetimeIndex(['2026-11-03 11:00:00']),columns=dq.FIELDS)
  frame.attrs=dict(self.meta,absence_evidence=proof,classify_absence=True)
  self.assertEqual(dq.audit(frame,'15m',now=self.now,check_stale=False)['state'],'READY')
  frame.attrs=dict(proof['identity'],absence_evidence=proof,classify_absence=True)
  proof['no_trades']=[];frame.index=pd.DatetimeIndex(['2026-11-03 10:45:00'])
  self.assertEqual(dq.audit(frame,'15m',now=self.now,check_stale=False)['state'],'READY')
 def test_new_publication_evidence_only_invalidates_intersecting_review(self):
  import cp_reconciliation as rv
  job=rv._spec(self.info['isin'],self.meta,'15m','2026-11-03 11:00:00','2026-11-03 11:00:00')
  with patch('eur_listings.stored_identity',return_value=self.meta):
   before=rv._input(self.s.db,job)['fingerprint']
   key='interval_evidence:'+self.info['isin']
   for values in ([],[['2026-11-03 10:00:00','2026-11-03 10:15:00']]):
    self.s.set_setting(key,{'incomplete_publication_intervals':values})
    self.assertEqual(rv._input(self.s.db,job)['fingerprint'],before)
   self.s.set_setting(key,{'incomplete_publication_intervals':[['2026-11-03 11:00:00','2026-11-03 11:15:00']]})
   self.assertNotEqual(rv._input(self.s.db,job)['fingerprint'],before)

if __name__=='__main__':unittest.main()
