"""Exact calculation reuse: isolated inputs, never the running account/archive."""
import copy,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
import data_quality as dq
import analytics
from calculation_reuse import using
from performance_cache import ReadCache,InputRevisionChanged
from snapshot_worker import SnapshotContext,Value


class ExactReuse(unittest.TestCase):
 def setUp(self):
  archive=Path(__file__).parent.parent.parent/'СМ-Архив'
  with tempfile.NamedTemporaryFile(prefix='cp-reuse-',suffix='.sqlite3',dir=archive,delete=False) as f:self.path=Path(f.name)
  self.writer=sqlite3.connect(self.path)
  self.writer.execute('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER)')
  self.writer.execute("INSERT INTO input_revisions VALUES('instrument:TEST',1)")
  self.writer.execute("INSERT INTO input_revisions VALUES('market:TEST:15m',1)");self.writer.commit()
  self.cache=ReadCache(self.path)
  self.addCleanup(self.path.unlink);self.addCleanup(self.writer.close);self.addCleanup(self.cache.close)
  self.now=pd.Timestamp('2026-11-10T12:00Z')
  self.meta=dict(source='gettex_chart',symbol='TEST.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin',timeframe='15m')
  self.frame=pd.DataFrame(dict(open=[10.]*40,high=[11.]*40,low=[9.]*40,close=[10.]*40,volume=[1.]*40),index=pd.date_range('2026-11-09 07:30',periods=40,freq='15min'))
  self.frame.attrs={**self.meta,'data_quality':{'state':'READY'}}
 def audit(self,f=None,**kw):return dq.audit(self.frame if f is None else f,'15m',now=self.now,check_stale=False,**kw)
 def test_exact_repeat_reuses_but_returns_independent_evidence(self):
  with patch('data_quality.expected_index',wraps=dq.expected_index) as build,using(self.cache):
   first=self.audit();calls=build.call_count;first['issues'].append('caller mutation')
   second=self.audit()
   self.assertEqual(build.call_count,calls);self.assertNotIn('caller mutation',second['issues'])
   self.assertEqual(second,dq.audit.__wrapped__(self.frame,'15m',now=self.now,check_stale=False)|{'checked_at':second['checked_at']})
 def test_changed_ohlcv_identity_evidence_and_clock_are_not_reused(self):
  with using(self.cache):
   self.assertEqual(self.audit()['state'],'READY')
   bad=self.frame.copy();bad.iloc[-1,bad.columns.get_loc('close')]=99
   self.assertEqual(self.audit(bad)['state'],'INVALID')
   foreign=self.frame.copy();foreign.attrs['currency']='USD'
   self.assertEqual(self.audit(foreign,expected=self.meta)['state'],'INVALID')
   proof=self.frame.copy();proof.attrs['classify_absence']=True;proof.attrs['absence_evidence']={'new':'unconfirmed'}
   with patch('data_quality.expected_index',wraps=dq.expected_index) as build:
    self.audit(proof);self.assertGreater(build.call_count,0)
   with patch('data_quality.expected_index',wraps=dq.expected_index) as build:
    dq.audit(self.frame,'15m',now=self.now+pd.Timedelta(minutes=15),check_stale=False)
    self.assertGreater(build.call_count,0)
 def test_implicit_clock_never_reuses_and_scope_does_not_escape(self):
  with patch('data_quality.expected_index',wraps=dq.expected_index) as build:
   with using(self.cache):
    dq.audit(self.frame,'15m',check_stale=False);count=build.call_count
    dq.audit(self.frame,'15m',check_stale=False);self.assertGreater(build.call_count,count)
    self.audit();count=build.call_count
   self.audit();self.assertGreater(build.call_count,count)
 def test_unrelated_write_reuses_exact_inputs_but_db_read_cannot_claim_purity(self):
  with using(self.cache),patch('data_quality.expected_index',wraps=dq.expected_index) as build:
   self.audit();count=build.call_count
   self.writer.execute("INSERT INTO input_revisions VALUES('market:OTHER:15m',1)");self.writer.commit()
   self.audit();self.assertEqual(build.call_count,count)
  def impure():self.cache.depend('market:TEST:15m');return 'unsafe'
  with self.assertRaisesRegex(ValueError,'read database dependencies'):
   self.cache.get('bad',impure,content_addressed=True)
  self.assertNotIn('bad',self.cache.items)
  with self.assertRaises(ValueError):self.cache.get('bad',lambda:None,scoped=True,content_addressed=True)
 def test_indicator_warmup_flags_and_dq_blockers_remain_exact(self):
  with using(self.cache),patch('analytics._analyse_features',wraps=analytics._analyse_features) as build:
   first=analytics._safe_features(self.frame,{},self.now);count=build.call_count
   second=analytics._safe_features(self.frame,{},self.now)
   self.assertEqual(build.call_count,count);pd.testing.assert_frame_equal(first[0],second[0]);self.assertEqual(first[1],second[1])
   blocked=self.frame.copy();blocked.attrs['data_quality']={'state':'INVALID','issues':['bad']}
   result=analytics._safe_features(blocked,{},self.now)[1]
   self.assertFalse(result['sufficient_history']);self.assertFalse(result['ohlcv_valid'])
   analytics._safe_features(self.frame,{},self.now,prove_scope=True,demo_initialization=True)
   self.assertGreater(build.call_count,count+1)
 def context(self):
  c=SnapshotContext();c.store=SimpleNamespace(read_cache=self.cache);c.user_state=lambda info:dict(info)
  c.set_analysis_scope(self.now);c.period=Value('Max');return c
 def loader(self,ctx,info,tf,required_identity=None):
  self.cache.depend('market:TEST:15m');out=self.frame.copy();out.attrs.update(required_identity or {},timeframe=tf)
  return out
 def test_pinned_series_replays_dependencies_and_each_clone_registers_proof(self):
  c=self.context();info={'isin':'TEST'};identity={k:self.meta[k] for k in dq.IDENTITY}
  with patch('desktop014.Desktop014._frame_uncached',side_effect=self.loader) as build:
   with self.cache.track_inputs():c.pinned_frame(info,'15m',identity)
   clone=copy.copy(c);clone.closed_specs={}
   with self.cache.track_inputs() as token:
    frame=clone.pinned_frame(info,'15m',identity);clone.closed_frame(info,frame,'15m')
   self.assertEqual(build.call_count,1);self.assertIn('market:TEST:15m',token);self.assertEqual(len(clone.closed_specs),1)
   self.writer.execute("UPDATE input_revisions SET revision=2 WHERE scope='market:TEST:15m'");self.writer.commit()
   with self.assertRaises(InputRevisionChanged):self.cache.assert_current(token)
   c.pinned_frame(info,'15m',identity);self.assertEqual(build.call_count,2)
 def test_source_tf_scope_and_stock_cannot_share_pinned_result(self):
  c=self.context();identity={k:self.meta[k] for k in dq.IDENTITY};info={'isin':'TEST'}
  with patch('desktop014.Desktop014._frame_uncached',side_effect=self.loader) as build:
   c.pinned_frame(info,'15m',identity);c.pinned_frame(info,'1H',identity)
   c.pinned_frame(info,'15m',dict(identity,source='other'))
   c.pinned_frame({'isin':'OTHER'},'15m',identity)
   c.set_analysis_scope(self.now+pd.Timedelta(minutes=15));c.pinned_frame(info,'15m',identity)
   self.assertEqual(build.call_count,5)
 def test_53_and_200_stock_inputs_stay_bounded_and_independent(self):
  for count in (53,200):
   with self.subTest(count=count),using(self.cache):
    for i in range(count):
     f=self.frame.copy();f.attrs['symbol']=str(i)+'.GTX'
     self.assertEqual(self.audit(f)['identity']['symbol'],str(i)+'.GTX')
    self.assertLessEqual(len(self.cache.analytics_items),self.cache.analytics_limit)


if __name__=='__main__':unittest.main()
