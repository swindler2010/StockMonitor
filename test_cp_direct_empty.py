"""Due own-source proof updates use the existing writer, never a new loader."""
import copy,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
from cp_direct_empty import pending
import test_cp_automatic_parent as parent_fixture

class DirectEmpty(unittest.TestCase):
 def fixture(self):
  owner=parent_fixture.AutomaticParent();self.addCleanup(owner.doCleanups)
  s,info,meta,proof,chart,now=owner.fixture();isin=info['isin'];at=chart['first']
  with s.connect() as db:
   current=rv.read(db,rv.key(chart));old=copy.deepcopy(current);current['automatic_parent_algorithm']=1;rv._save_job(db,current,old)
   j,_=rv._enqueue(db,rv._spec(isin,proof['identity'],'5m',at,at),{'kind':'old-source-fixture'},now)
   old=copy.deepcopy(j);j.update(status='AWAITING_USER',finding=dict(state='UNCONFIRMED'),input_fingerprint='old-own-source-proof',checks=1);rv._save_job(db,j,old)
  return s,info,proof,j,now
 def test_changed_complete_own_proof_is_processed_without_http_or_candle_edit(self):
  s,info,proof,j,now=self.fixture();isin=info['isin']
  with s.connect() as db:
   self.assertEqual([k for k,v in pending(db,isin,8)],[rv.key(j)]);self.assertEqual(pending(db,isin,0),[])
   before=db.execute('SELECT * FROM candles ORDER BY base_tf,ts').fetchall()
  with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No network')):result=rv.turn(s,info,now,native_empty_only=True)
  self.assertEqual(result['checked'],1);current=s.setting(rv.key(j));self.assertEqual(current['status'],'RESOLVED_NATIVE');self.assertEqual(current['finding']['state'],'NO_TRADES')
  with s.connect() as db:self.assertEqual(db.execute('SELECT * FROM candles ORDER BY base_tf,ts').fetchall(),before);self.assertEqual(pending(db,isin,8),[])
  self.assertIsNone(s.setting('cp_manual_risk:'+isin))
 def test_partial_zero_wrong_identity_current_observation_deadline_and_unchanged_inputs_stay_out(self):
  s,info,proof,j,now=self.fixture();isin=info['isin']
  for change in (dict(no_trades=[]),dict(incomplete_publication_intervals=[[j['first'],j['end']]]),dict(zero_quantity_intervals=[[j['first'],j['end']]]),dict(identity={**proof['identity'],'currency':'USD'})):
   with self.subTest(change=next(iter(change))):
    s.set_setting('interval_evidence:'+isin,{**proof,**change})
    with s.connect() as db:self.assertEqual(pending(db,isin,8),[])
  s.set_setting('interval_evidence:'+isin,proof)
  for change in (dict(observation=dict(result='PRESENT',identity_confirmed=True)),dict(next_check=(now+__import__('pandas').Timedelta(hours=1)).isoformat())):
   with s.connect() as db:
    current=rv.read(db,rv.key(j));old=copy.deepcopy(current);current.update(change);rv._save_job(db,current,old)
    self.assertEqual(pending(db,isin,8),[]);rv._save_job(db,j,current)
  with s.connect() as db:
   current=rv.read(db,rv.key(j));old=copy.deepcopy(current);current['input_fingerprint']=rv._input(db,current)['fingerprint'];rv._save_job(db,current,old);self.assertEqual(pending(db,isin,8),[])
  policy=s.setting(rv.SOURCE_POLICY);s.set_setting(rv.SOURCE_POLICY,{**policy,'approved':False})
  with s.connect() as db:self.assertEqual(pending(db,isin,8),[])
 def test_source_change_after_nomination_is_not_published_as_no_trades(self):
  s,info,proof,j,now=self.fixture();isin=info['isin']
  with s.connect() as db:self.assertTrue(pending(db,isin,8))
  s.set_setting('interval_evidence:'+isin,{**proof,'no_trades':[],'incomplete_publication_intervals':[[j['first'],j['end']]]})
  with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No network')):rv.turn(s,info,now,native_empty_only=True)
  current=s.setting(rv.key(j));self.assertNotEqual(current['status'],'RESOLVED_NATIVE');self.assertNotEqual(current['finding']['state'],'NO_TRADES')
  self.assertIsNone(s.setting('cp_manual_risk:'+isin))

if __name__=='__main__':unittest.main()
