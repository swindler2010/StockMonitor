"""New primary venue policy, preserving reserves and actual source boundaries."""
import pathlib,sys,unittest,copy,tempfile
from types import SimpleNamespace as NS
from unittest.mock import patch
import pandas as pd
R=pathlib.Path(__file__).parent
sys.path.insert(0,str(R/'stock_monitor'))
sys.path.insert(0,str(R/'history64-evidence/gettex-primary29/candidate'))
import timeframes,gettex_chart as chart
from desktop014 import Desktop014
from storage import Store
import data_quality as dq
import test_proxy_session28 as fixture

class PrimaryTests(unittest.TestCase):
 def setUp(self):
  self.f=fixture.ProxySession('runTest');self.f.setUp();self.addCleanup(self.f.tearDown)
  self.s,self.info,self.ctx=self.f.s,self.f.info,self.f.ctx;self.now=self.f.now
 def read(self,tf,**kwargs):return Desktop014._frame_uncached(self.ctx,self.info,tf,**kwargs)
 def deep_reserve(self):
  for b in ('5m','15m','1H','1D'):
   idx=dq.expected_index(self.f.p,b,'2026-09-01','2026-10-27',self.now)
   frame=pd.DataFrame(dict(open=900.,high=902.,low=899.,close=901.,volume=100.),index=idx)
   frame.attrs=dict(self.f.p);self.s.save_series(self.info,frame,b)
 def table_rows(self):
  with self.s.connect() as db:return {t:db.execute('SELECT * FROM '+t+' ORDER BY 1,2').fetchall() for t in ('series','candles','instruments')}
 def chart5(self):
  meta=chart.identity(self.info['isin'],'SAPG.GTX');raw=self.f.make(meta,'5m',200)
  self.s.save_series(self.info,raw,'5m');self.s.set_setting(chart.state_key(self.info['isin'],'5m'),dict(state='SAVED',identity=meta,listing_proof=dict(meta,status='CONFIRMED',isin=self.info['isin'])))
  return raw,meta
 def test_primary_all_eleven_tfs_preserves_deeper_reserve_without_mixing(self):
  self.deep_reserve();before=self.table_rows()
  for tf in timeframes.LABELS:
   with self.subTest(tf=tf):
    out=self.read(tf);self.assertFalse(out.empty)
    self.assertEqual(out.attrs['venue'],'GETTEX');self.assertEqual(out.attrs['currency'],'EUR')
    self.assertTrue(out.close.eq(101).all());self.assertFalse(out.close.eq(901).any())
  self.assertEqual(before,self.table_rows())
 def test_official_five_minute_fallback_is_used_without_inventing_children(self):
  with self.s.connect() as db:db.execute("DELETE FROM candles WHERE base_tf='15m'")
  for tf in ('15m','30m','1H','2H','3H','4H','1D'):
   with self.subTest(tf=tf):
    out=self.read(tf);self.assertEqual(out.attrs['source'],'gettex');self.assertTrue(out.close.eq(101).all())
    self.assertEqual(out.attrs['aggregation_base'],'5m')
  self.assertEqual(len(self.read('1D')),2)
 def test_chart_five_minute_fallback_uses_same_provider_for_all_derived_views(self):
  raw,meta=self.chart5()
  with patch.object(self.s,'load_official',return_value=pd.DataFrame()):
   for tf in ('15m','30m','1H','2H','3H','4H','1D'):
    with self.subTest(tf=tf):
     out=self.read(tf);self.assertEqual(out.attrs['source'],'gettex_chart');self.assertTrue(out.close.eq(201).all())
     self.assertEqual(out.attrs['aggregation_base'],'5m')
  self.assertEqual(timeframes.base_for('4H','gettex_chart'),'15m')
  self.assertEqual(timeframes.base_for('1D','gettex_chart'),'1D')
 def test_five_minute_daily_progress_survives_reopen_and_invalidates(self):
  raw,meta=self.chart5();a=chart.prepare_daily(self.s,self.info,self.now)
  self.assertEqual(a['base'],'5m');self.assertEqual(a['complete_closed'],2)
  reopened=Store(self.f.tmp.name);self.addCleanup(reopened.read_cache.close)
  with patch.object(chart,'derived_daily',side_effect=AssertionError('repeat work')):
   self.assertTrue(chart.prepare_daily(reopened,self.info,self.now)['skipped_unchanged'])
  with self.s.connect() as db:db.execute("UPDATE series SET volume=volume+7 WHERE source='gettex_chart' AND ts=?",(str(raw.index[0]),))
  b=chart.prepare_daily(reopened,self.info,self.now)
  self.assertNotEqual(a['input_revisions'],b['input_revisions'])
  self.assertEqual(chart.load(reopened,self.info,'1D',self.now).volume.iloc[0],raw.loc['2026-10-26'].volume.sum()+7)
 def test_deep_same_gettex_daily_archive_not_shortened_by_intraday(self):
  raw,meta=self.chart5()
  idx=dq.expected_index(meta,'1D','2026-09-01','2026-10-27',self.now)
  daily=pd.DataFrame(dict(open=200.,high=202.,low=199.,close=201.,volume=100.),index=idx);daily.attrs=dict(meta)
  self.s.save_series(self.info,daily,'1D');self.s.set_setting(chart.state_key(self.info['isin'],'1D'),self.s.setting(chart.state_key(self.info['isin'],'5m')))
  for tf in ('1D','1W','1M'):
   out=self.read(tf);self.assertEqual(out.attrs['source'],'gettex_chart');self.assertTrue(out.close.eq(201).all());self.assertLess(out.index[0],pd.Timestamp('2026-10-01'))
  self.assertEqual(len(self.s.load_series(self.info['isin'],'1D','gettex_chart')),len(daily))
 def test_wrong_identity_and_invalid_chart_do_not_replace_reserve(self):
  raw,meta=self.chart5();key=chart.state_key(self.info['isin'],'5m');saved=self.s.setting(key)
  with patch.object(self.s,'load_official',return_value=pd.DataFrame()):
   for change in ({'currency':'USD'},{'venue':'FRA'},{'symbol':'SAP.DE'}):
    self.s.set_setting(key,dict(saved,identity=dict(meta,**change)))
    self.assertEqual(self.read('15m').attrs['source'],'proxy')
   self.s.set_setting(key,saved)
   with self.s.connect() as db:db.execute("UPDATE series SET high=1 WHERE source='gettex_chart'")
   self.assertEqual(self.read('15m').attrs['source'],'proxy')
 def test_gap_stale_and_forming_aggregate_never_become_ready(self):
  raw,meta=self.chart5()
  with self.s.connect() as db:db.execute("DELETE FROM series WHERE source='gettex_chart' AND ts=?",(str(raw.index[3]),))
  day=chart.load(self.s,self.info,'1D',self.now)
  self.assertFalse(bool(day.iloc[0].bar_complete));self.assertNotEqual(day.attrs['data_quality']['state'],'READY')
  early=chart.load(self.s,self.info,'1D',self.now-pd.Timedelta(hours=3))
  self.assertGreater(timeframes.endpoints(early)[-1],self.now-pd.Timedelta(hours=3))
  self.assertEqual(chart.prepare_daily(self.s,self.info,self.now-pd.Timedelta(hours=3))['complete_closed'],0)
  self.ctx.analysis_at=self.now+pd.Timedelta(days=2)
  self.assertNotEqual(self.read('5m').attrs['data_quality']['state'],'READY')
 def test_explicit_foreign_daily_pin_and_revision_protection_stay_intact(self):
  self.deep_reserve()
  with self.s.read_cache.track_inputs() as token:out=self.read('1D',required_identity=self.f.p)
  self.assertEqual(out.attrs['source'],'proxy');self.assertTrue(out.close.eq(901).all())
  self.assertNotIn('source_selection',out.attrs)
  with self.assertRaises(ValueError):self.read('1D',required_identity=dict(self.f.p,currency='USD'))
  tail=self.s.load_series(self.info['isin'],'1D','proxy','SAP.DE','GER','EUR').tail(1).copy();tail['volume']=101.
  self.s.save_series(self.info,tail,'1D');self.assertFalse(self.s.read_cache.current(token))

if __name__=='__main__':unittest.main(verbosity=2)
