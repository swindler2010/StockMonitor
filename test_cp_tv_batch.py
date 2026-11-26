"""Only new packet, mixed native coverage and post-review integration cases."""
import copy, json, unittest
from unittest.mock import patch
import test_cp_reconciliation as fixtures
from test_cp_reconciliation import rv, ctl, Store, pd

class Batch(unittest.TestCase):
 setUp=fixtures.Reconciliation.setUp
 tearDown=fixtures.Reconciliation.tearDown
 event=fixtures.Reconciliation.event
 proof=fixtures.Reconciliation.proof
 jobs=fixtures.Reconciliation.jobs
 snapshot=fixtures.Reconciliation.snapshot
 def packet(self,count=2):
  self.now=pd.Timestamp.now(tz='UTC')
  for n in range(count):self.event(at=str(pd.Timestamp(self.a)+pd.Timedelta(minutes=15*n)))
  rv.turn(self.s,self.i,self.now)
  return self.s.setting(rv.PROGRESS+self.i['isin'])['review_batch']
 def answers(self,p):return [dict(id=x['id'],result='ABSENT',tv_symbol='GETTEX:APC') for x in p['items']]
 def send(self,p,answers=None,token='submission-test-001',confirmed=True):
  return rv.observe_batch(self.s,self.i['isin'],p,answers or self.answers(p),token,confirmed)
 def test_automatic_bounded_packet_precise_identity_and_utc(self):
  p=self.packet(4);self.assertEqual(len(p['items']),4)
  self.assertTrue(all(x['identity']==self.m and x['timezone']=='Europe/Berlin' for x in p['items']))
  self.assertTrue(all(pd.Timestamp(x['utc_start'])==pd.Timestamp(x['start']).tz_localize('Europe/Berlin').tz_convert('UTC') for x in p['items']))
  self.assertTrue(all(x['tv_symbol']=='' for x in p['items'])) # never guess ticker
 def test_atomic_confirmation_retains_unknown_no_trade_or_price_writes(self):
  p=self.packet();before=self.snapshot();a=self.answers(p);a[1]['result']='UNCONFIRMED'
  r=self.send(p,a);self.assertEqual(r['recorded'],2);self.assertEqual(r['candles_written'],0)
  rv.turn(self.s,self.i,pd.Timestamp.now(tz='UTC'))
  self.assertTrue(all(j['finding']['state']=='UNCONFIRMED' for j in self.jobs()['rows']))
  self.assertEqual(before,self.snapshot());self.assertIsNone(self.s.setting('cp_manual_risk:'+self.i['isin']))
 def test_idempotent_receipt_and_conflicting_retry(self):
  p=self.packet();first=self.send(p);second=self.send(p)
  self.assertTrue(second['duplicate']);self.assertEqual(first['recorded'],second['recorded'])
  a=self.answers(p);a[0]['result']='PRESENT'
  with self.assertRaisesRegex(ValueError,'CONFLICT'):self.send(p,a)
  self.assertTrue(all(not j.get('observation_history') for j in self.jobs()['rows']))
 def test_changed_last_row_rejects_whole_packet(self):
  p=self.packet();last=p['items'][-1]
  with self.s.connect() as db:
   db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.i['isin'],'gettex','15m',last['start'],100,101,99,100,5))
  with self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):self.send(p)
  self.assertTrue(all(not j.get('observation') for j in self.jobs()['rows']))
 def test_changed_instrument_and_proof_reject_confirmation(self):
  for change in ('identity','proof'):
   p=self.packet(1)
   if change=='identity':self.s.save_instrument(dict(self.i,name='Apple changed'))
   else:self.proof()
   with self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):self.send(p)
   self.assertTrue(all(not j.get('observation') for j in self.jobs()['rows']))
 def test_no_default_answer_wrong_venue_and_unconfirmed_identity(self):
  p=self.packet(1)
  with self.assertRaisesRegex(ValueError,'CONFIRMATION'):self.send(p,confirmed=False)
  for field,value in [('tv_symbol','NASDAQ:AAPL'),('result','NO_TRADES')]:
   a=self.answers(p);a[0][field]=value
   with self.assertRaises(ValueError):self.send(p,a)
  with self.assertRaisesRegex(ValueError,'CONFIRMATION'):rv.observe_batch(self.s,self.i['isin'],p,[],'test-empty',True)
 def test_tampered_packet_and_expired_packet_rejected(self):
  p=self.packet();q=copy.deepcopy(p);q['items'][0]['tf']='1D'
  with self.assertRaisesRegex(ValueError,'BATCH_CHANGED'):self.send(q)
  q=copy.deepcopy(p);q['valid_until']='2026-01-01T00:00:00Z'
  with self.assertRaisesRegex(ValueError,'EXPIRED'):self.send(q)
 def test_duplicate_rows_rejected_and_unanswered_rows_remain(self):
  p=self.packet();a=self.answers(p)
  with self.assertRaisesRegex(ValueError,'DUPLICATE'):self.send(p,[a[0],a[0]])
  self.send(p,[a[0]])
  self.assertEqual(sum(bool(j.get('observation')) for j in self.jobs()['rows']),1)
 def test_unchanged_packet_survives_routine_recheck(self):
  p=self.packet();rv.turn(self.s,self.i,self.now+pd.Timedelta(hours=1,minutes=1))
  self.assertEqual(p,self.s.setting(rv.PROGRESS+self.i['isin'])['review_batch'])
  self.assertEqual(self.send(p)['recorded'],2)
 def test_restart_keeps_packet_receipt_and_progress(self):
  p=self.packet();self.s.read_cache.close();self.s=Store(self.temp.name)
  self.assertEqual(p,self.s.setting(rv.PROGRESS+self.i['isin'])['review_batch']);r=self.send(p)
  self.s.read_cache.close();self.s=Store(self.temp.name)
  self.assertEqual(self.s.setting(rv.PROGRESS+self.i['isin'])['batch_receipts'][-1],r)
  self.assertTrue(self.send(p)['duplicate'])
 def test_human_priority_reserves_ordinary_queue_slots(self):
  self.packet(4);p=self.s.setting(rv.PROGRESS+self.i['isin'])['review_batch'];self.send(p)
  self.event(at='2026-10-29 10:00:00');self.event(at='2026-10-29 10:15:00')
  result=rv.turn(self.s,self.i,pd.Timestamp.now(tz='UTC'))
  self.assertEqual(result['checked'],4)
  jobs=self.jobs()['rows'];self.assertTrue(all(j.get('checks')==1 for j in jobs if j['first'].startswith('2026-10-29')))
  self.assertEqual(sum(j['status']=='RECHECK_QUEUED' for j in jobs),2)
  self.assertIsNone(result['analysis_requested_at'])
  self.assertIsNotNone(rv.turn(self.s,self.i,pd.Timestamp.now(tz='UTC'))['analysis_requested_at'])
 def test_existing_feed_requests_native_eligibility_once_and_records_error(self):
  from feed_worker import FeedContext
  p=self.packet(1);self.send(p)
  with patch.object(FeedContext,'_calculate',return_value={'errors':[]}),patch.object(ctl,'automatic_turn',return_value={}),patch('calculation_window.eligibility_turn',return_value={'error':'REAL_DATA_BLOCKED','checked_at':'fixture'}) as assess:
   FeedContext(self.s).calculate(self.i);FeedContext(self.s).calculate(self.i);self.assertEqual(assess.call_count,1)
  receipt=self.s.setting(rv.PROGRESS+self.i['isin'])['analysis_receipt'];self.assertEqual(receipt['error'],'REAL_DATA_BLOCKED')
 def test_newer_request_cannot_be_acknowledged_by_old_result(self):
  self.s.set_setting(rv.PROGRESS+self.i['isin'],{'analysis_requested_at':'new'})
  rv.complete_analysis_request(self.s,self.i['isin'],'old',{'checked_at':'old'})
  self.assertEqual(self.s.setting(rv.PROGRESS+self.i['isin']),{'analysis_requested_at':'new'})
 def test_mcp_collection_exports_identical_saved_packet_readonly(self):
  p=self.packet();before=self.snapshot()
  with self.s.connect() as d:
   d.execute('PRAGMA query_only=ON');result=rv.collect(d,self.i['isin'])
  self.assertEqual(result['review_batches'][self.i['isin']],p);self.assertEqual(before,self.snapshot())
 def mixed(self):
  spec=rv._spec(self.i['isin'],self.m,'15m','2026-10-28 10:00:00','2026-10-28 10:30:00')
  inputs={'rows':[(x,100.,101.,99.,100.,10.) for x in ('2026-10-28 10:00:00','2026-10-28 10:30:00')],'proof':self.proof()}
  return spec,inputs
 def test_mixed_native_rows_plus_complete_no_trades_resolves_no_padding(self):
  spec,inputs=self.mixed();original=copy.deepcopy(inputs);r=rv._classify(spec,inputs,self.now)
  self.assertEqual(r['state'],'NATIVE_ROWS_CONFIRMED');self.assertEqual(r['confirmed_empty_intervals'],1)
  self.assertEqual(r['actual_candles'],2);self.assertEqual(inputs,original);self.assertFalse(r['can_affect_dq'])
 def test_mixed_incomplete_wrong_source_isin_contradictory_evidence_blocked(self):
  spec,inputs=self.mixed();p=inputs['proof']
  bad=[dict(p,identity=dict(self.m,source='gettex_chart')),dict(p,isin='WRONG'),dict(p,no_trades=[[self.a,'2026-10-28 10:20:00']]),dict(p,trade_intervals=[[self.a,'2026-10-28 10:20:00']]),dict(p,zero_quantity_intervals=[[self.a,'2026-10-28 10:20:00']])]
  for proof in bad:
   with self.subTest(proof=proof):self.assertNotEqual(rv._classify(spec,dict(inputs,proof=proof),self.now)['state'],'NATIVE_ROWS_CONFIRMED')
 def test_mixed_invalid_rows_remain_invalid_despite_no_trades(self):
  spec,inputs=self.mixed();inputs['rows'][0]=('2026-10-28 10:00:00',100.,98.,99.,100.,10.)
  self.assertEqual(rv._classify(spec,inputs,self.now)['state'],'INVALID')
 def test_public_link_and_auth_capability_do_not_imply_data_rights(self):
  self.assertEqual(rv.review_url('GETTEX:APC'),'https://www.tradingview.com/chart/?symbol=GETTEX%3AAPC')
  for bad in ('NASDAQ:AAPL','GETTEX:APC&token=bad','https://bad'):
   with self.assertRaises(ValueError):rv.review_url(bad)
  c=rv.capability();self.assertEqual(c['mode'],'ASSISTED_ONLY');self.assertIn('AUTH_REQUIRED',c['official_mcp']);self.assertFalse(c['automated_requests'])
 def test_packet_rotation_reaches_tail_beyond_64_without_replacing_open_packet(self):
  now=pd.Timestamp.now(tz='UTC')
  with self.s.connect() as db:
   jobs=[]
   for n in range(200):
    a=str(pd.Timestamp('2026-10-28 07:30')+pd.Timedelta(minutes=n))
    j,_=rv._enqueue(db,rv._spec(self.i['isin'],self.m,'1m',a,a),{},now)
    jobs.append(j)
   jobs.sort(key=lambda j:j['id'])
   for n,j in enumerate(jobs):
    old=dict(j);j.update(status='RESOLVED_NATIVE' if n<192 else 'AWAITING_USER',review_required=n>=192,input_fingerprint='fixture',next_check=(now+pd.Timedelta(hours=3)).isoformat())
    rv._save_job(db,j,old)
  for _ in range(4):rv.turn(self.s,self.i,now)
  packet=self.s.setting(rv.PROGRESS+self.i['isin'])['review_batch']
  self.assertEqual(len(packet['items']),4)
  self.assertTrue(all(i['id'] in {j['id'] for j in jobs[192:]} for i in packet['items']))
  rv.turn(self.s,self.i,now+pd.Timedelta(minutes=1))
  self.assertEqual(packet,self.s.setting(rv.PROGRESS+self.i['isin'])['review_batch'])
 def test_partial_native_rows_do_not_reask_unchanged_completed_visual_review(self):
  now=pd.Timestamp.now(tz='UTC');spec=rv._spec(self.i['isin'],self.m,'15m','2026-10-28 10:00:00','2026-10-28 10:30:00')
  with self.s.connect() as db:
   rv._enqueue(db,spec,{},now)
   db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.i['isin'],'gettex','15m',spec['first'],100,101,99,100,10))
  rv.turn(self.s,self.i,now);p=self.s.setting(rv.PROGRESS+self.i['isin'])['review_batch']
  self.send(p);rv.turn(self.s,self.i,pd.Timestamp.now(tz='UTC'))
  j=self.jobs()['rows'][0]
  self.assertEqual(j['finding']['state'],'NATIVE_ROWS_PRESENT');self.assertTrue(j['finding']['observation_current'])
  self.assertFalse(j['review_required']);self.assertEqual(self.s.setting(rv.PROGRESS+self.i['isin'])['review_batch']['items'],[])

if __name__=='__main__':unittest.main(verbosity=2)
