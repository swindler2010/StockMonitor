import sys,tempfile,unittest,json
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
import revision_watcher as rw

class Watcher(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name);self.now=pd.Timestamp('2026-10-24T12:00Z')
  self.meta=dict(source='proxy',venue='GER',currency='EUR',symbol='SAP.DE',timezone='Europe/Berlin')
  self.info={'isin':'DE0007164600','eur_listings':[{**self.meta,'isin':'DE0007164600','status':'CONFIRMED'}]}
  self.s.save_instrument(self.info)
  self.f=pd.DataFrame([[10,12,9,10,3]]*4,columns=rw.FIELDS,index=pd.date_range('2026-10-21 09:00',periods=4,freq='h'));self.f.attrs={**self.meta,'timeframe':'1H'}
  self.s.save_series(self.info,self.f,'1H');self.dq='data_quality:'+self.info['isin']+':SAP.DE:1H';self.s.set_setting(self.dq,{'state':'GAP','reason':'preserve archive gate'})
 def tearDown(self):self.s.read_cache.close();self.tmp.cleanup()
 def run_check(self,frame=None,download=None):return rw.check(self.info,self.s,download or (lambda *a,**k:(frame if frame is not None else self.f).copy()),self.now)
 def rows(self):
  with self.s.connect() as db:return db.execute('select * from series order by ts').fetchall()
 def journal(self):
  with self.s.connect() as db:return db.execute('select old_ohlcv,new_ohlcv,revision_before,revision_after from candle_revision_history').fetchall()
 def changed(self):
  f=self.f.copy();f.iloc[1,f.columns.get_loc('close')]=11;return f
 def test_changed_only_row_and_journal_survives_restart(self):
  before=self.rows();r=self.run_check(self.changed());after=self.rows();self.assertEqual(r['state'],'UPDATED');self.assertEqual(r['changed'],1)
  self.assertEqual(sum(a!=b for a,b in zip(before,after)),1);j=self.journal();self.assertEqual(len(j),1);self.assertGreater(j[0][3],j[0][2]);self.assertEqual(json.loads(j[0][0])['close'],10);self.assertEqual(json.loads(j[0][1])['close'],11)
  other=Store(self.tmp.name)
  try:
   with other.connect() as db:self.assertEqual(db.execute('select count(*) from candle_revision_history').fetchone()[0],1)
   self.assertEqual(rw.check(self.info,other,lambda *a,**k:self.fail('restart cooldown'),self.now)['state'],'COOLDOWN')
  finally:other.read_cache.close()
 def test_unchanged_no_write_or_revision(self):
  with self.s.connect() as db:before=list(db.execute("select * from input_revisions where scope like 'market:%'"))
  self.assertEqual(self.run_check()['state'],'UNCHANGED');self.assertEqual(self.journal(),[])
  with self.s.connect() as db:self.assertEqual(before,list(db.execute("select * from input_revisions where scope like 'market:%'")))
 def test_wrong_identity_currency_venue_source(self):
  for field,value in [('currency','USD'),('venue','FRA'),('source','unconfirmed')]:
   self.s.set_setting('cp_revision_source_next',None)
   with self.s.connect() as db:db.execute("delete from settings where key like 'cp_revision:%'")
   f=self.changed();f.attrs[field]=value;before=self.rows();self.assertEqual(self.run_check(f)['state'],'UNCONFIRMED');self.assertEqual(before,self.rows())
 def test_unconfirmed_listing_no_network(self):
  self.info['eur_listings'][0]['status']='UNCONFIRMED';self.s.save_instrument(self.info)
  self.assertEqual(self.run_check(download=lambda *a,**k:self.fail('unconfirmed'))['state'],'NO_CONFIRMED_PROXY_WINDOW')
 def test_bad_ohlcv_and_dq_preserved(self):
  f=self.changed();f.iloc[1,f.columns.get_loc('high')]=1;before=self.rows();r=self.run_check(f);self.assertEqual(r['state'],'UNCONFIRMED');self.assertEqual(before,self.rows());self.assertEqual(self.s.setting(self.dq)['state'],'GAP')
 def test_shifted_timestamp_no_deletion(self):
  f=self.changed();f.index=f.index+pd.Timedelta(minutes=1);before=self.rows();self.assertEqual(self.run_check(f)['state'],'UNCONFIRMED');self.assertEqual(before,self.rows())
 def test_relevant_race_rejects_old_result(self):
  def racing(*a,**k):
   with self.s.connect() as db:db.execute('update series set volume=7 where ts=?',(str(self.f.index[0]),))
   return self.changed()
  r=self.run_check(download=racing);self.assertIn('REVISION_INPUT_CHANGED',r['reason']);self.assertEqual(self.journal(),[]);self.assertEqual(self.rows()[1][-2],10)
 def test_journal_failure_rolls_back_candle(self):
  before=self.rows()
  with self.s.connect() as db:db.execute("CREATE TRIGGER fail_revision BEFORE INSERT ON candle_revision_history BEGIN SELECT RAISE(ABORT,'test failure'); END")
  self.assertEqual(self.run_check(self.changed())['state'],'UNCONFIRMED');self.assertEqual(before,self.rows())
 def test_source_cooldown_no_request(self):
  self.s.set_setting('cp_source_retry:proxy',(self.now+pd.Timedelta(hours=2)).isoformat());self.assertEqual(self.run_check(download=lambda *a,**k:self.fail('rate limit'))['state'],'SOURCE_COOLDOWN')
 def test_missing_provider_does_not_change_dq(self):
  def fail(*a,**k):raise TimeoutError('offline')
  before=self.rows();self.assertEqual(self.run_check(download=fail)['state'],'UNCONFIRMED');self.assertEqual(before,self.rows());self.assertEqual(self.s.setting(self.dq)['state'],'GAP')
 def test_gettex_not_patched_over_old_trades(self):
  self.info['eur_listings']=[];self.s.save_instrument(self.info);self.s.save_frame(self.info['isin'],'gettex','1H',self.f)
  r=self.run_check(download=lambda *a,**k:self.fail('no authoritative trade correction'))
  self.assertIn('BLOCKED',r['gettex']);self.assertEqual(self.journal(),[])
 def test_forming_only_rows_not_requested(self):
  self.now=pd.Timestamp('2026-10-21T07:30Z')
  r=self.run_check(download=lambda *a,**k:self.fail('forming bar requested'))
  self.assertEqual(r['state'],'NO_CONFIRMED_PROXY_WINDOW');self.assertEqual(self.journal(),[])
 def test_derived_and_indicator_cache_invalidated(self):
  from analytics import resample,indicators
  cache=self.s.read_cache
  def calculate():
   cache.depend('market:'+self.info['isin']+':1H')
   frame=self.s.load_series(self.info['isin'],'1H','proxy','SAP.DE','GER','EUR');frame.attrs.update(self.meta,timeframe='1H')
   return resample(frame,'1H','2H'),indicators(frame)
  with cache.track_inputs() as token:before=cache.get('revision-test',calculate,scoped=True)
  self.assertEqual(self.run_check(self.changed())['state'],'UPDATED')
  with self.assertRaisesRegex(RuntimeError,'Stale'):cache.assert_current(token)
  after=cache.get('revision-test',calculate,scoped=True)
  self.assertNotEqual(before[0].iloc[0]['close'],after[0].iloc[0]['close']);self.assertNotEqual(before[1].iloc[1]['ema10'],after[1].iloc[1]['ema10'])
if __name__=='__main__':unittest.main()
