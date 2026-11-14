import sys,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
import data_quality as dq
import history_service as hs
from performance_cache import frame_dependency,capture_closed_inputs,validate_closed_inputs,InputRevisionChanged
from timeframes import aggregate,LABELS

class ParentRecovery(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.now=pd.Timestamp('2026-10-26T13:00Z');self.meta=dict(source='proxy',symbol='TEST.DE',venue='GER',currency='EUR',timezone='Europe/Berlin')
  self.info=dict(isin='TEST',name='test',eur_listings=[dict(self.meta,isin='TEST',status='CONFIRMED')]);self.store.save_instrument(self.info)
  idx=pd.date_range('2026-10-26 09:00','2026-10-26 11:55',freq='5min')
  self.child=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=2.),index=idx);self.child.attrs={**self.meta,'timeframe':'5m'}
  self.store.save_series(self.info,self.child,'5m')
  self.parent=aggregate(self.child,'5m','1H',self.now);self.parent=self.parent.drop(pd.Timestamp('2026-10-26 10:00'))
  self.store.save_series(self.info,self.parent,'1H')
 def tearDown(self):self.store.read_cache.close();self.tmp.cleanup()
 def repair(self,fetch=None):
  return dq.repair(self.info,self.store,self.meta['symbol'],'1H',self.meta,fetch or (lambda *a,**k:self.fail('Unnecessary network')),self.now,max_ranges=1)
 def rows(self):return self.store.load_series('TEST','1H','proxy','TEST.DE','GER','EUR')
 def test_real_children_atomic_restore_unique_counter_restart_and_revision_revoke(self):
  before=self.rows().copy();self.repair();out=self.rows()
  self.assertEqual(len(out),3);self.assertEqual(out.loc['2026-10-26 10:00','volume'],24.)
  pd.testing.assert_frame_equal(out.loc[before.index,list(dq.FIELDS)],before[list(dq.FIELDS)])
  with self.store.connect() as db:self.assertEqual(db.execute('select count(*) from candle_recoveries').fetchone()[0],1)
  reloaded=Store(self.tmp.name);self.assertFalse(reloaded.load_series('TEST','1H','proxy','TEST.DE','GER','EUR').attrs['invalid_parent_provenance']);reloaded.read_cache.close()
  self.repair()
  with self.store.connect() as db:self.assertEqual(db.execute('select count(*) from candle_recoveries').fetchone()[0],1)
  out.attrs['timeframe']='1H';spec=frame_dependency('TEST',out,'1H',self.now)
  with self.store.connect() as db:guard=capture_closed_inputs(db,[spec],self.store.read_cache.token_for(['instrument:TEST','market:TEST:1H']))
  changed=self.child.loc[['2026-10-26 10:05']].copy();changed['high']=103.;self.store.save_series(self.info,changed,'5m')
  with self.store.connect() as db:
   with self.assertRaises(InputRevisionChanged):validate_closed_inputs(db,guard)
  self.assertEqual(dq.audit(self.rows(),'1H',self.meta,self.now,check_stale=False)['state'],'INVALID')
  self.repair();self.assertEqual(self.rows().loc['2026-10-26 10:00','high'],103.)
 def test_unknown_missing_bad_identity_and_forming_do_not_create_parent(self):
  missing=self.child.drop(pd.Timestamp('2026-10-26 10:05'))
  with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts='2026-10-26 10:05:00'")
  self.assertIsNone(hs.recover_from_children(self.info,self.store,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',self.now))
  self.store.save_series(self.info,self.child,'5m')
  for field,value in [('currency','USD'),('venue','FRA'),('source','other')]:
   self.assertIsNone(hs.recover_from_children(self.info,self.store,{**self.meta,field:value},'1H','2026-10-26 10:00','2026-10-26 10:00',self.now))
  self.assertIsNone(hs.recover_from_children(self.info,self.store,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',pd.Timestamp('2026-10-26T09:30Z')))
  bad=self.child.loc[['2026-10-26 10:05']].copy();bad['high']=1.;self.store.save_series(self.info,bad,'5m')
  self.assertIsNone(hs.recover_from_children(self.info,self.store,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',self.now))
 def test_no_trades_proof_and_empty_bucket_no_synthetic_bar(self):
  proof=dict(isin='TEST',identity=self.meta,basis='complete_published_trade_files',reference='isolated fixture',checked_at=self.now.isoformat(),no_trades=[['2026-10-26 10:05','2026-10-26 10:10']])
  self.store.set_setting('interval_evidence:TEST',proof)
  with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts='2026-10-26 10:05:00'")
  self.repair();self.assertEqual(self.rows().loc['2026-10-26 10:00','volume'],22.)
  self.store.set_setting('interval_evidence:TEST',{})
  self.assertEqual(dq.audit(self.rows(),'1H',self.meta,self.now,check_stale=False)['state'],'INVALID')
  with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts>='2026-10-26 10:00' AND ts<'2026-10-26 11:00'")
  proof['no_trades']=[['2026-10-26 10:00','2026-10-26 11:00']];self.store.set_setting('interval_evidence:TEST',proof)
  self.assertIsNone(hs.recover_from_children(self.info,self.store,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',self.now))
 def test_race_rolls_back_and_valid_parent_conflict_is_not_overwritten(self):
  real=hs.recover_from_children
  def race(*args,**kwargs):
   value=real(*args,**kwargs)
   self.store.save_instrument({**self.info,'changed':True});return value
  with patch.object(hs,'recover_from_children',side_effect=race):report=self.repair()
  self.assertIn('input changed',report['recovery_error']);self.assertEqual(len(self.rows()),2)
  changed=self.child.loc[['2026-10-26 09:05']].copy();changed['high']=103.;self.store.save_series(self.info,changed,'5m')
  with self.assertRaisesRegex(ValueError,'PARENT_CHILD_CONFLICT'):real(self.info,self.store,self.meta,'1H','2026-10-26 09:00','2026-10-26 09:00',self.now)
  self.assertEqual(self.rows().loc['2026-10-26 09:00','high'],102.)
 def test_auxiliary_minute_uses_same_source_is_persisted_and_not_ui_tf(self):
  with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m'")
  minute=self.child.iloc[:1].reindex(pd.date_range('2026-10-26 10:00','2026-10-26 10:59',freq='1min')).ffill();minute.attrs=self.meta.copy()
  minute.loc[:,:]=[100.,102.,99.,101.,2.]
  def failed_parent(*a,**k):raise ValueError('provider missing parent')
  with patch.object(hs,'download',return_value=minute) as fetch:
   self.repair(failed_parent);self.assertEqual(fetch.call_count,1)
  self.assertEqual(self.rows().loc['2026-10-26 10:00','volume'],120.)
  self.assertEqual(len(self.store.load_series('TEST','1m','proxy','TEST.DE','GER','EUR')),60);self.assertNotIn('1m',LABELS)
  with patch.object(hs.requests,'get') as network:
   with self.assertRaisesRegex(ValueError,'OUTSIDE_BOUNDED'):hs.download('TEST.DE','1m',pd.Timestamp('2026-04-20T07:00Z'),pd.Timestamp('2026-04-20T08:00Z'),expected=self.meta)
   network.assert_not_called()
 def test_existing_queue_automatically_attempts_parent_without_manual_instrument_work(self):
  with patch.object(hs,'download',side_effect=AssertionError('network')):
   result=hs.inspect_saved_archive(self.info,self.store,self.now)
  self.assertEqual(result['base'],'1H');self.assertEqual(len(self.rows()),3)

 def test_auxiliary_cannot_replace_conflicting_valid_child(self):
  with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m'")
  minute=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=2.),index=pd.date_range('2026-10-26 10:00','2026-10-26 10:59',freq='1min'));minute.attrs=self.meta.copy()
  self.store.save_series(self.info,minute.iloc[:1],'1m')
  minute.loc[minute.index[0],'high']=104.
  with patch.object(hs,'download',return_value=minute):
   with self.assertRaisesRegex(ValueError,'AUXILIARY_CHILD_CONFLICT'):hs.recover_from_children(self.info,self.store,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',self.now,allow_download=True)
  self.assertEqual(self.store.load_series('TEST','1m','proxy','TEST.DE','GER','EUR').high.iloc[0],102.)

 def test_old_local_children_ignore_http_retention_but_never_query_old_minutes(self):
  with self.store.connect() as db:db.execute("UPDATE series SET ts=replace(ts,'2026-10-26','2024-04-22')")
  self.repair();self.assertEqual(len(self.rows()),3)
  self.assertEqual(self.rows().loc['2024-04-22 10:00','volume'],24.)
 def test_new_child_revision_does_not_bypass_network_cooldown(self):
  key=dq.key(self.info,'TEST.DE','1H');q=dq.inspect_cached(self.info,self.store,'TEST.DE','1H',self.meta,self.now)[1]
  q.update(next_retry=(self.now+pd.Timedelta(hours=1)).isoformat(),parent_child_revisions={s:0 for s in ('market:TEST:1m','market:TEST:5m','market:TEST:15m')})
  self.store.set_setting(key,q)
  with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts='2026-10-26 10:05:00'")
  result=self.repair();self.assertEqual(result['next_retry'],q['next_retry']);self.assertEqual(len(self.rows()),2)

 def test_recovery_bookkeeping_is_not_a_quality_change(self):
  key=dq.key(self.info,'TEST.DE','1H');self.store.set_setting(key,dict(state='READY',parent_child_revisions={'5m':1}))
  before=self.store.read_cache.token_for(['setting:'+key])
  self.store.set_setting(key,dict(state='READY',parent_child_revisions={'5m':2}))
  self.assertEqual(before,self.store.read_cache.token_for(before))
  self.store.set_setting(key,dict(state='INVALID',parent_child_revisions={'5m':2}))
  self.assertNotEqual(before,self.store.read_cache.token_for(before))

 def test_damaged_successful_http_response_uses_valid_auxiliary(self):
  with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m'")
  minute=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=2.),index=pd.date_range('2026-10-26 10:00','2026-10-26 10:59',freq='1min'));minute.attrs=self.meta.copy()
  bad=self.parent.iloc[:1].copy();bad.index=pd.to_datetime(['2026-10-26 10:00']);bad['high']=0.
  with patch.object(hs,'download',return_value=minute) as fetch:
   self.repair(lambda *a,**k:bad);self.assertEqual(fetch.call_count,1)
  self.assertEqual(self.rows().loc['2026-10-26 10:00','volume'],120.)
 def test_queued_old_listing_cannot_override_current_revoked_proof(self):
  self.store.save_instrument({**self.info,'eur_listings':[]})
  self.assertIsNone(hs.recover_from_children(self.info,self.store,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',self.now))

 def test_all_native_parent_bases_share_existing_aggregation(self):
  from analytics import resample
  for base,child in [('5m','1m'),('15m','5m'),('1D','1H')]:
   with self.subTest(base=base),tempfile.TemporaryDirectory() as folder:
    store=Store(folder);store.save_instrument(self.info)
    idx=dq.expected_index(self.meta,child,'2026-10-21','2026-10-23 18:00',self.now)
    raw=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=2.),index=idx);raw.attrs={**self.meta,'timeframe':child}
    complete=aggregate(raw,child,base,self.now) if child=='1m' else resample(raw,child,base,self.now)
    hole=complete.index[len(complete)//2];original=complete.drop(hole);store.save_series(self.info,raw,child);store.save_series(self.info,original,base)
    dq.repair(self.info,store,self.meta['symbol'],base,self.meta,lambda *a,**k:self.fail('network'),self.now,max_ranges=1)
    actual=store.load_series('TEST',base,'proxy','TEST.DE','GER','EUR')
    self.assertIn(hole,actual.index)
    for column in dq.FIELDS:self.assertEqual(float(actual.loc[hole,column]),float(complete.loc[hole,column]))
    store.read_cache.close()

if __name__=='__main__':unittest.main()
