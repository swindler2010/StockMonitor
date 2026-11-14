"""Receipt dependencies follow the real source/base, including rejected grants."""
import copy,unittest
import test_tv_accepted_display as fx
from test_tv_accepted_display import ctl
from performance_cache import InputRevisionChanged


class ReceiptDependencies(unittest.TestCase):
 setUp=fx.AcceptedDisplay.setUp
 tearDown=fx.AcceptedDisplay.tearDown
 prepare=fx.AcceptedDisplay.prepare
 native=fx.AcceptedDisplay.native
 def setup_receipts(self):
  self.prepare()
  self.match='cp_reconciliation_job:'+self.i['isin']+':MATCH'
  self.other='cp_reconciliation_job:'+self.i['isin']+':OTHER'
  self.s.set_setting(self.match,{'observation':'ABSENT'})
  self.s.set_setting(self.other,{'observation':'ABSENT'})
  self.grant_key='cp_manual_risk:'+self.i['isin']
  grant=self.s.setting(self.grant_key)
  with self.s.connect() as db:
   entry=grant['accepted']['0'];entry['input_revisions'].update(ctl._revisions(db,['setting:'+self.match]))
   other=copy.deepcopy(entry);other['problem']['tf']='5m';other['input_revisions']=ctl._revisions(db,['setting:'+self.other])
  grant['accepted']['other']=other;self.s.set_setting(self.grant_key,grant)
 def tracked(self,attach=False):
  with self.s.read_cache.track_inputs(isolated=True) as token:
   if attach:
    out=self.raw.copy();ctl.attach_tv_absence(self.s,out,self.i['isin'],'15m')
   else:out=self.native()
  return out,dict(token)
 def test_chart_receipts_only_track_matching_identity_and_base(self):
  self.setup_receipts();out,token=self.tracked(True)
  self.assertIn('tv_accepted_absence',out.attrs)
  self.assertIn('setting:'+self.match,token);self.assertNotIn('setting:'+self.other,token)
 def test_analysis_keeps_result_when_unrelated_tf_receipt_changes(self):
  self.setup_receipts();out,token=self.tracked()
  self.assertIn('accepted_gap_input',out.attrs)
  self.s.set_setting(self.other,{'observation':'PRESENT'})
  self.s.read_cache.assert_current(token)
  after,_=self.tracked()
  self.assertEqual(out.attrs['accepted_gap_input'],after.attrs['accepted_gap_input'])
  self.assertEqual(out.attrs['tv_accepted_absence'],after.attrs['tv_accepted_absence'])
 def test_related_change_still_revokes_and_rejected_receipt_is_tracked(self):
  self.setup_receipts();_,token=self.tracked()
  self.s.set_setting(self.match,{'observation':'PRESENT'})
  with self.assertRaises(InputRevisionChanged):self.s.read_cache.assert_current(token)
  out,token=self.tracked()
  self.assertNotIn('tv_accepted_absence',out.attrs);self.assertNotIn('accepted_gap_input',out.attrs)
  self.assertIn('setting:'+self.match,token)
 def test_foreign_listing_cannot_affect_or_cover_current_frame(self):
  self.setup_receipts();grant=self.s.setting(self.grant_key)
  grant['accepted']['other']['problem']['tf']='15m'
  grant['accepted']['other']['problem']['identity']['source']='gettex_chart'
  self.s.set_setting(self.grant_key,grant)
  out,token=self.tracked();self.assertNotIn('setting:'+self.other,token)
  self.assertEqual(out.attrs['accepted_gap_input']['accepted_problem_count'],1)
 def test_policy_and_grant_changes_still_revoke(self):
  self.setup_receipts();_,token=self.tracked()
  self.s.set_setting(ctl.TV_POLICY,{'enabled':False})
  with self.assertRaises(InputRevisionChanged):self.s.read_cache.assert_current(token)
  out,_=self.tracked();self.assertNotIn('accepted_gap_input',out.attrs)
  with self.s.read_cache.track_inputs(isolated=True) as token:self.native()
  grant=self.s.setting(self.grant_key);grant['accepted']={};self.s.set_setting(self.grant_key,grant)
  with self.assertRaises(InputRevisionChanged):self.s.read_cache.assert_current(token)
 def test_actual_5m_child_still_tracks_5m_receipt(self):
  self.setup_receipts();out=self.raw.copy()
  with self.s.read_cache.track_inputs(isolated=True) as token:ctl.attach_tv_absence(self.s,out,self.i['isin'],'5m')
  self.assertIn('setting:'+self.other,token);self.assertNotIn('setting:'+self.match,token)


if __name__=='__main__':unittest.main()
