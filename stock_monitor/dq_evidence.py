"""Read-only projection of existing CP evidence. No duplicate approval registry.

One requested instrument/page, one WAL view. Never creates grants, corrections,
jobs, candles, permissions or remote source calls. Unknown evidence stays explicit.
"""
import json,sqlite3,re
from contextlib import closing
from pathlib import Path

def collect(path,isin,page=0):
 if not isinstance(isin,str) or not re.fullmatch('[A-Z0-9]{12}',isin):raise ValueError('INVALID_EVIDENCE_ISIN')
 if type(page) is not int or not 0<=page<=100000:raise ValueError('INVALID_EVIDENCE_PAGE')
 from cp_controls import read,REVIEWED_PREFIX,REVIEWED_VERSION,_reviewed_binding
 import cp_reconciliation as rv
 import revision_watcher as rw
 from paper_account import ACCOUNT
 import pandas as pd
 now=pd.Timestamp.now(tz='UTC');rows=[];size=20
 with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
  db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
  r=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
  if not r:raise ValueError('INSTRUMENT_REMOVED')
  info=json.loads(r[0])
  # SQL selects only the page, not thousands of old grants/recovery events.
  catalogue="""SELECT key AS evidence_id,'official' AS kind,value AS payload FROM settings WHERE key LIKE ?
   UNION ALL SELECT key,'tv',value FROM settings WHERE key LIKE ? AND json_type(value,'$.observation')='object'
   UNION ALL SELECT s.key||':'||j.key,'owner',j.value FROM settings s,json_each(s.value,'$.entries') j WHERE s.key=?"""
  args=(rw.RETIME_PREFIX+isin+':%',rv.PREFIX+isin+':%',REVIEWED_PREFIX+isin)
  total=db.execute('SELECT count(*) FROM ('+catalogue+')',args).fetchone()[0]
  records=db.execute(catalogue+' ORDER BY evidence_id LIMIT ? OFFSET ?',(*args,size,page*size)).fetchall()
  for key,kind,payload in records:
   value=json.loads(payload);row={'evidence_id':key,'kind':kind,'isin':isin,'status':'UNCONFIRMED'}
   try:
    if kind=='official':
     row.update(identity=value.get('identity'),timeframe='15m',first=value.get('empty_first',value.get('first')),
      end=value.get('empty_end',value.get('end')),mode=value.get('mode'),approved_at=value.get('created_at'),
      fingerprint=value.get('source_digest'),receipt_state=value.get('state'))
     span=pd.Timestamp(value['end'])-pd.Timestamp(value['first'])
     valid=value.get('state')=='APPLIED' and pd.Timedelta(0)<span<=pd.Timedelta(days=1) and rw.retime_current(db,value,now)
     row.update(status='VALID' if valid else 'RECHECK_REQUIRED',reason='EXACT_OFFICIAL_RECEIPT_CURRENT' if valid else 'POLICY_IDENTITY_SOURCE_OR_NATIVE_INPUT_CHANGED')
    elif kind=='tv':
     observation=value['observation'];inputs=rv._input(db,value)
     valid=rv._observation_current(value,inputs)
     row.update(identity=value['identity'],timeframe=value['tf'],first=value['first'],end=value['end'],
      approved_at=observation.get('at'),fingerprint=observation.get('input_fingerprint'),
      current_fingerprint=inputs['fingerprint'],observation_result=observation.get('result'),
      status='VALID_OBSERVATION' if valid and observation.get('identity_confirmed') is True else 'RECHECK_REQUIRED',
      reason='CURRENT_VISUAL_OBSERVATION_NOT_EXCHANGE_NO_TRADES' if valid else 'OBSERVATION_INPUT_CHANGED_NOT_A_GRANT_REVOCATION')
     correction=read(db,rw.RETIME_PREFIX+isin+':'+value['id'],{})
     if correction.get('state')=='APPLIED' and rw.retime_current(db,correction,now):
      row.update(status='SUPERSEDED_BY_OFFICIAL',reason='EXACT_CURRENT_OFFICIAL_RECEIPT_REPLACES_OLD_VISUAL_FINDING',successor_evidence_id=rw.RETIME_PREFIX+isin+':'+value['id'])
    else:
     policy=read(db,REVIEWED_PREFIX+isin,{});problem=value['problem']
     active=(policy.get('approved') is True and policy.get('version')==REVIEWED_VERSION
      and policy.get('account')==ACCOUNT and policy.get('isin')==isin and policy.get('identity')==problem['identity'])
     row.update(identity=problem['identity'],timeframe=problem['tf'],range=problem.get('range'),approved_at=policy.get('authorized_at'))
     valid=active and _reviewed_binding(db,info,problem)==value.get('native_binding')
     row.update(identity=problem['identity'],timeframe=problem['tf'],first=problem.get('range',[None])[0],
      range=problem.get('range'),approved_at=policy.get('authorized_at'),fingerprint=value.get('native_binding'),
      status='VALID_OWNER_RISK' if valid else 'RECHECK_REQUIRED' if active else 'REVOKED',
      reason='OWNER_RISK_IS_NOT_NO_TRADES_OR_EXECUTION_PERMISSION')
   except (ValueError,KeyError,TypeError) as error:row.update(status='RESOLVED_OR_RECLASSIFIED' if str(error)=='REVIEW_NO_LONGER_MISSING' else 'RECHECK_REQUIRED',reason=str(error)[:180])
   # Identity and original classification remain visible even on rejection.
   rows.append(row)
  reports=[{'report_key':key,'timeframe':q.get('base_tf'),'identity':q.get('identity'),
    'state':q.get('state'),'checked_at':q.get('checked_at'),'missing_count':q.get('missing_count'),
    'ranges':q.get('ranges',[])[:200],'ranges_truncated':len(q.get('ranges',[]))>200}
    for key,payload in db.execute("SELECT key,value FROM settings WHERE key LIKE ? AND json_extract(value,'$.identity.venue')='GETTEX' AND json_extract(value,'$.identity.currency')='EUR' ORDER BY key",('data_quality:'+isin+':%',)) for q in [json.loads(payload)]]
  pending=read(db,'cp_assessment_request:'+isin,{})
  assessment=read(db,'cp_eligibility:'+isin,{})
  latest={k:assessment[k] for k in ('checked_at','expires_at','publication_state','error','last_failed_attempt') if k in assessment}
 return {'assessment_request':pending,'latest_saved_assessment':latest,'isin':isin,'name':info.get('name'),'checked_at':now.isoformat(),'page':page,'page_size':size,
  'total_evidence':total,'next_page':page+1 if (page+1)*size<total else None,'evidence':rows,'reports':reports,
  'trade_permission':False,'writes':False,'semantics':'Existing receipts are checked in one read-only view. Observation/owner risk is not exchange NO_TRADES. Factual DQ is preserved; no execution permission or new approval.',
  'as_of_only':True}


