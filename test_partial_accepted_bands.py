"""Partial archives honor current display receipts; all data/gates stay factual."""
import copy,sys,unittest
from pathlib import Path
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import test_cp_reviewed_risk as fixture
import cp_controls as cp
import data_quality as dq
import partial_chart as pc
from chart_modes import draw_prices
from performance_cache import InputRevisionChanged

class PartialAcceptedBands(unittest.TestCase):
 def setUp(self):
  fixture.ReviewedRisk.setUp(self)
  self.now=pd.Timestamp('2026-10-29T23:30Z')
  idx=dq.expected_index(self.m,'15m','2026-10-19','2026-10-29 22:45',self.now)
  f=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=100.),index=idx).drop(pd.Timestamp(self.a))
  f.attrs=copy.deepcopy(self.raw.attrs);self.raw=f
  self.s.save_series(self.i,f,'15m');self.q=dq.audit(f,'15m',now=self.now)
  f.attrs['data_quality']=self.q;self.s.set_setting(self.key,self.q)
  self.p=next(p for p in cp.problems(self.s.db,self.i['isin']) if p['range']==[self.a,self.a])
 tearDown=fixture.ReviewedRisk.tearDown
 def partial(self,raw=None):
  f=(self.raw if raw is None else raw).copy(deep=True)
  f.attrs['data_quality']={**f.attrs['data_quality'],'state':'INVALID','repairable':True}
  f.attrs['archive_invalid']={**f.attrs['data_quality'],'rejected_timestamps':['2026-10-18 23:00:00']}
  f.attrs['partial_chart']={'display_only':True,'excluded_rows':1,'raw_count':len(f)+1}
  return f
 def tv(self,raw=None,base='15m',gaps=None):
  f=self.raw if raw is None else raw
  self.s.set_setting(cp.TV_POLICY,dict(enabled=True,approved=True,version=cp.TV_VERSION))
  scopes=['market:'+self.i['isin']+':'+base,'instrument:'+self.i['isin'],'setting:'+cp.TV_POLICY]
  token=cp._revisions(self.s.db,scopes)
  entries={}
  for n,t in enumerate(gaps or [self.a]):
   entries[str(n)]=dict(actor=cp.TV_ACTOR,input_revisions=token,
    problem=dict(isin=self.i['isin'],kind='archive',tf=base,identity=self.m.copy(),state='UNCONFIRMED',range=[str(t),str(t)]),
    evidence=dict(status='OWNER_ACCEPTED_TV_ABSENCE',price_model=cp.TV_PRICE_MODEL))
  self.s.set_setting('cp_manual_risk:'+self.i['isin'],dict(accepted=entries))
 def project(self,tf='15m',raw=None,base='15m',now=None):
  return pc.project_saved(self.partial(raw),base,tf,now or self.now,self.s,self.i)
 def warnings(self,out):
  p=out.attrs['partial_chart'];return set(p['break_before'])-set(p['accepted_break_before'])
 def test_partial_tv_keeps_factual_invalid_and_no_missing_row_or_database_write(self):
  self.tv();before=list(self.s.db.iterdump());raw=self.raw.copy(deep=True)
  out=self.project();self.assertFalse(self.warnings(out))
  self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
  self.assertNotIn(pd.Timestamp(self.a),out.index)
  self.assertNotIn('accepted_risk',out.attrs)
  self.assertFalse(cp.analysis_quality_allowed(out.attrs['data_quality']))
  pd.testing.assert_frame_equal(raw,self.raw);self.assertEqual(before,list(self.s.db.iterdump()))
 def test_reviewed_discrepancy_hides_band_but_never_certifies_missing_parent(self):
  fixture.ReviewedRisk.authorize(self);fixture.ReviewedRisk.turn(self)
  out=self.project('4H');self.assertFalse(self.warnings(out))
  self.assertNotIn(pd.Timestamp('2026-10-28 11:30'),out.index)
  self.assertNotIn('tv_accepted_absence',out.attrs)
  self.assertTrue(out.attrs['partial_chart']['break_before'])
 def test_tv_restores_real_parent_only_with_exact_real_child_values(self):
  self.tv();out=self.project('4H');children=self.raw.loc['2026-10-28 11:30':'2026-10-28 15:15']
  row=out.loc['2026-10-28 11:30']
  np.testing.assert_array_equal(row[list(dq.FIELDS)],
   [children.open.iloc[0],children.high.max(),children.low.min(),children.close.iloc[-1],children.volume.sum()])
  self.assertTrue(row.bar_complete);self.assertFalse(self.warnings(out))
 def test_new_unaccepted_neighbour_or_rejected_damage_keeps_band(self):
  self.tv();f=self.raw.drop(pd.Timestamp('2026-10-28 13:45'))
  self.assertIn('2026-10-28 15:30:00',self.warnings(self.project('4H',f)))
  f=self.partial();f.attrs['archive_invalid']['rejected_timestamps']=[self.a]
  out=pc.project_saved(f,'15m','15m',self.now,self.s,self.i)
  self.assertIn('2026-10-28 13:45:00',self.warnings(out))
  out=pc.project_saved(f,'15m','4H',self.now,self.s,self.i)
  self.assertNotIn(pd.Timestamp('2026-10-28 11:30'),out.index)
  self.assertIn('2026-10-28 15:30:00',self.warnings(out))
 def test_revocation_and_market_revision_restore_warning_and_reject_publication(self):
  self.tv()
  with self.s.read_cache.track_inputs() as token:self.project()
  self.s.set_setting(cp.TV_POLICY,dict(enabled=False))
  with self.assertRaises(InputRevisionChanged):self.s.read_cache.assert_current(token)
  self.assertTrue(self.warnings(self.project()))
  self.tv()
  with self.s.connect() as db:db.execute('UPDATE series SET close=100.1 WHERE ts=?',(str(self.raw.index[-1]),))
  self.assertTrue(self.warnings(self.project()))
 def test_reviewed_policy_revocation_is_a_cache_dependency(self):
  fixture.ReviewedRisk.authorize(self);fixture.ReviewedRisk.turn(self)
  with self.s.read_cache.track_inputs() as token:self.project()
  policy=self.s.setting(cp.REVIEWED_PREFIX+self.i['isin']);policy['approved']=False
  self.s.set_setting(cp.REVIEWED_PREFIX+self.i['isin'],policy)
  with self.assertRaises(InputRevisionChanged):self.s.read_cache.assert_current(token)
  self.assertTrue(self.warnings(self.project()))
 def test_identity_or_lower_tf_cannot_borrow_receipt(self):
  self.tv()
  for change in ({'currency':'USD'},{'venue':'FRA'},{'symbol':'OTHER.GTX'},{'source':'proxy'}):
   f=self.raw.copy();f.attrs.update(change)
   self.assertFalse(pc._display_reviews(self.s,self.i,f,'15m'))
  self.assertFalse(pc._display_reviews(self.s,self.i,self.raw.copy(),'5m'))
 def test_renderer_omits_only_accepted_bands_keeps_path_breaks_and_new_live_gap(self):
  self.tv();out=self.project();old=out.attrs['partial_chart']['break_before'][0]
  new=str(out.index[-2]);out.attrs['partial_chart']['break_before'].append(new)
  ax=Figure().subplots();draw_prices(ax,out,'Line')
  self.assertEqual(len(ax.collections),2)
  self.assertEqual(len(ax.collections[0].get_paths()),1)
  self.assertEqual(np.isnan(ax.lines[0].get_ydata()).sum(),2)
  self.assertIn(old,out.attrs['partial_chart']['accepted_break_before'])
 def test_future_instrument_all_eleven_targets_use_same_generic_projection(self):
  self.i={**self.i,'isin':'DE0005557508','name':'Future stock'};self.s.save_instrument(self.i)
  now=pd.Timestamp('2026-11-02T23:30Z')
  for base,targets,start,gap in [
   ('5m',('5m','10m'),'2026-10-01','2026-10-28 13:30'),
   ('15m',('15m','30m','1H','2H','3H','4H'),'2026-10-01','2026-10-28 13:30'),
   ('1D',('1D','1W','1M'),'2026-01-01','2026-09-10')]:
   idx=dq.expected_index({**self.m,'timeframe':base},base,start,'2026-11-02 22:55',now)
   f=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=100.),index=idx).drop(pd.Timestamp(gap))
   f.attrs={**self.m,'timeframe':base,'classify_absence':True,
    'listing_proof':dict(self.m,isin=self.i['isin'],status='CONFIRMED')}
   f.attrs['data_quality']=dq.audit(f,base,now=now)
   self.tv(f,base,[gap])
   for tf in targets:
    with self.subTest(tf=tf):
     out=self.project(tf,f,base,now);self.assertIsNotNone(out)
     self.assertFalse(self.warnings(out));self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
 def test_old_receipt_and_price_age_do_not_create_fresh_price_or_permission(self):
  self.tv();out=self.project(now=self.now+pd.Timedelta(days=2))
  self.assertFalse(self.warnings(out));self.assertNotIn('accepted_risk',out.attrs)
  self.assertEqual(out.index[-1],self.raw.index[-1]);self.assertEqual(out.close.iloc[-1],self.raw.close.iloc[-1])

if __name__=='__main__':unittest.main()
