"""One bounded native-after-derived CP scenario; no live database or network."""
import copy,hashlib,json,sys,tempfile,unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
import gettex_chart as chart
import data_quality as dq
from storage import Store
from history_service import parent_provenance_key
from performance_cache import _closed_rows,_digest


class NativeParentConfirmation(unittest.TestCase):
 def test_native_replaces_derived_origin_only_with_exact_current_closed_evidence(self):
  archive=Path(__file__).parent.parent.parent/'СМ-Архив'
  with ExitStack() as stack:
   directory=stack.enter_context(tempfile.TemporaryDirectory(dir=archive))
   s=Store(directory);stack.callback(s.read_cache.close)
   isin='US67066G1040';meta=chart.identity(isin,'NVDA.GTX');info=dict(isin=isin,name='fixture')
   now=pd.Timestamp('2026-11-13T08:00Z');received=now-pd.Timedelta(hours=1)
   s.save_instrument(info)
   child=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=1.),
                      index=pd.date_range('2026-11-11 07:30','2026-11-11 22:45',freq='15min'))
   child.attrs=meta.copy();s.save_series(info,child,'15m')
   stamp='2026-11-11 00:00:00';values=[100.,102.02,99.,101.,62.]
   native=pd.DataFrame([values],index=pd.DatetimeIndex([stamp]),columns=chart.FIELDS)
   native.attrs=meta.copy();s.save_series(info,native,'1D')
   spec=dict(isin=isin,identity=meta,base_tf='15m',timeframe='15m',**{'from':stamp,'through':'2026-11-11T22:00:00+00:00'})
   with s.connect() as db:digests=[_digest(_closed_rows(db,spec))]
   proof=dict(inputs=[spec],digests=digests,ohlcv=[100.,102.,99.,101.,62.],
              absence_scope=dict(version=1,base='15m',stamps=[]),detected_at='2026-11-11T22:30:00+00:00')
   lineage=parent_provenance_key(isin,meta,'1D');s.set_setting(lineage,{stamp:proof})
   payload=dict(status='OK',request=dict(ric='NVDA.GTX',samples='D'),data=[dict(
       _DATE_END='2026-11-11',OPEN_PRC='100',HIGH_1='102.02',LOW_1='99',CLOSE_PRC='101',ACVOL_1='62')])
   raw=json.dumps(payload).encode();folder=Path(directory)/'chart_cache'/isin;folder.mkdir(parents=True)
   receipt=folder/'1D-test.json';receipt.write_bytes(raw)
   saved=dict(state='SAVED',identity=meta,listing_proof=dict(meta,isin=isin,status='CONFIRMED'),
       receipt=str(receipt),sha256=hashlib.sha256(raw).hexdigest(),received_at=received.isoformat(),
       requested_from='2026-11-10T23:00:00+00:00',requested_to='2026-11-12T22:00:00+00:00')
   key=chart.state_key(isin,'1D');s.set_setting(key,saved)
   def frame():return s.load_series(isin,'1D','gettex_chart','NVDA.GTX','GETTEX','EUR')
   self.assertEqual(dq.audit(frame(),'1D',now=now,check_stale=False)['state'],'INVALID')
   # Bad evidence must leave the original lineage and blocked data intact.
   for bad in ('hash','identity','stale_receipt','forming','row_changed','invalid_ohlcv','missing_row'):
    with self.subTest(guard=bad):
     state=copy.deepcopy(saved);reply=copy.deepcopy(payload);s.save_series(info,native,'1D')
     if bad=='hash':state['sha256']='0'*64
     if bad=='identity':state['identity']['currency']='USD'
     if bad=='stale_receipt':state['received_at']='2026-11-11T22:00:00+00:00'
     if bad=='forming':state['received_at']='2026-11-11T12:00:00+00:00'
     if bad=='row_changed':
      different=native.copy();different['high']=103.;s.save_series(info,different,'1D')
     if bad=='invalid_ohlcv':reply['data'][0]['HIGH_1']='98'
     if bad=='missing_row':reply['data']=[]
     content=json.dumps(reply).encode();receipt.write_bytes(content)
     if bad!='hash':state['sha256']=hashlib.sha256(content).hexdigest()
     s.set_setting(key,state)
     self.assertEqual(chart.confirm_native_parents(s,info,'1D',now)['confirmed'],0)
     self.assertIn(stamp,s.setting(lineage));self.assertIsNone(s.setting('cp_assessment_request:'+isin))
   receipt.write_bytes(raw);s.set_setting(key,saved);s.save_series(info,native,'1D')
   original_decode=chart.decode
   def raced(*args,**kwargs):
    result=original_decode(*args,**kwargs);s.save_instrument(dict(info,name='changed'));return result
   with patch.object(chart,'decode',side_effect=raced),self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):
    chart.confirm_native_parents(s,info,'1D',now)
   self.assertIn(stamp,s.setting(lineage));s.save_instrument(info)
   # Exercise the ordinary CP turn, including a source cooldown, without HTTP.
   with patch.object(chart,'_fetch_turn',return_value={'state':'COOLDOWN'}),patch.object(chart,'prepare_daily',return_value={}),patch.object(chart,'recover_closed_daily_tail',return_value={}):
    result=chart.turn(info,s,now)
   self.assertNotIn(stamp,s.setting(lineage))
   self.assertEqual(result['native_parent_confirmation']['1D']['confirmed'],1)
   self.assertEqual(frame()[list(chart.FIELDS)].iloc[0].tolist(),values)
   self.assertEqual(dq.audit(frame(),'1D',now=now,check_stale=False)['state'],'READY')
   self.assertNotEqual(s.setting(dq.key(info,meta['symbol'],'1D'))['state'],'INVALID')
   self.assertIsNotNone(s.setting('cp_assessment_request:'+isin))
   with s.connect() as db:
    rows=db.execute("SELECT payload FROM journal WHERE kind='cp_native_parent_confirmation'").fetchall()
    self.assertEqual(len(rows),1);self.assertEqual(json.loads(rows[0][0])['previous_derived_proof'],proof)
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='candle_recoveries'").fetchone():
     self.assertEqual(db.execute('SELECT count(*) FROM candle_recoveries').fetchone()[0],0)
   self.assertEqual(chart.confirm_native_parents(s,info,'1D',now)['confirmed'],0)
   self.assertIsNone(s.setting('cp_manual_risk:'+isin));s.read_cache.close()


if __name__=='__main__':unittest.main()
