import gzip,sys,tempfile,unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
import gettex_history as gh

class MissingFileRotation(unittest.TestCase):
 def setUp(self):
  archive=Path(__file__).resolve().parents[2]/'СМ-Архив'
  self.tmp=tempfile.TemporaryDirectory(dir=archive if archive.exists() else None)
  self.s=Store(self.tmp.name);self.info={'isin':'DE0007164600'}
  self.now=pd.Timestamp('2026-10-24T08:00Z');self.calls=[]
  self.key='data_quality:DE0007164600:DE0007164600:15m'
  self.report=dict(state='UNCONFIRMED',identity=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=self.info['isin'],timezone='Europe/Berlin'),ranges=[['2026-10-14 22:45','2026-10-14 22:45']])
  self.s.set_setting(self.key,self.report)
 def tearDown(self):self.s.read_cache.close();self.tmp.cleanup()
 def http(self,url):
  if url==gh.PAGE:return b'<a href="https://erdk.bayerische-boerse.de/data/posttrade.20261023.06.00.munc.csv.gz">file</a>'
  self.calls.append(url.rsplit('/',1)[-1])
  if self.mode=='all' or len(self.calls)==1:raise HTTPError(url,404,'missing',{},None)
  if self.mode=='limited':raise HTTPError(url,429,'limited',{'Retry-After':'172800'},None)
  if self.mode=='corrupt':return b'not gzip'
  return gzip.compress(b'DE0007164600,05:40:00,EUR,10,1\n')
 def test_missing_first_file_does_not_hide_available_neighbours(self):
  self.mode='first';r=gh.acquire_missing_evidence(self.info,self.s,self.http,self.now,budget=3)
  self.assertEqual(r['downloaded'],2);self.assertEqual(len(self.calls),3)
  self.assertFalse((self.s.directory/'gettex_files'/self.calls[0]).exists())
  self.assertEqual(self.s.setting(self.key),self.report)
  self.assertEqual(self.s.setting('interval_evidence:'+self.info['isin'],{}),{})
 def test_all_missing_cursor_survives_reopen_and_cooldown(self):
  self.mode='all';gh.acquire_missing_evidence(self.info,self.s,self.http,self.now,budget=2)
  first=list(self.calls);self.assertEqual(len(first),2)
  other=Store(self.tmp.name)
  try:
   self.assertEqual(gh.acquire_missing_evidence(self.info,other,lambda u:self.fail('cooldown'),self.now+pd.Timedelta(minutes=1))['state'],'COOLDOWN')
   self.calls=[];gh.acquire_missing_evidence(self.info,other,self.http,self.now+pd.Timedelta(days=1,minutes=1),budget=2)
   self.assertEqual(len(self.calls),2);self.assertFalse(set(first)&set(self.calls))
  finally:other.read_cache.close()
 def test_429_after_404_stops_batch_and_preserves_retry_after(self):
  self.mode='limited';gh.acquire_missing_evidence(self.info,self.s,self.http,self.now,budget=8)
  self.assertEqual(len(self.calls),2)
  self.assertEqual(pd.Timestamp(self.s.setting('candlepilot_source_cooldown')),self.now+pd.Timedelta(days=2))
 def test_cursor_wraps_only_after_independent_files_have_turns(self):
  self.mode='all'
  schedule=pd.DataFrame(dict(open=[pd.Timestamp('2026-10-14T05:30Z')],close=[pd.Timestamp('2026-10-14T06:15Z')]))
  with patch('timeframes.schedule',return_value=schedule):
   for n in range(4):gh.acquire_missing_evidence(self.info,self.s,self.http,self.now+pd.Timedelta(days=n,minutes=n),budget=2)
  self.assertEqual(len(self.calls),8);self.assertEqual(len(set(self.calls[:6])),6)
  self.assertEqual(self.calls[:2],self.calls[6:])
 def test_other_response_failure_still_stops_the_batch(self):
  self.mode='corrupt';r=gh.acquire_missing_evidence(self.info,self.s,self.http,self.now,budget=8)
  self.assertEqual(len(self.calls),2);self.assertEqual(r['state'],'UNCONFIRMED')
  self.assertEqual(list((self.s.directory/'gettex_files').glob('*.gz')),[])
 def test_interruption_after_lease_preserves_completed_cursor(self):
  self.mode='all';gh.acquire_missing_evidence(self.info,self.s,self.http,self.now,budget=2)
  first=list(self.calls)
  later=self.now+pd.Timedelta(days=1,minutes=1)
  def interrupted(url):raise KeyboardInterrupt()
  with self.assertRaises(KeyboardInterrupt):gh.acquire_missing_evidence(self.info,self.s,interrupted,later,budget=2)
  other=Store(self.tmp.name)
  try:
   self.calls=[];gh.acquire_missing_evidence(self.info,other,self.http,later+pd.Timedelta(minutes=31),budget=2)
   self.assertEqual(len(self.calls),2);self.assertFalse(set(first)&set(self.calls))
  finally:other.read_cache.close()

if __name__=='__main__':unittest.main()
