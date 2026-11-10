"""Accepted TV absence reaches aggregation/display; never a market-data rewrite."""
import copy,json,sys,unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_controls as ctl
import data_quality as dq
from storage import Store
from analytics import resample
from partial_chart import select_display
from timeframes import accepted_display_slots

ARCHIVE=Path(__file__).resolve().parents[1].parent/'СМ-Архив'
TEST_DB=ARCHIVE/'TV принятые интервалы тест.sqlite3'

class FixtureStore(Store):
 @contextmanager
 def connect(self):
  self.path=TEST_DB
  with super().connect() as db:yield db

class AcceptedDisplay(unittest.TestCase):
 def setUp(self):
  self.assertFalse(TEST_DB.exists(),'Do not overwrite a previous fixture')
  self.s=FixtureStore(ARCHIVE)
  self.i=dict(isin='US0378331005',name='Apple')
  self.s.save_instrument(self.i)
  self.meta=dict(source='gettex',symbol=self.i['isin'],venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.now=pd.Timestamp('2026-11-02T20:00Z');self.missing=pd.Timestamp('2026-10-28 12:15')
  self.s.set_setting(ctl.TV_POLICY,dict(enabled=True,approved=True,version=ctl.TV_VERSION))
 def tearDown(self):
  self.s.read_cache.close()
  for suffix in ('','-wal','-shm'):
   path=Path(str(TEST_DB)+suffix)
   if path.exists():path.unlink()
 def prepare(self,other=None,granted=None,isin=None):
  if isin:
   self.i=dict(isin=isin,name='Newly registered stock');self.meta['symbol']=isin;self.s.save_instrument(self.i)
  gaps=list(granted or [self.missing]);gaps+=list(other or [])
  idx=dq.expected_index(self.meta,'15m','2026-10-01','2026-11-02 20:45',self.now)
  f=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=10.),index=idx).drop(gaps)
  f.attrs={**self.meta,'timeframe':'15m','classify_absence':True}
  self.s.save_frame(self.i['isin'],'gettex','15m',f)
  f['source']='gettex';f.attrs['data_quality']=dq.audit(f,'15m',now=self.now)
  self.s.set_setting(dq.key(self.i,self.meta['symbol'],'15m'),f.attrs['data_quality'])
  with self.s.connect() as db:
   scopes=['market:'+self.i['isin']+':15m','instrument:'+self.i['isin'],'setting:'+ctl.TV_POLICY]
   token=ctl._revisions(db,scopes);entries={}
   # Current receipts are the issuer's already-tested contract. No TV access.
   for n,t in enumerate(granted or [self.missing]):
    p=dict(isin=self.i['isin'],kind='archive',tf='15m',identity=self.meta.copy(),state='UNCONFIRMED',range=[str(t),str(t)])
    entries[str(n)]=dict(actor=ctl.TV_ACTOR,problem=p,input_revisions=token,
      evidence=dict(status='OWNER_ACCEPTED_TV_ABSENCE',price_model=ctl.TV_PRICE_MODEL))
   ctl.write(db,'cp_manual_risk:'+self.i['isin'],dict(account='five-eur-10000-v1',accepted=entries))
  self.raw=f
  return f
 def native(self):return ctl.accepted_gap_input(self.s,self.i,self.raw,'15m',self.now)
 def derived(self,tf='4H'):
  return ctl.accepted_gap_output(resample(self.native(),'15m',tf,self.now),tf,self.now)
 def display(self,tf):
  normal=resample(self.native(),'15m',tf,self.now)
  return select_display(self.i,tf,normal,self.s,self.now)
 def test_real_4h_bucket_retained_without_ohlcv_invention(self):
  f=self.prepare();before=f.copy(deep=True)
  self.assertNotIn(pd.Timestamp('2026-10-28 11:30'),resample(f,'15m','4H',self.now).index)
  out=self.derived();row=out.loc['2026-10-28 11:30'];children=f.loc['2026-10-28 11:30':'2026-10-28 15:15']
  self.assertEqual(row.volume,children.volume.sum());self.assertEqual(row.close,children.close.iloc[-1])
  self.assertTrue(row.bar_complete);pd.testing.assert_frame_equal(f,before)
  self.assertNotIn(self.missing,out.index)
  self.assertEqual(out.attrs['data_quality']['state'],'UNCONFIRMED')
  self.assertTrue(out.attrs['accepted_risk']['tv_complete_aggregate'])
  self.assertTrue(ctl.analysis_quality_allowed(out.attrs['data_quality'],out.attrs['accepted_risk']))
  self.assertFalse(ctl.analysis_quality_allowed(out.attrs['data_quality']))
 def test_unaccepted_neighbour_keeps_parent_excluded_and_analysis_blocked(self):
  self.prepare(other=[pd.Timestamp('2026-10-28 13:30')]);out=self.derived()
  self.assertNotIn(pd.Timestamp('2026-10-28 11:30'),out.index)
  self.assertNotIn('accepted_risk',out.attrs)
  display=self.display('4H');self.assertIn('2026-10-28 15:30:00',display.attrs['partial_chart']['break_before'])
 def test_native_chart_has_no_band_for_accepted_absence(self):
  self.prepare();out=self.display('15m')
  self.assertNotIn('2026-10-28 12:30:00',out.attrs.get('partial_chart',{}).get('break_before',[]))
  self.assertNotIn(self.missing,out.index)
 def test_chart_keeps_only_unknown_gap_band(self):
  self.prepare(other=[pd.Timestamp('2026-10-29 12:15')]);out=self.display('15m')
  breaks=out.attrs['partial_chart']['break_before']
  self.assertNotIn('2026-10-28 12:30:00',breaks);self.assertIn('2026-10-29 12:30:00',breaks)
 def test_daily_weekly_monthly_keep_real_sums_and_factual_dq(self):
  self.prepare()
  for tf,stamp in [('1D','2026-10-28'),('1W','2026-10-30'),('1M','2026-10-01')]:
   with self.subTest(tf=tf):
    out=resample(self.native(),'15m',tf,self.now)
    self.assertTrue(out.loc[stamp].bar_complete)
    self.assertEqual(out.attrs['daily_provenance']['raw_state'],'UNCONFIRMED')
    self.assertNotEqual(out.attrs['data_quality']['state'],'READY')
 def test_all_empty_parent_is_not_fabricated_or_drawn_as_error(self):
  gaps=list(pd.date_range('2026-10-28 11:30','2026-10-28 15:15',freq='15min'))
  self.prepare(granted=gaps);out=self.display('4H')
  self.assertNotIn(pd.Timestamp('2026-10-28 11:30'),out.index)
  self.assertNotIn('2026-10-28 15:30:00',out.attrs.get('partial_chart',{}).get('break_before',[]))
 def test_policy_or_market_revision_revokes_and_invalidates_cached_chart(self):
  self.prepare();self.display('15m')
  self.s.set_setting(ctl.TV_POLICY,dict(enabled=False))
  out=self.display('15m');self.assertIn('2026-10-28 12:30:00',out.attrs['partial_chart']['break_before'])
  self.assertNotIn('tv_accepted_absence',self.native().attrs)
 def test_changed_ohlcv_revokes_aggregation_before_cp_runs(self):
  self.prepare()
  with self.s.connect() as db:db.execute('UPDATE candles SET close=100.1 WHERE ts=?',(str(self.raw.index[-1]),))
  self.assertNotIn('tv_accepted_absence',self.native().attrs)
  self.assertNotIn(pd.Timestamp('2026-10-28 11:30'),self.derived().index)
 def test_wrong_venue_currency_or_tf_cannot_borrow_receipt(self):
  self.prepare();f=self.native()
  for change in ({'venue':'FRA'},{'currency':'USD'},{'source':'gettex_chart'}):
   m={**f.attrs,**change}
   self.assertEqual(len(ctl.tv_absent_slots(m,[self.missing],'15m',self.now)),0)
  self.assertEqual(len(ctl.tv_absent_slots(f.attrs,[self.missing],'5m',self.now)),0)
 def test_future_or_unreviewed_slots_never_covered(self):
  self.prepare();f=self.native()
  self.assertEqual(len(ctl.tv_absent_slots(f.attrs,[self.missing],'15m',pd.Timestamp('2026-10-28T11:20Z'))),0)
  self.assertEqual(len(ctl.tv_absent_slots(f.attrs,[self.missing+pd.Timedelta(minutes=15)],'15m',self.now)),0)
 def test_new_registered_stock_uses_same_policy(self):
  self.prepare(isin='DE0005557508');self.assertIn(pd.Timestamp('2026-10-28 11:30'),self.derived().index)
 def test_all_intraday_parents_use_same_accepted_children(self):
  self.prepare()
  for target in ('30m','1H','2H','3H','4H'):
   with self.subTest(tf=target):
    out=self.derived(target)
    rows=out.loc['2026-10-28'];self.assertEqual(rows.volume.sum(),self.raw.loc['2026-10-28'].volume.sum())
    self.assertTrue(rows.bar_complete.all())
  # Same pure kernel for 5m -> 10m; upper-TF receipts cannot certify 5m.
  idx=pd.date_range('2026-10-28 07:30','2026-10-28 22:55',freq='5min')
  f=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=10.),index=idx).drop(self.missing)
  f.attrs=copy.deepcopy(self.native().attrs);f.attrs['timeframe']='5m'
  for key in ('accepted_gap_input','tv_accepted_absence'):f.attrs[key]['base']='5m'
  f.attrs['data_quality']=dq.audit(f,'5m',now=self.now)
  out=resample(f,'5m','10m',self.now)
  self.assertTrue(out.bar_complete.all());self.assertEqual(out.volume.sum(),f.volume.sum())
 def test_changed_receipt_rejects_publication_without_recalculation(self):
  from performance_cache import InputRevisionChanged
  self.prepare()
  with self.s.read_cache.track_inputs() as token:self.native()
  self.s.set_setting('cp_manual_risk:'+self.i['isin'],dict(accepted={}))
  with self.assertRaises(InputRevisionChanged):self.s.read_cache.assert_current(token)
 def test_invalid_and_stale_remain_blocked(self):
  self.prepare();self.raw.iloc[2,self.raw.columns.get_loc('high')]=1
  self.assertNotIn('accepted_risk',self.derived().attrs)
  self.prepare();out=self.derived();risk=out.attrs.get('accepted_risk')
  for state in ('INVALID','STALE','SOURCE_UNAVAILABLE'):
   q={**out.attrs['data_quality'],'state':state};self.assertFalse(ctl.analysis_quality_allowed(q,risk))
  self.assertFalse(ctl.analysis_quality_allowed({**out.attrs['data_quality'],'stale_count':1},risk))

if __name__=='__main__':unittest.main()
