import gzip,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_universal23 import Universal,ClosedStore,StockMonitor,gh,dq,pd

class PartialEvidence(unittest.TestCase):
 def setUp(self):
  self.fixture=Universal();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
  self.addCleanup(self.fixture.doCleanups)
  self.s=self.fixture.s;self.info=self.fixture.info;self.now=self.fixture.now
  self.parse=self.fixture.parse;self.paths=self.fixture.files()
  self.bad=next(p for p in self.paths if '.06.00.munc.' in p.name)
 def classify(self):return gh.classify_cached_session(self.info,self.s,self.parse,self.now)
 def proof(self):return self.s.setting('interval_evidence:'+self.info['isin'],{})
 def absent(self,stamp):
  meta=dict(self.fixture.frame().attrs,absence_evidence=self.proof())
  return dq.no_trades_proof(meta,pd.Timestamp(stamp),pd.Timestamp(stamp)+pd.Timedelta(minutes=5),self.now)
 def test_empty_slice_preserves_independent_valid_intervals(self):
  self.bad.write_bytes(gzip.compress(b''));r=self.classify()
  self.assertEqual(r['state'],'PARTIAL');self.assertEqual(r['unconfirmed_intervals'],3)
  self.assertIsNone(self.absent('2026-10-21 07:50'))
  self.assertIsNotNone(self.absent('2026-10-21 08:05'))
  self.assertIn(self.bad.name,r['unconfirmed_files'])
 def test_missing_or_malformed_file_is_not_no_trades(self):
  for content in (None,b'bad-format\n'):
   if content is None:self.bad.unlink()
   else:self.bad.write_bytes(gzip.compress(content))
   r=self.classify();self.assertEqual(r['state'],'PARTIAL')
   self.assertIsNone(self.absent('2026-10-21 07:50'))
   self.assertIsNotNone(self.absent('2026-10-21 08:05'))
 def test_corrected_file_releases_only_its_intervals_and_resume_skips(self):
  original=self.bad.read_bytes();self.bad.write_bytes(gzip.compress(b''));self.classify()
  reopened=ClosedStore(self.s.directory)
  with patch.object(StockMonitor,'_parse_gettex_csv',side_effect=AssertionError('duplicate work')):
   self.assertEqual(gh.classify_cached_session(self.info,reopened,self.parse,self.now)['state'],'IDLE')
  self.bad.write_bytes(original);self.assertEqual(self.classify()['state'],'DONE')
  self.assertIsNotNone(self.absent('2026-10-21 07:50'))
 def test_corrupted_previously_valid_slice_withdraws_only_that_proof(self):
  self.classify();self.assertIsNotNone(self.absent('2026-10-21 07:50'))
  self.bad.write_bytes(gzip.compress(b''));self.classify()
  self.assertIsNone(self.absent('2026-10-21 07:50'))
  self.assertIsNotNone(self.absent('2026-10-21 08:05'))
 def test_old_unconfirmed_checkpoint_reprocessed_once_no_ohlcv_mutation(self):
  self.bad.write_bytes(gzip.compress(b''));self.classify()
  key='history_evidence_progress:'+self.info['isin'];progress=self.s.setting(key)
  progress['20261021']['state']='UNCONFIRMED';progress['20261021'].pop('evidence_policy',None)
  self.s.set_setting(key,progress)
  frame=self.fixture.frame();self.s.save_frame(self.info['isin'],'gettex','5m',frame)
  before=self.s.load_official(self.info['isin'],'5m').copy()
  self.assertEqual(self.classify()['state'],'PARTIAL')
  self.assertEqual(self.classify()['state'],'IDLE')
  pd.testing.assert_frame_equal(self.s.load_official(self.info['isin'],'5m'),before)
 def test_invalid_prices_unknown_source_and_revision_races_stay_blocked(self):
  self.fixture.files(bad=True);self.assertEqual(self.classify()['state'],'UNCONFIRMED')
  self.assertFalse(self.proof())
  self.fixture.files();self.bad.write_bytes(gzip.compress(b''));self.classify()
  frame=self.fixture.frame();frame.attrs.update(source='gettex_chart',symbol='FAKE.GTX')
  dq.attach_interval_evidence(self.s,frame,self.info['isin']);self.assertNotIn('absence_evidence',frame.attrs)
  self.bad.write_bytes(gzip.compress(b'bad-format\n'))
  self.s.save_instrument(dict(self.info,currency='USD'))
  with self.assertRaisesRegex(ValueError,'instrument changed'):self.classify()
 def test_queue_focus_is_not_an_instrument_change_but_real_edits_block(self):
  runtime=dict(self.info,_cp_focus={'isin':self.info['isin'],'timeframe':'15m','source':'gettex_chart'})
  self.bad.write_bytes(gzip.compress(b''))
  self.assertEqual(gh.classify_cached_session(runtime,self.s,self.parse,self.now)['state'],'PARTIAL')
  proof=self.proof()
  self.bad.write_bytes(gzip.compress(b'bad-format\n'))
  self.s.save_instrument(dict(self.info,currency='USD'))
  with self.assertRaisesRegex(ValueError,'instrument changed'):
   gh.classify_cached_session(runtime,self.s,self.parse,self.now)
  self.assertEqual(self.proof(),proof)
  with self.s.connect() as db:db.execute('DELETE FROM instruments WHERE isin=?',(self.info['isin'],))
  with self.assertRaisesRegex(ValueError,'instrument changed'):
   gh.classify_cached_session(runtime,self.s,self.parse,self.now)
  self.assertEqual(self.proof(),proof)
 def acquisition_setup(self):
  self.bad.write_bytes(gzip.compress(b''));self.classify()
  self.s.set_setting('data_quality:'+self.info['isin']+':'+self.info['isin']+':5m',
    dict(identity=self.fixture.frame().attrs,state='UNCONFIRMED',ranges=[['2026-10-21 07:50','2026-10-21 07:50']]))
  calls=[]
  def http(url):
   calls.append(url)
   if url==gh.PAGE:return b'<a href="https://erdk.bayerische-boerse.de/posttrade.20261022.21.00.munc.csv.gz">file</a>'
   return self.response
  return calls,http
 def test_existing_rejected_file_is_retried_with_backup_no_candle_write(self):
  calls,http=self.acquisition_setup();original=self.bad.read_bytes()
  self.response=gzip.compress(b'DE0007236101,06:00:00,EUR,10,1\n')
  out=gh.acquire_missing_evidence(self.info,self.s,http,self.now)
  self.assertEqual(out['replaced_invalid_files'],1);self.assertEqual(len(calls),2)
  self.assertEqual(self.bad.read_bytes(),self.response)
  backups=list((self.bad.parent/'rejected_files').iterdir());self.assertEqual(backups[0].read_bytes(),original)
  with self.s.connect() as db:self.assertEqual(db.execute('select count(*) from candles').fetchone()[0],0)
  self.assertEqual(self.classify()['state'],'DONE')
 def test_empty_replacement_keeps_evidence_and_obeys_cooldown(self):
  calls,http=self.acquisition_setup();self.response=self.bad.read_bytes();original=self.response
  out=gh.acquire_missing_evidence(self.info,self.s,http,self.now)
  self.assertEqual(out['state'],'UNCONFIRMED');self.assertIn('UNCONFIRMED_REPLACEMENT_FILE',out['reason'])
  self.assertEqual(self.bad.read_bytes(),original);self.assertIsNone(self.absent('2026-10-21 07:50'))
  again=gh.acquire_missing_evidence(self.info,self.s,http,self.now+pd.Timedelta(minutes=1))
  self.assertEqual(again['state'],'COOLDOWN');self.assertEqual(len(calls),2)

 def add_zero_quantity(self,positive=False):
  p=next(p for p in self.paths if '.06.15.mund.' in p.name)
  content=gzip.decompress(p.read_bytes())
  content+=(self.info['isin']+',06:04:32,EUR,10,0\n').encode()
  if positive:content+=(self.info['isin']+',06:03:00,EUR,10,2\n').encode()
  p.write_bytes(gzip.compress(content))
 def test_zero_quantity_is_observation_not_proven_execution(self):
  self.add_zero_quantity();r=self.classify();meta=dict(self.fixture.frame().attrs,absence_evidence=self.proof())
  self.assertFalse(dq.known_trade(meta,'2026-10-21 08:00','2026-10-21 08:05',self.now))
  self.assertIsNone(self.absent('2026-10-21 08:00'))
  self.assertIn(['2026-10-21 08:00:00','2026-10-21 08:05:00'],self.proof()['zero_quantity_intervals'])
  self.assertEqual(r['zero_quantity_only_intervals'],1)
 def test_positive_execution_in_quote_interval_still_proves_gap(self):
  self.add_zero_quantity(positive=True);self.classify();meta=dict(self.fixture.frame().attrs,absence_evidence=self.proof())
  self.assertTrue(dq.known_trade(meta,'2026-10-21 08:00','2026-10-21 08:05',self.now))
  self.assertIsNone(self.absent('2026-10-21 08:00'))
  self.assertNotIn(['2026-10-21 08:00:00','2026-10-21 08:05:00'],self.proof()['zero_quantity_intervals'])
 def test_quantity_policy_updates_old_proof_once_without_candle_write(self):
  self.add_zero_quantity();self.classify()
  key='history_evidence_progress:'+self.info['isin'];p=self.s.setting(key)
  p['20261021']['evidence_policy']=2;self.s.set_setting(key,p)
  key_proof='interval_evidence:'+self.info['isin'];proof=self.proof()
  proof['trade_intervals'].append(['2026-10-21 08:00:00','2026-10-21 08:05:00']);self.s.set_setting(key_proof,proof)
  frame=self.fixture.frame();self.s.save_frame(self.info['isin'],'gettex','5m',frame)
  before=self.s.load_official(self.info['isin'],'5m').copy()
  self.assertEqual(self.classify()['evidence_policy'],3)
  self.assertEqual(self.classify()['state'],'IDLE')
  self.assertNotIn(['2026-10-21 08:00:00','2026-10-21 08:05:00'],self.proof()['trade_intervals'])
  after=self.s.load_official(self.info['isin'],'5m');pd.testing.assert_frame_equal(after,before)

if __name__=='__main__':unittest.main()
