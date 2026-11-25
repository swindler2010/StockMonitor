"""An appended archive bar must not invalidate unchanged closed analysis."""
import copy,unittest
import test_admission_expiry as fx
import calculation_window as cw
from performance_cache import capture_closed_inputs,validate_closed_inputs,InputRevisionChanged


class ArchiveTailCounts(unittest.TestCase):
 def test_tail_counts_keep_closed_proof_but_material_changes_and_expiry_reject(self):
  f=fx.Publication(methodName='runTest');f.setUp();self.addCleanup(f.tearDown)
  key='data_quality:TEST:TEST.GTX:15m'
  meta=dict(source='gettex_chart',symbol='TEST.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  spec=dict(isin='TEST',identity=meta,base_tf='15m',timeframe='1H',through='2026-11-10T09:15Z')
  report=dict(state='UNCONFIRMED',identity=meta,base_tf='15m',missing_count=1,
   ranges=[['2026-11-09 22:45:00']*2],interval_classification=dict(expected_open_intervals=2,
    with_candles=1,NO_TRADES=0,UNCONFIRMED=1,GAP=0,market_closed_intervals=0,
    unconfirmed_ranges=[['2026-11-09 22:45:00']*2],DELAYED=0,PENDING=0))
  def insert(stamp,close=10):
   with f.s.connect() as db:db.execute('INSERT OR REPLACE INTO series VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
    ('TEST','gettex_chart','GETTEX','EUR','TEST.GTX','15m',stamp,10,11,9,close,100))
  insert('2026-11-10 10:00:00');f.s.set_setting(key,report)
  with f.s.connect() as db:
   token={k:v for k,v in db.execute('SELECT scope,revision FROM input_revisions')
    if k in ('instrument:TEST','market:TEST:15m','setting:'+key)}
   proof=capture_closed_inputs(db,[spec],token)
  record=f.assessment(type('Ctx',(),{'store':f.s})(),f.i,f.now);record['execution_version']=1
  candidate=dict(timeframe='1H',input_binding_version=2,closed_inputs=proof,
   analysis_ready=True,trade_ready=False,execution_revisions=copy.deepcopy(token),
   expires_at=(f.now+fx.pd.Timedelta(minutes=10)).isoformat())
  original=copy.deepcopy(candidate)
  def state(now=f.now):
   with f.s.connect() as db:return cw.candidate_state(db,record,candidate,now)
  self.assertEqual(state(),'CURRENT')
  insert('2026-11-10 10:15:00') # exactly outside the proven closed range
  tail=copy.deepcopy(report);tail['interval_classification'].update(expected_open_intervals=3,with_candles=2)
  f.s.set_setting(key,tail)
  self.assertEqual(state(),'CURRENT')
  with f.s.connect() as db:
   current=validate_closed_inputs(db,proof)
   self.assertNotEqual(current['market:TEST:15m'],token['market:TEST:15m'])
   self.assertNotEqual(current['setting:'+key],token['setting:'+key])
  self.assertEqual(candidate,original) # execution revisions and deadline never rebase
  self.assertEqual(state(fx.pd.Timestamp(candidate['expires_at'])),'EXPIRED')
  self.assertEqual(f.s.setting(key),tail) # factual DQ and diagnostics untouched
  for changes in [dict(UNCONFIRMED=2,expected_open_intervals=4),dict(GAP=1,expected_open_intervals=4),
    dict(unconfirmed_ranges=[['2026-11-10 10:00:00']*2]),dict(DELAYED=1),dict(PENDING=1),
    dict(evidence_reference='new proof'),dict(unknown_fact=True),dict(with_candles=999)]:
   with self.subTest(changes=changes):
    bad=copy.deepcopy(tail);bad['interval_classification'].update(changes);f.s.set_setting(key,bad)
    self.assertEqual(state(),'STALE')
  f.s.set_setting(key,dict(tail,state='INVALID'));self.assertEqual(state(),'STALE')
  f.s.set_setting(key,dict(tail,identity=dict(meta,currency='USD')));self.assertEqual(state(),'STALE')
  f.s.set_setting(key,tail);insert('2026-11-10 10:00:00',10.5)
  self.assertEqual(state(),'STALE')
  with f.s.connect() as db:
   with self.assertRaisesRegex(InputRevisionChanged,'Closed candle changed'):validate_closed_inputs(db,proof)
  self.assertFalse(candidate['trade_ready'])


if __name__=='__main__':unittest.main()
