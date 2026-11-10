import copy,unittest,contextlib
from unittest.mock import patch
import pandas as pd
import data_quality as dq,partial_chart as p
from analytics import resample
NOW=pd.Timestamp('2026-11-02T22:50Z');ISIN='US0378331005'
META=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
INFO=dict(isin=ISIN,name='Apple',kind='stock')
def frame(index,base='1D',meta=META):
 f=pd.DataFrame([[100,103,99,101,20]]*len(index),index=pd.DatetimeIndex(index),columns=dq.FIELDS)
 f.attrs=dict(meta,timeframe=base,listing_proof=dict(meta,isin=ISIN,status='CONFIRMED'))
 f.attrs['data_quality']=dq.audit(f,base,expected=meta,now=NOW)
 return f
def daily(start,end):return frame(dq.expected_index(META,'1D',pd.Timestamp(start),pd.Timestamp(end),NOW))
class Memory:
 def setting(self,key,default=None):return default
 def load_official(self,*args):return pd.DataFrame()
 def load_series(self,*args):return pd.DataFrame()
 def set_setting(self,*args):raise AssertionError('Display must not write')
class DisplayDepth(unittest.TestCase):
 def choose(self,normal,raw,tf='1D',info=INFO):
  with patch('gettex_chart._load_native',side_effect=lambda s,i,b:raw.get(b,pd.DataFrame())),patch('onvista_archive.available_daily',return_value=[]):
   return p.select_display(info,tf,normal,Memory(),NOW)
 def partial(self,raw,bad):
  damaged=raw.copy();damaged.loc[bad,'high']=1
  report=dq.audit(damaged,raw.attrs['timeframe'],expected=raw.attrs,now=NOW)
  saved=raw.drop(bad).copy();saved.attrs.update(archive_invalid=dict(report,rejected_timestamps=[str(bad)]),data_quality=report,
   partial_chart={'display_only':True,'excluded_rows':1,'raw_count':len(raw)})
  return saved
 def test_long_partial_daily_wins_over_short_valid_without_clearing_invalid(self):
  full=daily('2019-01-02','2019-04-30');raw=self.partial(full,full.index[20]);old=copy.deepcopy(raw)
  normal=full.iloc[-10:].copy();out=self.choose(normal,{'1D':raw})
  self.assertEqual(len(out),len(raw));self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
  self.assertTrue(out.attrs['partial_chart']['break_before']);pd.testing.assert_frame_equal(raw,old)
  self.assertEqual(p.bind({},out)['trading_engine']['status'],'DATA BLOCKED')
 def test_old_weeks_and_months_use_derived_provenance_and_exclude_bad_period(self):
  full=daily('2019-01-02','2019-06-28');raw=self.partial(full,pd.Timestamp('2019-03-12'))
  for tf,bad in (('1W',pd.Timestamp('2019-03-15')),('1M',pd.Timestamp('2019-03-01'))):
   out=self.choose(resample(full.iloc[-20:],'1D',tf,NOW),{'1D':raw},tf)
   self.assertEqual(out.attrs['source'],'gettex_chart');self.assertNotIn(bad,out.index)
   self.assertGreater(len(out),1);self.assertTrue(out.bar_complete.all());self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
   self.assertEqual(out.attrs['aggregation_base'],'1D');self.assertTrue(out.attrs['bar_ends'])
 def test_unproven_historical_aggregate_still_rejected(self):
  with self.assertRaisesRegex(ValueError,'provenance'):
   dq.expected_index(META,'1W',pd.Timestamp('2019-01-04'),pd.Timestamp('2019-03-29'),NOW)
 def test_confirmed_gettex_wins_over_longer_separate_reserve(self):
  full=daily('2026-01-02','2026-10-30');reserve=full.copy();m=dict(META,source='proxy',symbol='APC.F',venue='FRA')
  reserve.attrs.update(m,listing_proof=dict(m,isin=ISIN,status='CONFIRMED'))
  out=self.choose(reserve,{'1D':full.iloc[-20:].copy()})
  self.assertEqual(out.attrs['venue'],'GETTEX');self.assertEqual(len(out),20)
 def test_wrong_currency_or_isin_or_venue_never_wins(self):
  short=daily('2026-10-01','2026-10-30');full=daily('2026-01-02','2026-10-30')
  for field,value in (('currency','USD'),('venue','GER'),('isin','US0000000000')):
   bad=full.copy()
   if field=='isin':bad.attrs['listing_proof']={**bad.attrs['listing_proof'],'isin':value}
   else:bad.attrs[field]=value
   out=self.choose(short,{'1D':bad});self.assertEqual(len(out),len(short));self.assertEqual(out.attrs['currency'],'EUR')
 def test_longest_requested_tf_can_come_from_five_minutes(self):
  raw=frame(pd.date_range('2026-10-30 07:30','2026-10-30 22:55',freq='5min'),'5m')
  short=resample(raw,'5m','15m',NOW).iloc[-3:].copy()
  out=self.choose(short,{'5m':raw,'15m':short},'15m')
  self.assertEqual(len(out),62);self.assertEqual(out.attrs['aggregation_base'],'5m')
  self.assertEqual(out.volume.iloc[0],60)
 def test_incomplete_children_do_not_become_complete_aggregate(self):
  raw=frame(pd.date_range('2026-10-30 07:30','2026-10-30 22:55',freq='5min'),'5m').drop(pd.Timestamp('2026-10-30 08:05'))
  out=self.choose(pd.DataFrame(),{'5m':raw},'30m')
  self.assertNotIn(pd.Timestamp('2026-10-30 08:00'),out.index)
  self.assertNotEqual(out.attrs['data_quality']['state'],'READY');self.assertTrue(out.attrs['partial_chart'])

 def test_max_preserves_older_verified_periods_over_denser_recent_archive(self):
  older=daily('2026-01-02','2026-10-30').iloc[[0,-1]].copy()
  older.attrs['data_quality']=dq.audit(older,'1D',expected=META,now=NOW)
  recent=daily('2026-10-01','2026-10-30')
  self.assertGreater(len(recent),len(older))
  out=self.choose(recent,{'1D':older})
  self.assertEqual(out.index[0],older.index[0]);self.assertEqual(len(out),2)
  self.assertTrue(out.attrs['partial_chart']['break_before'])
  self.assertNotEqual(out.attrs['data_quality']['state'],'READY')
  self.assertEqual(p.bind({},out)['trading_engine']['status'],'DATA BLOCKED')
 def test_unlocalized_invalid_is_not_displayed_as_valid(self):
  raw=daily('2026-01-02','2026-10-30');raw.index=raw.index[::-1]
  raw.attrs['data_quality']=dq.audit(raw,'1D',now=NOW)
  out=self.choose(raw,{'1D':raw});self.assertTrue(out.empty);self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
 def test_max_does_not_shorten_selected_archive(self):
  from chart_periods import visible_frame
  raw=daily('2026-01-02','2026-10-30');out=self.choose(raw.iloc[-5:].copy(),{'1D':raw})
  self.assertEqual(len(visible_frame(out,'Max')),len(raw))
 def test_mcp_partial_indicator_values_are_not_filled_across_holes(self):
  raw=daily('2026-01-02','2026-10-30');out=self.partial(raw,raw.index[80]);enriched=p.bind({},out)['analysis'][0]
  self.assertTrue(enriched[['ema10','ema30','rsi','atr','macd','macd_signal','volume_mean20']].isna().all().all())

 def test_no_unconfirmed_or_dollar_fallback_when_no_candidates(self):
  raw=daily('2026-10-01','2026-10-30');raw.attrs['currency']='USD'
  self.assertTrue(self.choose(raw,{}).empty)

 def test_persisted_global_invalid_is_not_hidden_by_localized_damage(self):
  full=daily('2026-10-01','2026-10-30');raw=self.partial(full,full.index[4])
  global_bad={'state':'INVALID','repairable':False,'identity':META,'issues':['Identity evidence conflict']}
  with patch.object(Memory,'setting',return_value=global_bad):
   self.assertTrue(self.choose(pd.DataFrame(),{'1D':raw}).empty)

 def test_source_outage_retains_verified_saved_rows_and_trade_block(self):
  full=daily('2026-01-02','2026-10-30');raw=self.partial(full,full.index[4]);normal=full.iloc[-5:].copy()
  normal.attrs.update(source='onvista',symbol='123',listing_proof={**normal.attrs['listing_proof'],'source':'onvista','symbol':'123'})
  report={'state':'SOURCE_UNAVAILABLE','repairable':True,'identity':META,'issues':['Recovery source unavailable']}
  with patch.object(Memory,'setting',return_value=report):
   out=self.choose(normal,{'1D':raw})
   self.assertEqual(len(out),len(raw));self.assertEqual(out.attrs['data_quality']['state'],'SOURCE_UNAVAILABLE')
   self.assertTrue(out.attrs['archive_invalid']['rejected_timestamps'])
   self.assertEqual(p.bind({},out)['trading_engine']['status'],'DATA BLOCKED')

 def test_visible_gap_metadata_satisfies_existing_windows_binding(self):
  raw=frame(pd.date_range('2026-10-30 07:30','2026-10-30 09:00',freq='5min'),'5m').drop(pd.Timestamp('2026-10-30 08:05'))
  out=self.choose(pd.DataFrame(),{'5m':raw},'5m');part=out.attrs['partial_chart']
  self.assertEqual(part['segments'],2);self.assertEqual(part['excluded_rows'],0);self.assertEqual(part['raw_count'],len(raw))
  self.assertEqual(out.attrs['data_quality']['state'],'GAP')

 def test_source_outage_without_damaged_rows_has_complete_display_metadata(self):
  raw=daily('2026-01-02','2026-10-30');report={'state':'SOURCE_UNAVAILABLE','identity':META,'issues':['Offline']}
  with patch.object(Memory,'setting',return_value=report):out=self.choose(pd.DataFrame(),{'1D':raw})
  self.assertEqual(len(out),len(raw));part=out.attrs['partial_chart']
  self.assertEqual(part['segments'],1);self.assertEqual(part['excluded_rows'],0);self.assertEqual(part['raw_count'],len(raw))

 def test_long_partial_render_keeps_all_breaks_with_bounded_artist_count(self):
  from matplotlib.figure import Figure
  from chart_modes import draw_prices
  import numpy as np
  raw=frame(pd.date_range('2026-10-30 07:30',periods=2000,freq='5min'),'5m')
  raw.attrs['partial_chart']={'display_only':True,'break_before':[str(x) for x in raw.index[1:818]]}
  ax=Figure().subplots();draw_prices(ax,raw,'Line')
  self.assertEqual(len(ax.collections),2);self.assertEqual(len(ax.collections[1].get_segments()),817)
  self.assertEqual(np.isnan(ax.lines[0].get_xdata()).sum(),817)
  self.assertEqual(len(ax.patches),0)

 def cached_store(self):
  from performance_cache import ReadCache
  s=Memory();s.read_cache=ReadCache(':memory:');s.read_cache.db.execute('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER)')
  self.addCleanup(s.read_cache.close);return s

 def test_cache_53_and_200_jobs_bounded_with_actual_selected_frames(self):
  from performance_cache import InputRevisionChanged
  s=self.cached_store();raw=daily('2026-10-01','2026-10-30');calls=[]
  def native(store,info,base):
   if base!='1D':return pd.DataFrame()
   calls.append(info['isin']);store.read_cache.depend('market:'+info['isin']+':1D')
   f=raw.copy();f.attrs['listing_proof']={**f.attrs['listing_proof'],'isin':info['isin']};return f
  with patch('gettex_chart._load_native',side_effect=native),patch('onvista_archive.available_daily',return_value=[]),patch.object(p,'load',return_value=None):
   for n in range(200):
    info={**INFO,'isin':'US'+str(n).zfill(10)}
    out=p.select_display(info,'1D',pd.DataFrame(),s,NOW)
    self.assertEqual(len(out),len(raw))
    again=p.select_display(info,'1D',pd.DataFrame(),s,NOW)
    self.assertEqual(len(again),len(raw));self.assertEqual(len(calls),n+1)
    if n in (52,199):self.assertLessEqual(len(s.read_cache.items),64)

 def test_relevant_revision_invalidates_display_without_settings_or_writes(self):
  s=self.cached_store();raw=daily('2026-10-01','2026-10-30');source=[raw];scope='market:'+ISIN+':1D'
  def native(store,info,base):
   if base!='1D':return pd.DataFrame()
   store.read_cache.depend(scope);return source[0]
  with patch('gettex_chart._load_native',side_effect=native),patch('onvista_archive.available_daily',return_value=[]),patch.object(p,'load',return_value=None):
   before=p.select_display(INFO,'1D',pd.DataFrame(),s,NOW)
   source[0]=raw.iloc[2:].copy();s.read_cache.db.execute('INSERT INTO input_revisions VALUES(?,1)',(scope,));s.read_cache.db.commit()
   after=p.select_display(INFO,'1D',pd.DataFrame(),s,NOW)
   self.assertEqual(len(after),len(before)-2)

 def test_revision_during_projection_rejected_by_existing_publication_guard(self):
  from performance_cache import InputRevisionChanged
  s=self.cached_store();raw=daily('2026-10-01','2026-10-30');scope='market:'+ISIN+':1D'
  def native(store,info,base):
   if base!='1D':return pd.DataFrame()
   store.read_cache.depend(scope);return raw
  original=p.quality.expected_index;changed=[False]
  def grid(*args,**kwargs):
   out=original(*args,**kwargs)
   if not changed[0]:
    changed[0]=True;s.read_cache.db.execute('INSERT INTO input_revisions VALUES(?,1)',(scope,));s.read_cache.db.commit()
   return out
  with patch('gettex_chart._load_native',side_effect=native),patch('onvista_archive.available_daily',return_value=[]),patch.object(p,'load',return_value=None),patch.object(p.quality,'expected_index',side_effect=grid):
   with s.read_cache.track_inputs() as token:p.select_display(INFO,'1D',pd.DataFrame(),s,NOW)
   with self.assertRaises(InputRevisionChanged):s.read_cache.assert_current(token)
   self.assertFalse(s.read_cache.items)

 def test_chart_and_mcp_share_partial_projection_without_core_or_writes(self):
  import chart_worker,analytical_read
  s=self.cached_store();raw=daily('2026-10-01','2026-10-30');raw=self.partial(raw,raw.index[4])
  normal=raw.iloc[-3:].copy();normal.attrs.pop('partial_chart');normal.attrs.pop('archive_invalid')
  values=dict(watchlist=[INFO],symbol='Apple',timeframe='1D',period='Max',request={'isin':ISIN,'timeframe':'1D','limit':3},drafts={})
  with patch('snapshot_worker.SnapshotContext.user_state',return_value={}),patch('snapshot_worker.SnapshotContext.frame_for',return_value=normal),patch('snapshot_worker.SnapshotContext.trading_analysis',side_effect=AssertionError('CORE must remain separate')),patch('gettex_chart._load_native',side_effect=lambda s,i,b:raw if b=='1D' else pd.DataFrame()),patch('onvista_archive.available_daily',return_value=[]),patch('paper_account.instrument_allowed',return_value=False),patch('candle_timing.annotate_page',return_value=0):
   chart=chart_worker.prepare(s,values);page=analytical_read.calculate(s,values)
   self.assertEqual(len(chart['frame']),page['stored_frame_count']);self.assertEqual(page['identity']['source'],'gettex_chart')
   self.assertTrue(page['indicator_basis']['display_only']);self.assertEqual(page['data_quality']['state'],'INVALID')
   self.assertTrue(all(b['ema10'] is None for b in page['bars']));self.assertIsNone(page['analysis_context'])

 def test_unsaved_venue_lock_is_honored_by_chart_mcp_and_cache(self):
  import chart_worker,analytical_read
  s=self.cached_store();raw=daily('2026-10-01','2026-10-30')
  meta=dict(META,source='proxy',symbol='APC.F',venue='FRA')
  raw.attrs.update(meta,listing_proof=dict(meta,isin=ISIN,status='CONFIRMED'))
  values=dict(watchlist=[INFO],symbol='Apple',timeframe='1D',period='Max',request={'isin':ISIN,'timeframe':'1D','limit':1},drafts={})
  with patch('snapshot_worker.SnapshotContext.user_state',return_value={'history_venue_lock':'GETTEX'}),patch('snapshot_worker.SnapshotContext.frame_for',return_value=raw),patch('snapshot_worker.SnapshotContext.trading_analysis',side_effect=AssertionError('CORE unchanged')),patch('gettex_chart._load_native',return_value=pd.DataFrame()),patch('onvista_archive.available_daily',return_value=[]),patch('paper_account.instrument_allowed',return_value=False),patch('candle_timing.annotate_page',return_value=0):
   self.assertFalse(p.select_display(INFO,'1D',raw,s,NOW).empty)
   self.assertTrue(p.select_display({**INFO,'history_venue_lock':'GETTEX'},'1D',raw,s,NOW).empty)
   self.assertTrue(chart_worker.prepare(s,values)['frame'].empty)
   self.assertEqual(analytical_read.calculate(s,values)['stored_frame_count'],0)

if __name__=='__main__':unittest.main()
