import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
ROOT=Path(__file__).parent;sys.path.insert(0,str(ROOT/'stock_monitor'))
from storage import Store
import data_quality as dq
import history_service as hs
import gettex_chart as chart

class LocalRecoveryCooldown(unittest.TestCase):
 def setUp(self):
  self.fixture=json.loads((ROOT/'history64-evidence/readiness30/real-local-fixture.json').read_text(encoding='utf8'))
  self.tmp=tempfile.TemporaryDirectory(prefix='cp-local-recovery-');self.addCleanup(self.tmp.cleanup)
  self.store=Store(self.tmp.name);self.addCleanup(lambda:self.store.read_cache.close())
  self.info=self.fixture['info'];self.meta=self.fixture['identity'];self.now=pd.Timestamp(self.fixture['at'])
  self.stamp=pd.Timestamp(self.fixture['target']);self.wait=(self.now+pd.Timedelta(hours=2)).isoformat()
  self.store.save_instrument(self.info)
  for tf,rows in self.fixture['frames'].items():
   f=pd.DataFrame(rows,columns=['ts',*dq.FIELDS]).set_index('ts');f.index=pd.to_datetime(f.index);f.attrs=dict(self.meta,timeframe=tf)
   if tf=='15m':self.expected=f.copy();f=f.drop(self.stamp)
   else:self.child=f.copy()
   self.store.save_series(self.info,f,tf)
   self.store.set_setting(chart.state_key(self.info['isin'],tf),self.fixture['states'][tf])
  self.store.set_setting('cp_source_retry:gettex_chart',self.wait)
 def rows(self):return self.store.load_series(self.info['isin'],'15m',self.meta['source'],self.meta['symbol'],self.meta['venue'],self.meta['currency'])
 def turn(self):
  with patch.object(chart.Client,'fetch',side_effect=AssertionError('Network forbidden during provider cooldown')) as network:
   result=hs.inspect_saved_archive(self.info,self.store,self.now)
   network.assert_not_called()
  return result
 def test_provider_wait_does_not_block_exact_real_local_repair(self):
  result=self.turn();out=self.rows()
  self.assertEqual(result['base'],'15m');self.assertIn(self.stamp,out.index)
  pd.testing.assert_frame_equal(out[list(dq.FIELDS)],self.expected[list(dq.FIELDS)],check_dtype=False)
  self.assertFalse(out.attrs['invalid_parent_provenance'])
  self.assertEqual(self.store.setting('cp_source_retry:gettex_chart'),self.wait)
  with self.store.connect() as db:self.assertEqual(db.execute('select count(*) from candle_recoveries').fetchone()[0],1)
  self.store.read_cache.close();self.store=Store(self.tmp.name)
  self.turn();self.turn()
  with self.store.connect() as db:self.assertEqual(db.execute('select count(*) from candle_recoveries').fetchone()[0],1)
 def test_incomplete_children_keep_block_and_original_retry(self):
  with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts=?",(str(self.stamp+pd.Timedelta(minutes=5)),))
  result=self.turn();self.assertNotIn(self.stamp,self.rows().index)
  report=self.store.setting(dq.key(self.info,self.meta['symbol'],'15m'),{})
  self.assertNotEqual(report['state'],'READY');self.assertEqual(report['next_retry'],self.wait)
  self.assertEqual(result['recovery'],'WAITING_PROVIDER');self.assertFalse(report.get('attempts'))
 def test_new_children_can_repair_during_unchanged_provider_wait(self):
  missing=self.child.loc[[self.stamp+pd.Timedelta(minutes=5)]].copy()
  with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts=?",(str(missing.index[0]),))
  self.turn();self.store.save_series(self.info,missing,'5m')
  # The existing fair queue visits the never-audited 5m first, then 15m.
  self.turn();self.turn()
  self.assertIn(self.stamp,self.rows().index);self.assertEqual(self.store.setting('cp_source_retry:gettex_chart'),self.wait)
 def test_bad_child_does_not_pass_dq_or_fabricate_parent(self):
  bad=self.child.loc[[self.stamp+pd.Timedelta(minutes=5)]].copy();bad['high']=0.
  self.store.save_series(self.info,bad,'5m');self.turn()
  self.assertNotIn(self.stamp,self.rows().index)
  self.assertNotEqual(self.store.setting(dq.key(self.info,self.meta['symbol'],'15m'),{})['state'],'READY')
 def test_identity_change_or_revision_race_never_writes(self):
  for mismatch in ('currency','venue','source'):
   with self.subTest(mismatch=mismatch):
    wrong=dict(self.fixture['states']['15m']);wrong['identity']={**self.meta,mismatch:'wrong'}
    self.store.set_setting(chart.state_key(self.info['isin'],'15m'),wrong)
    self.assertIsNone(hs.recover_from_children(self.info,self.store,self.meta,'15m',str(self.stamp),str(self.stamp),self.now))
  self.store.set_setting(chart.state_key(self.info['isin'],'15m'),self.fixture['states']['15m'])
  real=hs.recover_from_children
  def changed(*a,**kw):
   result=real(*a,**kw);self.store.save_instrument({**self.info,'test_revision_changed':True});return result
  with patch.object(hs,'recover_from_children',side_effect=changed):self.turn()
  self.assertNotIn(self.stamp,self.rows().index)
  report=self.store.setting(dq.key(self.info,self.meta['symbol'],'15m'),{})
  self.assertIn('Recovery input changed',report['recovery_error'])
 def test_legacy_provider_wait_without_local_attempt_is_not_local_cooldown(self):
  report=dq.inspect_cached(self.info,self.store,self.meta['symbol'],'15m',self.meta,self.now)[1]
  report['next_retry']=self.wait;self.store.set_setting(dq.key(self.info,self.meta['symbol'],'15m'),report)
  self.turn();self.assertIn(self.stamp,self.rows().index)
 def test_same_children_are_not_retried_and_later_network_cooldown_is_preserved(self):
  with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts=?",(str(self.stamp+pd.Timedelta(minutes=5)),))
  self.turn()
  report=self.store.setting(dq.key(self.info,self.meta['symbol'],'15m'),{})
  with patch.object(hs,'recover_from_children',side_effect=AssertionError('Unchanged local retry')):
   again=dq.repair(self.info,self.store,self.meta['symbol'],'15m',self.meta,lambda *a,**k:self.fail('HTTP'),self.now,network_not_before=self.wait)
  self.assertEqual(again,report)
 def test_new_child_revision_never_allows_http_before_later_retry_after(self):
  report=dq.inspect_cached(self.info,self.store,self.meta['symbol'],'15m',self.meta,self.now)[1]
  later=(self.now+pd.Timedelta(hours=3)).isoformat();report.update(next_retry=later,parent_child_revisions={})
  self.store.set_setting(dq.key(self.info,self.meta['symbol'],'15m'),report)
  with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts=?",(str(self.stamp+pd.Timedelta(minutes=5)),))
  self.turn();self.assertEqual(self.store.setting(dq.key(self.info,self.meta['symbol'],'15m'),{})['next_retry'],later)
  self.assertNotIn(self.stamp,self.rows().index)

if __name__=='__main__':unittest.main()
