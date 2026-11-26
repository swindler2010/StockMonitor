"""Only new exchange-interruption cases; never import TradingView prices."""
import copy,json,pathlib,sys,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).parent/'stock_monitor'))
import pandas as pd
import data_quality as dq
import gettex_history as gh
import cp_reconciliation as rv
from storage import Store
from timeframes import aggregate

class Interruption(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.s=Store(self.temp.name)
  self.i={'isin':'DE000ENER6Y0','name':'Siemens Energy'};self.s.save_instrument(self.i)
  self.m=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=self.i['isin'],timezone='Europe/Berlin')
  self.now=pd.Timestamp('2026-11-02T18:00Z')
  self.p=dict(isin=self.i['isin'],identity=self.m,basis='complete_published_trade_files',checked_at=self.now.isoformat(),reference={'GETTEX:20261026':{}},no_trades=[],trade_intervals=[],zero_quantity_intervals=[])
  self.s.set_setting('interval_evidence:'+self.i['isin'],self.p)
  import paper_account
  with self.s.connect() as d:paper_account.initialize(d)
  with self.s.connect() as d:d.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.i['isin'],'gettex','5m','2026-10-26 09:55:00',100,101,99,100,10))
 def tearDown(self):self.s.read_cache.close();self.temp.cleanup()
 def install(self):
  result=gh.attach_known_interruptions(self.i,self.s,self.now)
  self.p=self.s.setting('interval_evidence:'+self.i['isin']);return result
 def meta(self):return dict(self.m,classify_absence=True,absence_evidence=self.p,timeframe='5m')
 def frame(self,stamps,span=None):
  f=pd.DataFrame([[100,101,99,100,10] for _ in stamps],index=pd.to_datetime(stamps),columns=dq.FIELDS);f.attrs=self.meta()
  if span:f.attrs['intraday_span']=span
  return f
 def test_existing_writer_attaches_once_preserves_market_and_owner_data(self):
  with self.s.connect() as d:before={t:d.execute('SELECT * FROM '+t).fetchall() for t in ('instruments','candles','series','paper_accounts','paper_fills')}
  self.assertTrue(self.install());self.assertFalse(self.install())
  self.assertEqual(self.p['no_trades'],[]);self.assertEqual(self.p['trade_intervals'],[])
  with self.s.connect() as d:self.assertEqual(before,{t:d.execute('SELECT * FROM '+t).fetchall() for t in before})
 def test_exact_closed_interval_is_market_closed_not_no_trades(self):
  self.install();r=dq.interval_status(self.meta(),'2026-10-26 08:30','2026-10-26 09:15',self.now)
  self.assertEqual(r['state'],'MARKET_CLOSED');self.assertIn('page=108',r['evidence']['reference'])
  self.p['no_trades']=[['2026-10-26 09:50','2026-10-26 09:55']]
  f=self.frame(['2026-10-26 09:55'],['2026-10-26 08:30','2026-10-26 09:55'])
  r=dq.audit(f,'5m',now=self.now,check_stale=False)
  self.assertEqual(r['state'],'READY');self.assertEqual(r['interval_classification']['market_closed_intervals'],16);self.assertEqual(r['interval_classification']['NO_TRADES'],1)
 def test_chart_wrong_currency_venue_and_isin_cannot_borrow_notice(self):
  self.install()
  for fields in ({'source':'gettex_chart'},{'currency':'USD'},{'venue':'FRA'},{'symbol':'OTHER'}):
   m=dict(self.meta(),**fields);self.assertIsNone(dq.market_closed_proof(m,'2026-10-26 08:30','2026-10-26 09:15',self.now))
 def test_partial_boundary_and_other_date_not_closed(self):
  self.install()
  for a,b in [('2026-10-26 09:45','2026-10-26 10:00'),('2026-10-27 08:30','2026-10-27 09:15'),('2026-10-26 09:55','2026-10-26 10:00')]:
   self.assertIsNone(dq.market_closed_proof(self.meta(),a,b,self.now))
 def test_positive_and_zero_publications_remain_blocked(self):
  self.install()
  for field in ('trade_intervals','zero_quantity_intervals'):
   self.p[field]=[['2026-10-26 08:30','2026-10-26 08:35']]
   r=dq.interval_status(self.meta(),'2026-10-26 08:30','2026-10-26 08:35',self.now)
   self.assertEqual(r['state'],'UNCONFIRMED')
   self.assertEqual(len(dq.covered_empty(self.meta(),pd.to_datetime(['2026-10-26 08:30']),'5m',self.now)),0)
   self.p[field]=[]
 def test_existing_bar_during_halt_is_retained_and_invalid(self):
  self.install();f=self.frame(['2026-10-26 08:30']);before=f.copy()
  r=dq.audit(f,'5m',now=self.now,check_stale=False)
  self.assertEqual(r['state'],'INVALID');self.assertTrue(any('EXCHANGE_NOTICE_CONFLICT' in x for x in r['issues']));pd.testing.assert_frame_equal(f,before)
 def test_real_gap_after_halt_remains_unconfirmed(self):
  self.install();f=self.frame(['2026-10-26 09:55','2026-10-26 10:05'])
  r=dq.audit(f,'5m',now=self.now,check_stale=False);self.assertEqual(r['state'],'UNCONFIRMED');self.assertEqual(r['missing_count'],1)
 def test_existing_freshness_is_not_waived(self):
  self.install();r=dq.audit(self.frame(['2026-10-26 09:55']),'5m',now=self.now)
  self.assertEqual(r['state'],'STALE');self.assertGreater(r['stale_count'],0)
 def test_aggregate_keeps_session_anchor_real_rows_only(self):
  self.install();stamps=pd.date_range('2026-10-26 09:55','2026-10-26 11:25',freq='5min')
  self.p['no_trades']=[['2026-10-26 07:30','2026-10-26 07:35'],['2026-10-26 09:50','2026-10-26 09:55']]
  f=self.frame(stamps);out=aggregate(f,'5m','4H',now=self.now)
  self.assertEqual(list(out.index),[pd.Timestamp('2026-10-26 07:30')]);self.assertTrue(bool(out.iloc[0].bar_complete))
  self.assertEqual(out.iloc[0].volume,10*len(stamps));self.assertEqual(len(f),len(stamps))
 def test_no_listing_or_benchmark_or_changed_identity_cannot_install(self):
  with self.s.connect() as d:d.execute('DELETE FROM candles')
  self.assertFalse(self.install())
  self.assertFalse(gh.attach_known_interruptions(dict(self.i,kind='benchmark'),self.s,self.now))
  with self.assertRaisesRegex(ValueError,'instrument changed'):gh.attach_known_interruptions(dict(self.i,name='Changed'),self.s,self.now)
 def test_notice_and_check_time_tampering_rejected(self):
  self.install();original=copy.deepcopy(self.p)
  self.p['exchange_closures'][0]['end']='2026-10-26 23:00:00';self.assertIsNone(dq.market_closed_proof(self.meta(),'2026-10-26 08:30','2026-10-26 08:35',self.now))
  self.p=original;self.p['closure_checked_at']='2027-01-01T00:00Z';self.assertIsNone(dq.market_closed_proof(self.meta(),'2026-10-26 08:30','2026-10-26 08:35',self.now))
 def test_cp_natural_turn_resolves_notice_and_requests_existing_analysis(self):
  spec=rv._spec(self.i['isin'],self.m,'5m','2026-10-26 08:30','2026-10-26 09:10')
  with self.s.connect() as d:rv._enqueue(d,spec,{'kind':'fixture'},self.now)
  r=rv.turn(self.s,self.i,self.now)
  with self.s.connect() as d:j=rv.read(d,rv.key(spec))
  self.assertEqual(j['status'],'RESOLVED_NATIVE');self.assertEqual(j['finding']['state'],'MARKET_CLOSED');self.assertTrue(r['analysis_requested_at'])
  self.s.read_cache.close();self.s=Store(self.temp.name);self.assertFalse(self.install())
 def test_notice_invalidates_only_overlapping_review_inputs(self):
  specs=[rv._spec(self.i['isin'],self.m,'5m',at,at) for at in ('2026-10-14 17:45','2026-10-26 08:30')]
  with self.s.connect() as d:before=[rv._input(d,s)['fingerprint'] for s in specs]
  self.install()
  with self.s.connect() as d:after=[rv._input(d,s)['fingerprint'] for s in specs]
  self.assertEqual(before[0],after[0]);self.assertNotEqual(before[1],after[1])
 def test_closure_revision_revokes_cached_market_assessment(self):
  with self.s.connect() as d:before=dict(d.execute('SELECT scope,revision FROM input_revisions'))
  self.install()
  from performance_cache import InputRevisionChanged
  with self.assertRaises(InputRevisionChanged):self.s.read_cache.assert_current(before)
 def test_minute_notice_boundary_rows_and_unknown_slots_not_reinterpreted(self):
  self.install()
  for stamp in ('2026-10-26 07:30','2026-10-26 09:50'):
   r=dq.audit(self.frame([stamp]),'5m',now=self.now,check_stale=False)
   self.assertEqual(r['state'],'READY')
   self.assertIsNone(dq.market_closed_proof(self.meta(),stamp,pd.Timestamp(stamp)+pd.Timedelta(minutes=5),self.now))

if __name__=='__main__':unittest.main()