def dispatch(app,args):
 """Queue the existing assessor only. No new evidence/grants or candle writes."""
 from concurrent.futures import Future
 from cp_assessment import request
 queue=app.bridge.commands;cid=args['_command_id'];future=Future()
 isin=args['isin']
 def valid():return not getattr(app,'closing',False) and queue.proposal_valid(cid)
 def work():
  if not valid():raise ValueError('EVIDENCE_REQUEST_EXPIRED')
  with app.store.connect() as db:
   if not db.execute('SELECT 1 FROM instruments WHERE isin=?',(isin,)).fetchone():raise ValueError('INSTRUMENT_REMOVED')
  result=request(app.store,isin,valid,explicit=True)
  return {**result,'status':'QUEUED','mechanism':'cp_assessment_request',
   'evidence_route':'/@dq-evidence/'+isin+'/0','grants_changed':False,'candles_changed':False,
   'trade_permission':False,'reason':'Existing receipts revalidated by ordinary all-TF assessment; pending is not READY'}
 def done(result):future.set_result(result)
 def failed(_):future.set_result({'status':'BLOCKED','code':'EVIDENCE_REQUEST_REJECTED','trade_permission':False})
 if app.job(work,done,failed,lane='read',key=('dq-evidence-request',cid)) is False:failed(None)
 return future
