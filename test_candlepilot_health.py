from contextlib import closing
import unittest,tempfile,sqlite3,json,sys,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from candlepilot_health import collect,delta,proof_slots
class Health(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'stock_monitor.sqlite3'
  with closing(sqlite3.connect(self.path)) as c, c:
   c.executescript('CREATE TABLE instruments(isin TEXT,payload TEXT);CREATE TABLE settings(key TEXT,value TEXT);CREATE TABLE candle_revision_history(isin TEXT,identity TEXT,base_tf TEXT,ts TEXT);')
   c.execute('INSERT INTO instruments VALUES (?,?)',('ISIN',json.dumps({'name':'Example'})))
 def setting(self,key,value):
  with closing(sqlite3.connect(self.path)) as c, c:c.execute('INSERT INTO settings VALUES (?,?)',(key,json.dumps(value)))
 def test_read_only(self):
  before=self.path.read_bytes();collect(self.path);self.assertEqual(before,self.path.read_bytes())
 def test_series_dedup_and_latest(self):
  identity=dict(source='proxy',venue='GER',currency='EUR',symbol='X.DE')
  self.setting('history_archive_progress:ISIN',{'series':{json.dumps(['proxy','GER','EUR','X.DE','1D']):{'quality':{'state':'GAP','checked_at':'2026-01-01'}}}})
  self.setting('data_quality:ISIN:X.DE:1D',dict(identity=identity,base_tf='1D',state='INVALID',checked_at='2026-02-01'))
  r=collect(self.path);self.assertEqual((r['instruments'],r['archive_series'],r['saved_dq_series']),(1,1,1));self.assertEqual(r['states'],{'INVALID':1})
 def test_revision_not_double_counted(self):
  with closing(sqlite3.connect(self.path)) as c, c:c.executemany('INSERT INTO candle_revision_history VALUES (?,?,?,?)',[('I','meta','1D','t')]*2)
  r=collect(self.path);self.assertEqual(r['revision_changed_unique_candles'],1);self.assertEqual(r['revision_events'],2);self.assertIsNone(r['recovered_unique_candles'])
 def test_evidence_not_unknown(self):
  self.setting('interval_evidence:I',{'basis':'complete_published_trade_files','identity':{},'no_trades':[['a','b'],['a','b']]})
  self.setting('interval_evidence:J',{'basis':'empty_api','identity':{},'no_trades':[['c','d']]})
  self.assertEqual(collect(self.path)['no_trades_proof_ranges'],1)
 def test_delta_not_repairs(self):
  before=[{'key':'a','state':'GAP'},{'key':'b','state':'READY'},{'key':'c','state':'INVALID'}]
  after=[{'key':'a','state':'READY'},{'key':'b','state':'INVALID'},{'key':'d','state':'READY'}]
  r=delta(after,before);self.assertEqual([r[k] for k in ('resolved','new_problems','changed','added','removed')],[1,1,2,1,1])
 def test_overlapping_proofs_count_once(self):
  r=proof_slots({('I','2026-10-20 07:30:00','2026-10-20 07:45:00'),('I','2026-10-20 07:35:00','2026-10-20 07:40:00'),('J','2026-10-20 07:35:00','2026-10-20 07:40:00')})
  self.assertEqual(r,(4,0))
 def test_unquantified_is_not_proof_slot(self):
  self.assertEqual(proof_slots({('I','bad','bad'),('I','2026-10-20 07:31:00','2026-10-20 07:36:00')}),(0,2))
 def test_persisted_baseline(self):
  r=collect(self.path);(self.path.parent/'candlepilot-health-checkpoint.json').write_text(json.dumps(r));self.assertEqual(collect(self.path)['delta']['changed'],0)
if __name__=='__main__':unittest.main()
