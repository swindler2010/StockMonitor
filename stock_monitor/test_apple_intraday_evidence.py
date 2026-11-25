import copy
import importlib.util
import json
from pathlib import Path
import unittest
import pandas as pd
import data_quality as q

E=Path(__file__).resolve().parents[1]/'history64-evidence/apple-intraday30'
NOW=pd.Timestamp('2026-10-30T13:50:17.912386Z')
META=dict(source='gettex',symbol='US0378331005',venue='GETTEX',currency='EUR',timezone='Europe/Berlin',timeframe='5m',classify_absence=True)

def meta(trades=(),empty=()):
 m=copy.deepcopy(META)
 m['absence_evidence']=dict(identity={k:m[k] for k in q.IDENTITY},basis='complete_published_trade_files',reference='saved-test-evidence',checked_at=NOW.isoformat(),trade_intervals=list(trades),no_trades=list(empty))
 return m

class IndexedEvidenceTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  spec=importlib.util.spec_from_file_location('evidence_before',E/'before-code/data_quality.py');cls.before=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.before)

 def test_trade_boundaries_and_nested_ranges_match_previous(self):
  m=meta(trades=[('2026-10-29 08:00','2026-10-29 08:30'),('2026-10-29 08:05','2026-10-29 08:06'),('2026-10-29 08:10','2026-10-29 08:10'),('2026-10-29 08:11','2026-10-29 08:12')])
  cases=[('08:05','08:06',True),('08:04','08:05',False),('08:06','08:11',False),('08:11','08:12',True),('08:07','08:09',False),('08:00','08:30',True)]
  for a,b,expected in cases:
   args=(m,'2026-10-29 '+a,'2026-10-29 '+b,NOW)
   self.assertEqual(q.known_trade(*args),expected)
   self.assertEqual(q.known_trade(*args),self.before.known_trade(*args))

 def test_identity_time_and_proof_requirements_still_block(self):
  m=meta(trades=[('2026-10-29 08:00','2026-10-29 08:01')])
  for kind in ('source','currency','venue','basis','future','missing_reference','trade_after_checked'):
   bad=copy.deepcopy(m)
   if kind in ('source','currency','venue'):bad['absence_evidence']['identity'][kind]='wrong'
   elif kind=='basis':bad['absence_evidence']['basis']='empty_api'
   elif kind=='future':bad['absence_evidence']['checked_at']=(NOW+pd.Timedelta(hours=1)).isoformat()
   elif kind=='missing_reference':bad['absence_evidence']['reference']=''
   else:bad['absence_evidence']['checked_at']='2026-10-29T07:00:00Z'
   self.assertFalse(q.known_trade(bad,'2026-10-29 08:00','2026-10-29 08:05',NOW),kind)

 def test_changed_evidence_does_not_reuse_stale_answer(self):
  m=meta(trades=[])
  args=(m,'2026-10-29 08:00','2026-10-29 08:05',NOW)
  self.assertFalse(q.known_trade(*args))
  m['absence_evidence']['trade_intervals'].append(['2026-10-29 08:01','2026-10-29 08:02'])
  self.assertTrue(q.known_trade(*args))
  m['absence_evidence']['trade_intervals'].clear()
  self.assertFalse(q.known_trade(*args))

 def test_covered_empty_close_dst_and_contradiction_preserved(self):
  m=meta(empty=[('2026-10-23 22:55','2026-10-23 23:00'),('2026-10-26 07:30','2026-10-26 07:40')],trades=[('2026-10-26 07:36','2026-10-26 07:37')])
  stamps=pd.to_datetime(['2026-10-23 22:55','2026-10-24 07:30','2026-10-26 07:30','2026-10-26 07:35'])
  actual=q.covered_empty(m,stamps,'5m',NOW)
  self.assertTrue(actual.equals(pd.to_datetime(['2026-10-23 22:55','2026-10-26 07:30'])))
  self.assertTrue(actual.equals(self.before.covered_empty(m,stamps,'5m',NOW)))

 def test_real_saved_audits_are_identical(self):
  before=json.loads((E/'before.json').read_text(encoding='utf8'))
  for base in ('5m','15m'):
   raw=pd.read_pickle(E/('gettex-'+base+'.pkl'))
   actual=q.audit(raw,base,now=NOW);expected=before['bases']['gettex:'+base]['dq']
   actual.pop('checked_at');expected.pop('checked_at')
   self.assertEqual(actual,expected,base)

 def test_unknown_and_invalid_are_not_promoted(self):
  f=pd.read_pickle(E/'gettex_chart-15m.pkl')
  report=q.audit(f,'15m',now=NOW)
  self.assertEqual(report['state'],'UNCONFIRMED')
  bad=f.copy();bad.iloc[-1,bad.columns.get_loc('high')]=0
  self.assertEqual(q.audit(bad,'15m',now=NOW)['state'],'INVALID')

if __name__=='__main__':unittest.main()
