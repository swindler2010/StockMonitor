"""Owner GETTEX venue policy and bounded cache accumulation. Memory only."""
import gzip,json,unittest
from types import SimpleNamespace
from unittest.mock import patch
from test_gettex_live_evidence import MemoryStore,pd,gh,StockMonitor
from storage import Store
from eur_listings import OWNER_HISTORY_POLICY,owner_history_info,venue_allowed

POLICY=dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX',accumulate_cached_history=True)
APPLE='US0378331005'
SAP='DE0007164600'

class CachedHistory(unittest.TestCase):
 def setUp(self):
  self.s=MemoryStore();self.addCleanup(self.s.db.close)
  self.s.db.executescript('''DROP TABLE instruments;CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT);
   DROP TABLE candles;CREATE TABLE candles(isin TEXT,source TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(isin,source,base_tf,ts));
   CREATE TABLE trades(isin TEXT,ts TEXT,price REAL,volume REAL,PRIMARY KEY(isin,ts,price,volume));
   CREATE TABLE writes(kind TEXT);CREATE TRIGGER ci AFTER INSERT ON candles BEGIN INSERT INTO writes VALUES ('insert');END;
   CREATE TRIGGER cu AFTER UPDATE ON candles BEGIN INSERT INTO writes VALUES ('update');END;''')
  def setting(k,v):
   with self.s.connect() as db:db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(k,json.dumps(v)))
  self.s.set_setting=setting;setting(OWNER_HISTORY_POLICY,POLICY)
  Store.save_instrument(self.s,dict(isin=APPLE,name='Apple',currency='USD'))
  self.now=pd.Timestamp('2026-11-03T11:00Z')
  self.parse=lambda *a,**k:StockMonitor._parse_gettex_csv(None,*a,**k)
  self.munc=self.file('munc');self.mund=self.file('mund')
  self.content=f'{APPLE},10:01:00,EUR,250,2\n{APPLE},10:07:00,EUR,251,3\n{SAP},10:02:00,EUR,200,4\n'.encode()
  self.munc.write_bytes(gzip.compress(self.content))
  self.mund.write_bytes(gzip.compress(b'DE0007236101,10:02:00,EUR,200,4\n'))
 def file(self,mic,label='20261103.10.15'):return self.s.directory/'gettex_files'/f'posttrade.{label}.{mic}.csv.gz'
 def run_turn(self,parse=None,now=None):return gh.accumulate_cached_pair(self.s,parse or self.parse,now or self.now)
 def rows(self):return self.s.db.execute('SELECT * FROM candles ORDER BY base_tf,ts').fetchall()
 def test_missing_real_bars_added_and_provenance_saved(self):
  r=self.run_turn();self.assertEqual(r['added'],3);self.assertEqual(r['state'],'ACCUMULATED')
  day=self.s.db.execute("SELECT open,high,low,close,volume FROM candles WHERE base_tf='15m'").fetchone()
  self.assertEqual(day,(250,251,250,251,5))
  self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM trades').fetchone()[0],2)
  p=self.s.setting('gettex_cached_history');self.assertEqual(len(p['last_result']['sha256']),2)
  self.assertNotIn('interval_evidence:'+APPLE,dict(self.s.db.execute('SELECT * FROM settings')))
 def test_restart_unchanged_checkpoint_prevents_reparse_and_rewrite(self):
  self.run_turn();before=self.rows();count=self.s.db.total_changes
  # New facade, same persisted SQLite/cache; no in-process progress is required.
  other=SimpleNamespace(directory=self.s.directory,connect=self.s.connect,setting=self.s.setting,set_setting=self.s.set_setting,instruments=self.s.instruments)
  r=gh.accumulate_cached_pair(other,lambda *a,**k:self.fail('unchanged reparse'),self.now)
  self.assertEqual(r['state'],'IDLE');self.assertEqual(self.rows(),before);self.assertEqual(count,self.s.db.total_changes)
 def test_new_stock_reuses_shared_cache_without_rewriting_old_stock(self):
  self.run_turn();old=self.rows()
  Store.save_instrument(self.s,dict(isin=SAP,name='SAP'))
  r=self.run_turn();self.assertEqual(r['added'],2)
  self.assertEqual([r for r in self.rows() if r[0]==APPLE],old)
  self.assertEqual(self.s.setting('gettex_cached_history')['registered_stocks'],2)
 def test_conflicting_candle_blocks_trade_ledger_contamination(self):
  self.s.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(APPLE,'gettex','15m','2026-11-03 11:00:00',249,249,249,249,2));self.s.db.commit()
  old=self.rows();r=self.run_turn()
  self.assertEqual(r['conflicts'],1);self.assertEqual(r['added'],0);self.assertEqual(self.rows(),old)
  self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM trades').fetchone()[0],0)
 def test_different_retained_trades_block_partial_reconstruction(self):
  self.s.db.execute('INSERT INTO trades VALUES (?,?,?,?)',(APPLE,'2026-11-03 11:09:00',253,2));self.s.db.commit()
  r=self.run_turn();self.assertEqual(r['errors'][APPLE],'EXISTING_TRADE_COVERAGE_DIFFERS');self.assertEqual(r['added'],0)
  self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM trades').fetchone()[0],1)
 def test_existing_identical_candles_are_not_rewritten(self):
  self.run_turn();old=self.rows();self.s.set_setting('gettex_cached_history',{})
  writes=self.s.db.execute('SELECT COUNT(*) FROM writes').fetchone()[0]
  self.assertEqual(self.run_turn()['added'],0);self.assertEqual(old,self.rows())
  self.assertEqual(writes,self.s.db.execute('SELECT COUNT(*) FROM writes').fetchone()[0])
 def test_changed_source_file_is_left_for_existing_recovery(self):
  self.run_turn();old=self.rows();self.munc.write_bytes(gzip.compress(self.content.replace(b'250,2',b'252,2')))
  r=self.run_turn();self.assertEqual(r['state'],'BLOCKED');self.assertIn('CHANGED_SOURCE_FILE',r['reason']);self.assertEqual(old,self.rows())
  self.assertEqual(self.run_turn()['state'],'IDLE')
 def test_usd_is_not_imported_and_invalid_eur_is_not_ready(self):
  self.munc.write_bytes(gzip.compress(f'{APPLE},10:01:00,USD,250,2\n'.encode()))
  self.assertEqual(self.run_turn()['added'],0);self.assertFalse(self.rows())
  self.s.set_setting('gettex_cached_history',{});self.munc.write_bytes(gzip.compress(f'{APPLE},10:01:00,EUR,-1,2\n'.encode()))
  r=self.run_turn();self.assertEqual(r['state'],'REVIEW_REQUIRED');self.assertIn(APPLE,r['errors']);self.assertFalse(self.rows())
 def test_incorrect_timestamp_stays_blocked(self):
  self.munc.write_bytes(gzip.compress(f'{APPLE},09:59:00,EUR,250,2\n'.encode()))
  r=self.run_turn();self.assertEqual(r['errors'][APPLE],'INVALID_TRADE_TIMESTAMP');self.assertFalse(self.rows())
 def test_empty_publication_does_not_prove_no_trades(self):
  self.mund.write_bytes(gzip.compress(b''))
  r=self.run_turn();self.assertEqual(r['state'],'BLOCKED');self.assertFalse(self.rows())
  self.assertEqual(self.run_turn()['state'],'IDLE')
 def test_only_closed_complete_pairs_and_no_synthetic_endpoint(self):
  self.assertEqual(self.run_turn(now=pd.Timestamp('2026-11-03T10:14Z'))['state'],'IDLE')
  self.munc.write_bytes(gzip.compress(self.content+f'{APPLE},10:15:00,EUR,252,1\n'.encode()))
  self.run_turn();self.assertEqual(len(self.rows()),3)
  self.assertEqual(self.s.setting('gettex_cached_history')['last_result']['endpoint_rows_deferred'],1)
 def test_single_pair_budget_then_resume(self):
  for mic in ('munc','mund'):self.file(mic,'20261103.10.30').write_bytes(gzip.compress(f'{APPLE},10:20:00,EUR,252,1\n'.encode()))
  self.assertEqual(self.run_turn()['pair'],'20261103.10.15')
  self.assertEqual(self.run_turn()['pair'],'20261103.10.30')
  self.assertEqual(self.run_turn()['state'],'IDLE')
 def test_changed_policy_during_parse_cannot_publish(self):
  def race(*a,**k):
   out=self.parse(*a,**k);self.s.set_setting(OWNER_HISTORY_POLICY,dict(POLICY,approved=False));return out
  self.assertEqual(self.run_turn(parse=race)['state'],'INPUT_CHANGED');self.assertFalse(self.rows())
  self.assertIsNone(self.s.setting('gettex_cached_history'))
 def test_missing_pair_never_imports(self):
  self.mund.unlink();self.assertEqual(self.run_turn()['state'],'IDLE');self.assertFalse(self.rows())
 def test_watchlist_save_and_new_stock_inherit_venue_not_provider(self):
  benchmark=dict(isin='IDX',kind='benchmark',currency='USD',history_source='yahoo')
  Stock=owner_history_info(dict(isin=APPLE,currency='USD'),POLICY)
  self.assertEqual(Stock['currency'],'USD');self.assertEqual(Stock['history_venue_lock'],'GETTEX')
  self.assertNotIn('history_provider_lock',Stock)
  with self.s.connect() as db:Store._write_watchlist(db,[(APPLE,json.dumps(Stock)),('IDX',json.dumps(benchmark))])
  self.assertEqual(self.s.instruments()[1],benchmark)
  for provider in ('gettex','gettex_chart','onvista'):
   self.assertTrue(venue_allowed(Stock,dict(source=provider,venue='GETTEX',currency='EUR')))
  self.assertFalse(venue_allowed(Stock,dict(source='proxy',venue='FRA',currency='EUR')))
  self.assertFalse(venue_allowed(Stock,dict(source='gettex_chart',venue='GETTEX',currency='USD')))
 def test_planner_filters_foreign_saved_archive_without_deleting_it(self):
  import candle_schedule as cs
  info=dict(isin=APPLE);foreign=dict(source='proxy',venue='FRA',symbol='APC.F',currency='EUR',timezone='Europe/Berlin')
  self.s.set_setting('history_identity:'+APPLE,foreign);self.s.set_setting('cp_maintenance:'+APPLE,dict(at=self.now.isoformat()))
  seen=[]
  def decision(store,isin,meta,base,now):seen.append(meta);return dict(due=False)
  with patch.object(cs,'decision',side_effect=decision),patch.object(cs,'fresh_candidate',return_value=(None,{})),patch('calculation_window.eligibility_due',return_value=None),patch('cp_controls.due_request',return_value=None),patch('cp_controls.historical_review_due',return_value=None),patch('cp_reconciliation.due_observation',return_value=None):
   r=cs.planned_feeds(self.s,[info],self.now)
  self.assertTrue(seen);self.assertTrue(all(m['venue']=='GETTEX' for m in seen));self.assertEqual(r['feeds'],[])
  self.assertEqual(self.s.setting('history_identity:'+APPLE),foreign)
 def test_owner_policy_preserves_long_gettex_chart_over_short_official(self):
  import test_display_history_depth as depth
  from desktop014 import Desktop014
  info=Desktop014.user_state(SimpleNamespace(drafts={},store=self.s),depth.INFO)
  full=depth.daily('2026-01-02','2026-10-30');short=full.iloc[-5:].copy()
  short.attrs.update(source='gettex',symbol=APPLE)
  fixture=depth.DisplayDepth();store=fixture.cached_store();store.connect=self.s.connect;self.addCleanup(fixture.doCleanups)
  with patch.object(depth,'Memory',return_value=store):
   for tf in ('1D','1W','1M'):
    out=fixture.choose(depth.resample(short,'1D',tf,depth.NOW),{'1D':full},tf,info)
    self.assertEqual(out.attrs['source'],'gettex_chart');self.assertLess(out.index[0],short.index[0])
  self.assertEqual(len(full),len(depth.daily('2026-01-02','2026-10-30')))
 def test_unapproved_policy_does_not_change_source_or_start_cache_import(self):
  self.s.set_setting(OWNER_HISTORY_POLICY,dict(POLICY,approved=False))
  info=dict(isin=SAP,history_symbol='SAP.DE')
  Store.save_instrument(self.s,info)
  self.assertEqual(self.s.instruments()[-1],info)
  self.assertEqual(self.run_turn()['state'],'NOT_ENABLED')
 def test_universe_change_during_parse_rejects_receipt_and_all_rows(self):
  def race(*a,**k):
   out=self.parse(*a,**k);Store.save_instrument(self.s,dict(isin=SAP));return out
  self.assertEqual(self.run_turn(parse=race)['state'],'INPUT_CHANGED');self.assertFalse(self.rows())
  self.assertIsNone(self.s.setting('gettex_cached_history'))

if __name__=='__main__':unittest.main()
