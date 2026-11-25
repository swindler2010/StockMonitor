"""Bounded same-listing revision check inside CP maintenance, never a new worker."""
import json,hashlib
import pandas as pd
from history_backfill import FIELDS,validate,intraday_retention_start
from timeframes import endpoints

IDENTITY=('source','venue','currency','symbol','timezone')

# Standing owner permission; this is a bounded correction on CP's existing
# writer, not a TV importer or a replacement history provider.
RETIME_POLICY='cp_official_timebase_policy'
RETIME_VERSION='official_gettex_conserved_window_v1'
RETIME_PREFIX='cp_official_timebase_revision:'


def _aggregate(rows):
 if not rows:return None
 return [rows[0][1],max(r[2] for r in rows),min(r[3] for r in rows),rows[-1][4],sum(r[5] for r in rows)]


def _native(db,isin,meta,base,first,end):
 return db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? AND ts>=? AND ts<? ORDER BY ts',
  (isin,*[meta[k] for k in IDENTITY[:4]],base,first,end)).fetchall()


def _official_window(db,isin,first,end,now,allow_zero_rows=False,_memo=None):
 """Require complete native publication evidence and exact trade-built OHLCV."""
 import data_quality as dq
 from cp_controls import read
 cache_key=('official_window',isin,first,end,str(now),allow_zero_rows)
 if _memo is not None:
  if not db.in_transaction or not db.execute('PRAGMA query_only').fetchone()[0]:raise ValueError('READ_MEMO_REQUIRES_READONLY_VIEW')
  view=(db,db.execute('PRAGMA data_version').fetchone()[0],db.total_changes)
  if _memo.setdefault('_official_view',view)!=view:raise ValueError('READ_MEMO_VIEW_CHANGED')
  if cache_key in _memo:return _memo[cache_key]
  key=('official_proof',isin)
  if key not in _memo:_memo[key]=read(db,'interval_evidence:'+isin,{})
  proof=_memo[key]
 else:proof=read(db,'interval_evidence:'+isin,{})
 meta=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin')
 if proof.get('isin')!=isin or proof.get('identity')!=meta:raise ValueError('RETIME_OFFICIAL_IDENTITY')
 a,b=pd.Timestamp(first),pd.Timestamp(end)
 if b.tz_localize(meta['timezone']).tz_convert('UTC')+pd.Timedelta(minutes=15)>now:raise ValueError('RETIME_NOT_CLOSED')
 fields=('no_trades','trade_intervals','zero_quantity_intervals','incomplete_publication_intervals')
 scoped={k:v for k,v in proof.items() if k not in (*fields,'reference','checked_at','limits')}
 for field in fields:
  if _memo is None:scoped[field]=[p for p in proof.get(field,[]) if pd.Timestamp(p[0])<b and pd.Timestamp(p[1])>a]
  else:
   key=('official_intervals',isin,field)
   if key not in _memo:_memo[key]=[(pd.Timestamp(p[0]),pd.Timestamp(p[1]),p) for p in proof.get(field,[])]
   scoped[field]=[p for x,y,p in _memo[key] if x<b and y>a]
 if scoped['incomplete_publication_intervals'] or (scoped['zero_quantity_intervals'] and not allow_zero_rows):
  raise ValueError('RETIME_INCOMPLETE_PUBLICATION')
 # Retain hashes of exactly the files covering this window, not later files.
 manifest=proof.get('reference',{}).get('GETTEX:'+a.strftime('%Y%m%d'),{})
 refs={}
 for left in pd.date_range(a.floor('15min'),b,freq='15min',inclusive='left'):
  right=(left+pd.Timedelta(minutes=15)).tz_localize(meta['timezone']).tz_convert('UTC')
  for board in ('munc','mund'):
   name='posttrade.'+right.strftime('%Y%m%d.%H.%M')+'.'+board+'.csv.gz'
   if not isinstance(manifest.get(name),str) or len(manifest[name])!=64:raise ValueError('RETIME_PUBLICATION_PAIR_MISSING')
   refs[name]=manifest[name]
 scoped['reference']={'GETTEX:'+a.strftime('%Y%m%d'):refs}
 authority={**meta,'absence_evidence':{**scoped,'checked_at':proof.get('checked_at')}}
 trades=db.execute('SELECT ts,price,volume FROM trades WHERE isin=? AND ts>=? AND ts<? ORDER BY ts',(isin,first,end)).fetchall()
 if not trades:raise ValueError('RETIME_NO_CONFIRMED_TRADES')
 raw=pd.DataFrame(trades,columns=['ts','price','volume']);raw.index=pd.to_datetime(raw.pop('ts'),format='mixed')
 if raw.index.has_duplicates:raise ValueError('RETIME_AMBIGUOUS_TRADE_ORDER')
 if not (raw['price'].gt(0).all() and raw['volume'].ge(0).all()):raise ValueError('RETIME_INVALID_TRADES')
 for left in pd.date_range(a,b,freq='5min',inclusive='left'):
  right=left+pd.Timedelta(minutes=5)
  part=raw[(raw.index>=left)&(raw.index<right)]
  # A published zero-quantity price observation is not an empty interval.
  # Explicit verified modes may corroborate or retime this native price;
  # it never creates turnover or proves NO_TRADES. Preserve zero evidence.
  zero_row=(allow_zero_rows and not part.empty and part.volume.eq(0).all()
    and any(pd.Timestamp(x)<=left and pd.Timestamp(y)>=right for x,y in scoped['zero_quantity_intervals']))
  if not (dq.no_trades_proof(authority,left,right,now) or dq.known_trade(authority,left,right,now) or zero_row):
   raise ValueError('RETIME_PUBLICATION_COVERAGE_MISSING')
 result={}
 for base in ('5m','15m'):
  built=[]
  for at,part in raw.groupby(pd.Grouper(freq=base.replace('m','min'))):
   if part.empty:continue
   built.append((str(at),float(part.price.iloc[0]),float(part.price.max()),float(part.price.min()),float(part.price.iloc[-1]),float(part.volume.sum())))
  saved=db.execute("SELECT ts,open,high,low,close,volume FROM candles WHERE isin=? AND source='gettex' AND base_tf=? AND ts>=? AND ts<? ORDER BY ts",(isin,base,first,end)).fetchall()
  if saved!=built:raise ValueError('RETIME_OFFICIAL_CANDLES_TRADE_MISMATCH:'+base)
  frame=pd.DataFrame([r[1:] for r in saved],index=pd.to_datetime([r[0] for r in saved]),columns=FIELDS)
  frame.attrs={**authority,'classify_absence':True,'timeframe':base};validate(frame)
  if dq.audit(frame,base,meta,now,check_stale=False)['state']!='READY':raise ValueError('RETIME_OFFICIAL_DQ')
  for left in pd.date_range(a,b,freq=base.replace('m','min'),inclusive='left'):
   if str(left) not in {r[0] for r in saved} and not dq.no_trades_proof(authority,left,left+dq.STEPS[base],now):
    raise ValueError('RETIME_EMPTY_INTERVAL_UNCONFIRMED')
  result[base]=saved
 result=dict(rows=result,trades=trades,proof=scoped)
 if _memo is not None:_memo[cache_key]=result
 return result


def _five_minute_empty_plan(db,job,inputs,now,policy):
 """Prove an empty5m slot using complete exact5m AND15m native windows."""
 from cp_controls import read,digest
 from cp_reconciliation import SOURCE_POLICY
 import data_quality as dq
 from history_service import parent_provenance_key
 sp=read(db,SOURCE_POLICY,{})
 if not (sp.get('approved') is True and sp.get('version')==1 and sp.get('scope')=='registered_stocks' and sp.get('exclusive_venue')=='GETTEX'):return None
 a=pd.Timestamp(job['first']);end=pd.Timestamp(job['end']);before=inputs['neighbors']['before'];after=inputs['neighbors']['after']
 if inputs['rows'] or not pd.Timedelta(0)<end-a<=pd.Timedelta(minutes=15):return None
 authority={**inputs['proof'].get('identity',{}),'absence_evidence':inputs['proof']}
 if not dq.no_trades_proof(authority,a,end,now):return None
 if job.get('observation') and job['observation'].get('result')!='ABSENT':raise ValueError('RETIME_VISUAL_REFERENCE_CONFLICT_OR_CHANGED')
 lo=a.floor('15min');hi=end.ceil('15min');windows=[]
 if before and after and pd.Timestamp(before[0])==a-pd.Timedelta(minutes=5) and pd.Timestamp(after[0])==end:
  windows.append((pd.Timestamp(before[0]).floor('15min'),(pd.Timestamp(after[0])+pd.Timedelta(minutes=5)).ceil('15min')))
 windows.extend(((lo,hi),(lo-pd.Timedelta(minutes=15),hi),(lo,hi+pd.Timedelta(minutes=15))))
 found=False
 for first,last in dict.fromkeys(windows):
  if first.normalize()!=last.normalize() or last-first>pd.Timedelta(minutes=45):continue
  try:
   source=_official_window(db,job['isin'],str(first),str(last),now,allow_zero_rows=True)
   native={base:_native(db,job['isin'],job['identity'],base,str(first),str(last)) for base in ('5m','15m')}
   if any(native[b]!=source['rows'][b] for b in native):continue
   found=True;break
  except (ValueError,KeyError,TypeError):continue
 if not found:return None
 lineage={}
 for b,rows in native.items():
  saved=read(db,parent_provenance_key(job['isin'],job['identity'],b),{})
  lineage[b]={r[0]:saved.get(r[0]) for r in rows}
 return dict(version=RETIME_VERSION,mode='VERIFIED_NATIVE_EMPTY',reviewed_tf='5m',isin=job['isin'],identity=job['identity'],
   first=str(first),end=str(last),empty_first=str(a),empty_end=str(end),job_id=job['id'],policy_digest=digest(policy),source_policy_digest=digest(sp),
   input_fingerprint=inputs['fingerprint'],before=native,source=source,source_digest=digest(source),created_at=now.isoformat(),
   zero_observations_corroborated=True,lineage_before=lineage,
   origin='Complete paired official GETTEX tape; exact full native5m/15m parent windows prove this5m absence; no prices replaced or synthetic observations')


def _reviewed_positive_price_extreme(db,job,inputs,now,policy):
 """A current visual5m discrepancy plus one fully confirmed interior price.

 The quantity was already counted. Restore only an omitted positive trade's
 H/L within one complete unchanged native parent. Never use TV prices as data.
 """
 from cp_controls import read,digest
 from cp_reconciliation import SOURCE_POLICY,_observation_current
 from history_service import parent_provenance_key
 obs=job.get('observation') or {};meta=job['identity'];sp=read(db,SOURCE_POLICY,{})
 if not (job['tf']=='5m' and job['first']==job['last'] and len(inputs['rows'])==1
     and obs.get('result')=='DIFFERENT' and _observation_current(job,inputs)
     and sp.get('approved') is True and sp.get('version')==1 and sp.get('scope')=='registered_stocks' and sp.get('exclusive_venue')=='GETTEX'):return None
 a=pd.Timestamp(job['first']).floor('15min');end=a+pd.Timedelta(minutes=15)
 try:
  source=_official_window(db,job['isin'],str(a),str(end),now)
  if len(source['trades'])>5000:return None
  native={base:_native(db,job['isin'],meta,base,str(a),str(end)) for base in ('5m','15m')}
  if any([r[0] for r in native[b]]!=[r[0] for r in source['rows'][b]] for b in native) or len(native['15m'])!=1:return None
  if _aggregate(native['5m'])!=list(native['15m'][0][1:]):return None
  changed=False
  for original,confirmed in zip(native['5m'],source['rows']['5m']):
   if original==confirmed:continue
   if (original[0]!=job['first'] or original[1]!=confirmed[1] or original[4:]!=confirmed[4:]
       or confirmed[2]<original[2] or confirmed[3]>original[3]):return None
   ticks=[t for t in source['trades'] if pd.Timestamp(original[0])<=pd.Timestamp(t[0])<pd.Timestamp(original[0])+pd.Timedelta(minutes=5)]
   if any(t[2]<=0 for t in ticks):return None
   omitted=[t for t in ticks if not original[3]<=t[1]<=original[2]]
   kept=[t[1] for t in ticks if original[3]<=t[1]<=original[2]]
   if (len(omitted)!=1 or not kept or [kept[0],max(kept),min(kept),kept[-1]]!=list(original[1:5])
       or sum(t[2] for t in ticks)!=original[5]):return None
   changed=True
  if not changed:return None
  old=native['15m'][0];new=source['rows']['15m'][0]
  if old[1]!=new[1] or old[4:]!=new[4:]:return None
  _protect_visual_references(db,job,inputs,dict(first=str(a),end=str(end),source=source))
  lineage={base:{r[0]:read(db,parent_provenance_key(job['isin'],meta,base),{}).get(r[0]) for r in rows} for base,rows in native.items()}
  return dict(version=RETIME_VERSION,mode='VERIFIED_NATIVE_POSITIVE_PRICE_EXTREME',reviewed_tf='5m',isin=job['isin'],identity=meta,
    first=str(a),end=str(end),job_id=job['id'],policy_digest=digest(policy),source_policy_digest=digest(sp),input_fingerprint=inputs['fingerprint'],
    before=native,source=source,source_digest=digest(source),created_at=now.isoformat(),lineage_before=lineage,
    origin='Current5m visual discrepancy; complete paired official tape confirms one omitted positive interior price; exact O/C/V, all native timestamps and parent-child aggregation; original values retained')
 except (KeyError,TypeError,IndexError):return None


def _five_minute_partition_plan(db,job,inputs,now,policy):
 """Reuse the unique conserved native/tape partition, not a guessed5m shift."""
 from cp_controls import read,digest
 from cp_reconciliation import SOURCE_POLICY,_observation_current
 from history_service import parent_provenance_key
 sp=read(db,SOURCE_POLICY,{})
 if not (sp.get('approved') is True and sp.get('version')==1 and sp.get('scope')=='registered_stocks' and sp.get('exclusive_venue')=='GETTEX'):return None
 a=pd.Timestamp(job['first']);end=pd.Timestamp(job['end']);obs=job.get('observation')
 opening_review=bool(job['first']==job['last'] and len(inputs['rows'])==1 and obs and obs.get('result')=='DIFFERENT' and _observation_current(job,inputs))
 if (inputs['rows'] and not opening_review) or not pd.Timedelta(0)<end-a<=pd.Timedelta(minutes=30):return None
 if obs and (obs.get('result') not in ('ABSENT','PRESENT','DIFFERENT') or not _observation_current(job,inputs)):return None
 plan=_reviewed_partition_plan(db,{**job,'first':str(a.floor('15min'))},inputs,now,policy)
 if not plan:plan=_reviewed_session_tail_plan(db,job,inputs,now,policy)
 if not plan:
  if obs and obs['result']=='PRESENT':
   import data_quality as dq
   authority={**inputs['proof'].get('identity',{}),'absence_evidence':inputs['proof']}
   if dq.no_trades_proof(authority,a,end,now):raise ValueError('RETIME_VISUAL_REFERENCE_CONFLICT_OR_CHANGED')
  return None
 if not (pd.Timestamp(plan['first'])<=a<end<=pd.Timestamp(plan['end'])):return None
 present=any(a<=pd.Timestamp(r[0])<end for r in plan['source']['rows']['5m'])
 if obs and ((obs['result']=='ABSENT' and present) or (obs['result']=='PRESENT' and not present)):
  raise ValueError('RETIME_VISUAL_REFERENCE_CONFLICT_OR_CHANGED')
 lineage={}
 for base,rows in plan['before'].items():
  saved=read(db,parent_provenance_key(job['isin'],job['identity'],base),{})
  lineage[base]={r[0]:saved.get(r[0]) for r in rows}
 _protect_visual_references(db,job,inputs,plan)
 return dict(plan,reviewed_tf='5m',source_policy_digest=digest(sp),lineage_before=lineage,
   origin=plan['origin']+'; full current5m review and other current visual references protected')


def _automatic_zero_plan(db,job,inputs,now,policy):
 """An exact, independently proved <=5m zero-price timestamp convention.

 No external observation is claimed. All positive candles stay identical;
 existing flat zero observations map bijectively to the complete tape.
 """
 from cp_controls import read,digest
 from cp_reconciliation import SOURCE_POLICY
 from history_service import parent_provenance_key
 sp=read(db,SOURCE_POLICY,{})
 if not (job['tf']=='5m' and not job.get('observation') and not inputs['rows']
     and sp.get('approved') is True and sp.get('version')==1 and sp.get('scope')=='registered_stocks' and sp.get('exclusive_venue')=='GETTEX'):return None
 a=pd.Timestamp(job['first']).floor('15min');meta=job['identity']
 from eur_listings import stored_identity
 if not all(stored_identity(db,inputs['info'],meta,b) for b in ('5m','15m')):return None
 for width in (30,45,60):
  for offset in range(0,width,15):
   left=a-pd.Timedelta(minutes=offset);end=left+pd.Timedelta(minutes=width)
   if left.normalize()!=end.normalize() or not left<=pd.Timestamp(job['first'])<pd.Timestamp(job['end'])<=end:continue
   try:
    source=_official_window(db,job['isin'],str(left),str(end),now,allow_zero_rows=True)
    native={b:_native(db,job['isin'],meta,b,str(left),str(end)) for b in ('5m','15m')}
    if any(not native[b] or _aggregate(native[b])!=_aggregate(source['rows'][b]) for b in native):continue
    if not 1<=len(native['5m'])<=9 or len(source['trades'])>5000:continue
    if {str(pd.Timestamp(r[0]).floor('15min')) for r in native['5m']}!={r[0] for r in native['15m']}:continue
    if any(_aggregate([r for r in native['5m'] if pd.Timestamp(p[0])<=pd.Timestamp(r[0])<pd.Timestamp(p[0])+pd.Timedelta(minutes=15)])!=list(p[1:]) for p in native['15m']):continue
    if [r for r in native['5m'] if r[5]>0]!=[r for r in source['rows']['5m'] if r[5]>0]:continue
    old=[r for r in native['5m'] if r[5]==0];actual=[r for r in source['rows']['5m'] if r[5]==0]
    if not old or len(old)!=len(actual) or any(len(set(r[1:5]))!=1 for r in old+actual):continue
    used=set();mapping={}
    for row in old:
     if row in actual:used.add(row[0]);mapping[row[0]]=row[0]
    valid=True
    for row in old:
     if row[0] in mapping:continue
     matches=[r for r in actual if r[0] not in used and r[1:]==row[1:] and pd.Timedelta(0)<=pd.Timestamp(row[0])-pd.Timestamp(r[0])<=pd.Timedelta(minutes=5)]
     if len(matches)!=1:valid=False;break
     used.add(matches[0][0]);mapping[row[0]]=matches[0][0]
    if not valid or len(used)!=len(actual) or not any(k!=v for k,v in mapping.items()):continue
    partition=_trade_partition(native['5m'],source['trades'],max_delay=5)
    if partition is None:continue
    stamps={b:{r[0] for r in source['rows'][b]} for b in native};moves=[]
    for row in native['5m']:
     if row[0] not in stamps['5m']:moves.append(dict(base_tf='5m',old_at=row[0],new_at=mapping[row[0]],ohlcv=list(row[1:]),binding='unique existing flat zero observation shifted at most5minutes; all positive candles unchanged'))
    for row in native['15m']:
     if row[0] in stamps['15m']:continue
     children=[r for r in native['5m'] if pd.Timestamp(row[0])<=pd.Timestamp(r[0])<pd.Timestamp(row[0])+pd.Timedelta(minutes=15)]
     targets={str(pd.Timestamp(mapping[r[0]]).floor('15min')) for r in children if r[0] in mapping}
     if row[5]!=0 or len(set(row[1:5]))!=1 or len(children)!=sum(r[0] in mapping for r in children) or not targets<=stamps['15m'] or not targets:valid=False;break
     moves.append(dict(base_tf='15m',old_at=row[0],new_at=min(targets),ohlcv=list(row[1:]),binding='exact original zero parent recomposed only from its bijectively matched zero children',recomposed_into=sorted(targets)))
    if not valid:continue
    plan=dict(first=str(left),end=str(end),source=source)
    _protect_visual_references(db,job,inputs,plan)
    lineage={b:{r[0]:read(db,parent_provenance_key(job['isin'],meta,b),{}).get(r[0]) for r in rows} for b,rows in native.items()}
    return dict(version=RETIME_VERSION,mode='VERIFIED_NATIVE_TRADE_PARTITION',reviewed_tf='5m',isin=job['isin'],identity=meta,
      first=str(left),end=str(end),job_id=job['id'],policy_digest=digest(policy),source_policy_digest=digest(sp),
      input_fingerprint=inputs['fingerprint'],before=native,source=source,source_digest=digest(source),created_at=now.isoformat(),
      native_trade_partition=partition,relocations=moves,lineage_before=lineage,zero_observations_corroborated=True,
      automatic_zero_timebase=True,automatic_zero_version=1,zero_timestamp_mapping=mapping,
      origin='Complete paired primary GETTEX proof; unique full event partition and bijective existing flat zero observations delayed<=5minutes; every positive5m candle unchanged, exact original parents and conserved whole5m/15mOHLCV; no TV observation claimed')
   except (ValueError,KeyError,TypeError):continue
 return None


def _partition_co_reviews(db,job,inputs,plan):
 """Bind disjoint current DIFFERENT diagnoses to one fully proved partition.

 A unique conserved partition may reconcile disjoint diagnoses of the same
 TF together. It never accepts contradictory presence facts or overlap.
 """
 from cp_reconciliation import PREFIX,_input,_observation_current
 from cp_controls import digest
 if not (plan.get('sampled_positive_opening_burst') or (job['tf']=='15m' and plan.get('native_partition_proved'))) or (job.get('observation') or {}).get('result')!='DIFFERENT' or not _observation_current(job,inputs):return {}
 prefix=PREFIX+job['isin']+':'
 rows=db.execute("SELECT value FROM settings WHERE key>=? AND key<? AND json_extract(value,'$.observation.result')='DIFFERENT' AND json_extract(value,'$.first')>=? AND json_extract(value,'$.end')<=? LIMIT 9",(prefix,prefix+'\uffff',plan['first'],plan['end'])).fetchall()
 if len(rows)>8:return {}
 result={}
 for raw, in rows:
  review=json.loads(raw)
  if review['id']==job['id'] or review.get('identity')!=job['identity'] or review.get('tf')!=job['tf']:continue
  if not (review['end']<=job['first'] or job['end']<=review['first']):continue
  checked=_input(db,review)
  if not _observation_current(review,checked) or not review['observation'].get('identity_confirmed'):continue
  actual=[r for r in plan['source']['rows'][job['tf']] if review['first']<=r[0]<review['end']]
  original=[r for r in plan['before'][job['tf']] if review['first']<=r[0]<review['end']]
  if checked['rows']!=original or original==actual:continue
  result[review['id']]=dict(job_digest=digest(review),input_fingerprint=checked['fingerprint'],first=review['first'],end=review['end'])
 return result


def _protect_visual_references(db,job,inputs,plan):
 """Never retime a different current TV-confirmed scope or its bound context."""
 from cp_reconciliation import PREFIX,_input,_observation_current
 meta=job['identity'];prefix=PREFIX+job['isin']+':'
 rows=db.execute("""SELECT value FROM settings WHERE key>=? AND key<?
   AND json_extract(value,'$.observation.result') IN ('ABSENT','PRESENT','DIFFERENT','ZERO_VOLUME')
   AND json_extract(value,'$.tf') IN ('5m','15m')
   AND json_extract(value,'$.identity.source')=? AND json_extract(value,'$.identity.symbol')=?
   AND json_extract(value,'$.identity.venue')=? AND json_extract(value,'$.identity.currency')=?
   AND json_extract(value,'$.identity.timezone')=?
   AND ((json_extract(value,'$.first')<? AND json_extract(value,'$.end')>?)
     OR (json_extract(value,'$.native_neighbors.before[0]')>=? AND json_extract(value,'$.native_neighbors.before[0]')<?)
     OR (json_extract(value,'$.native_neighbors.after[0]')>=? AND json_extract(value,'$.native_neighbors.after[0]')<?)) LIMIT 33""",
   (prefix,prefix+'\uffff',*[meta[k] for k in ('source','symbol','venue','currency','timezone')],plan['end'],plan['first'],plan['first'],plan['end'],plan['first'],plan['end'])).fetchall()
 if len(rows)>32:raise ValueError('RETIME_VISUAL_REFERENCE_WINDOW_TOO_DENSE')
 own=job.get('observation') or {};own_at=pd.Timestamp(own['at']) if own.get('at') else None
 for raw, in rows:
  review=json.loads(raw)
  if review['id']==job['id']:continue
  checked=_input(db,review)
  if not _observation_current(review,checked):continue
  obs=review['observation'];base=review['tf'];canonical=plan['source']['rows'][base]
  binding=plan.get('co_review_inputs',{}).get(review['id'])
  if binding and obs['result']=='DIFFERENT':
   from cp_controls import digest
   if digest(review)==binding['job_digest'] and checked['fingerprint']==binding['input_fingerprint']:continue
  # A proved partition may update only the context of a disjoint diagnosis;
  # it does not change that diagnosis's subject rows or contradict its fact.
  # The unchanged subject can reuse its observation only through the exact
  # current primary-receipt binding below, never by moving its timestamp.
  disjoint=review['end']<=plan['first'] or plan['end']<=review['first']
  if (obs['result']=='DIFFERENT' and disjoint and plan.get('native_partition_proved')
      and own.get('result')=='DIFFERENT' and own_at is not None
      and obs.get('at')):continue
  present=any(review['first']<=r[0]<review['end'] for r in canonical)
  covered=plan['first']<=review['first'] and review['end']<=plan['end']
  if (obs['result']=='ABSENT' and present
      or obs['result']=='PRESENT' and covered and not present
      or obs['result']=='ZERO_VOLUME' and any(review['first']<=r[0]<review['end'] and r[5]>0 for r in canonical)
      or obs['result']=='DIFFERENT'):
   raise ValueError('RETIME_OTHER_CURRENT_TV_REFERENCE_CONFLICT')
  if obs['result']!='ABSENT':continue
  # An absence review binds its surrounding native context. A newer exact
  # DIFFERENT diagnosis can supersede that context, never its absence fact.
  newer=own.get('result')=='DIFFERENT' and own_at is not None and obs.get('at') and own_at>pd.Timestamp(obs['at'])
  if newer:continue
  expected={r[0]:list(r[1:]) for r in canonical}
  for old in checked['neighbors'].values():
   if old and plan['first']<=old[0]<plan['end'] and expected.get(old[0])!=list(old[1:]):
    raise ValueError('RETIME_CURRENT_TV_BOUND_CONTEXT_CHANGED')


def empty_native_span(job,inputs):
 """Exact two-sided hole, or the final slot at a verified session boundary."""
 from timeframes import schedule
 a=pd.Timestamp(job['first']);before=inputs['neighbors']['before'];after=inputs['neighbors']['after']
 if (job['tf']!='15m' or job['last']!=str(a) or inputs['rows'] or not before
     or pd.Timestamp(before[0])!=a-pd.Timedelta(minutes=15) or before[0][:10]!=str(a)[:10]):return None
 end=a+pd.Timedelta(minutes=15)
 if after and pd.Timestamp(after[0])==end and after[0][:10]==str(a)[:10]:
  return before[0],str(end+pd.Timedelta(minutes=15)),{before[0],after[0]}
 day=schedule(job['identity'],a,a)
 if (len(day)==1 and end.tz_localize(job['identity']['timezone']).tz_convert('UTC')==day.close.iloc[0]
     and (not after or pd.Timestamp(after[0])>=end)):
  return before[0],str(end),{before[0]}
 return None


def _trade_partition(native,trades,max_delay=15):
 """Unique ordered tape partition reproducing every original 5m OHLCV."""
 if not native or len(native)>9 or len(trades)>5000:return None
 solutions=[];attempts=0
 def visit(i,j,parts):
  nonlocal attempts
  if len(solutions)>1:return
  if i==len(native):
   if j==len(trades):solutions.append(parts)
   return
  row=native[i];at=pd.Timestamp(row[0]);volume=0.;prices=[]
  for k in range(j,len(trades)):
   attempts+=1
   if attempts>20000:raise ValueError('RETIME_PARTITION_COMPLEXITY_LIMIT')
   stamp,price,qty=trades[k];t=pd.Timestamp(stamp)
   if not at-pd.Timedelta(minutes=max_delay)<=t<at+pd.Timedelta(minutes=5):break
   volume+=qty;prices.append(price)
   if volume>row[5]:break
   if [prices[0],max(prices),min(prices),prices[-1],volume]==list(row[1:]):
    visit(i+1,k+1,parts+[(j,k+1)])
 visit(0,0,[])
 return solutions[0] if len(solutions)==1 else None


def _sampled_price_partition(native,trades,max_delay=45):
 """Unique full-quantity partition with one omitted positive interior price.

 No event quantity is removed. This models a sampled native extreme only;
 the caller must conserve the whole window and verify every native parent.
 """
 if not native or len(native)>9 or len(trades)>5000:return None
 solutions=[];attempts=0
 def visit(i,j,parts,omitted):
  nonlocal attempts
  if len(solutions)>1:return
  if i==len(native):
   if j==len(trades) and len(omitted)==1:solutions.append((parts,omitted))
   return
  row=native[i];at=pd.Timestamp(row[0]);volume=0.;prices=[]
  for k in range(j,len(trades)):
   attempts+=1
   if attempts>20000:raise ValueError('RETIME_PARTITION_COMPLEXITY_LIMIT')
   stamp,price,qty=trades[k];t=pd.Timestamp(stamp)
   if not at-pd.Timedelta(minutes=max_delay)<=t<at+pd.Timedelta(minutes=5):break
   volume+=qty;prices.append(price)
   if volume>row[5]:break
   if volume!=row[5]:continue
   expected=list(row[1:5]);actual=[prices[0],max(prices),min(prices),prices[-1]]
   if actual==expected:visit(i+1,k+1,parts+[(j,k+1)],omitted)
   elif not omitted:
    for x in range(1,len(prices)-1):
     if trades[j+x][2]<=0 or row[3]<=prices[x]<=row[2]:continue
     kept=prices[:x]+prices[x+1:]
     if kept and [kept[0],max(kept),min(kept),kept[-1]]==expected:
      visit(i+1,k+1,parts+[(j,k+1)],[dict(native_at=row[0],event_index=j+x,event=trades[j+x])])
 visit(0,0,[],[])
 return solutions[0] if len(solutions)==1 else None


def _opening_price_partition(native,trades,max_delay=45):
 """One short identical-price opening burst, with every quantity retained."""
 if not native or len(native)>9 or len(trades)>5000:return None
 solutions=[];attempts=0
 def visit(i,j,parts,omitted):
  nonlocal attempts
  if len(solutions)>1:return
  if i==len(native):
   if j==len(trades) and omitted:solutions.append((parts,omitted))
   return
  row=native[i];at=pd.Timestamp(row[0]);volume=0.;prices=[]
  for k in range(j,len(trades)):
   attempts+=1
   if attempts>20000:raise ValueError('RETIME_PARTITION_COMPLEXITY_LIMIT')
   stamp,price,qty=trades[k];t=pd.Timestamp(stamp)
   if not at-pd.Timedelta(minutes=max_delay)<=t<at+pd.Timedelta(minutes=5):break
   volume+=qty;prices.append(price)
   if volume>row[5]:break
   if volume!=row[5]:continue
   expected=list(row[1:5]);actual=[prices[0],max(prices),min(prices),prices[-1]]
   if actual==expected:visit(i+1,k+1,parts+[(j,k+1)],omitted)
   elif not omitted and not row[3]<=prices[0]<=row[2]:
    # A bounded positive burst at one price, never a missing quantity or
    # arbitrary trimming. Its end is uniquely the first different price.
    count=next((n for n,p in enumerate(prices) if p!=prices[0]),len(prices))
    if not 1<=count<=3 or count==len(prices):continue
    burst=trades[j:j+count];kept=prices[count:]
    if any(t[2]<=0 for t in burst) or len({pd.Timestamp(t[0]).floor('5min') for t in burst})!=1:continue
    if [kept[0],max(kept),min(kept),kept[-1]]==expected:
     visit(i+1,k+1,parts+[(j,k+1)],[dict(native_at=row[0],event_index=j+n,event=trades[j+n]) for n in range(count)])
 visit(0,0,[],[])
 return solutions[0] if len(solutions)==1 else None


def _reviewed_session_tail_plan(db,job,inputs,now,policy):
 """Recover a fully published session tail after a unique native prefix.

 This is explicitly missing-source-data recovery, not conserved retiming.
 Every old event/quantity must map first; only a positive final <=15m suffix
 may be new. Two unchanged leading children and exact parents anchor it.
 """
 from cp_controls import read,digest
 from cp_reconciliation import SOURCE_POLICY,_observation_current
 from history_service import parent_provenance_key
 from timeframes import schedule
 sp=read(db,SOURCE_POLICY,{});obs=job.get('observation') or {};a=pd.Timestamp(job['first'])
 if not (job['tf']=='5m' and not inputs['rows'] and obs.get('result') in ('PRESENT','DIFFERENT') and _observation_current(job,inputs)
     and sp.get('approved') is True and sp.get('version')==1 and sp.get('scope')=='registered_stocks' and sp.get('exclusive_venue')=='GETTEX'):return None
 sessions=schedule(job['identity'],a,a)
 if len(sessions)!=1:return None
 end=sessions.close.iloc[0].tz_convert(job['identity']['timezone']).tz_localize(None)
 if not pd.Timedelta(0)<end-a<=pd.Timedelta(minutes=60):return None
 for width in (30,45,60):
  left=end-pd.Timedelta(minutes=width)
  if not left<=a<pd.Timestamp(job['end'])<=end:continue
  try:
   source=_official_window(db,job['isin'],str(left),str(end),now,allow_zero_rows=True);ticks=source['trades']
   native={b:_native(db,job['isin'],job['identity'],b,str(left),str(end)) for b in ('5m','15m')}
   if not 2<=len(native['5m'])<=9 or not native['15m'] or len(ticks)>5000:continue
   if {str(pd.Timestamp(r[0]).floor('15min')) for r in native['5m']}!={r[0] for r in native['15m']}:continue
   if any(_aggregate([r for r in native['5m'] if pd.Timestamp(p[0])<=pd.Timestamp(r[0])<pd.Timestamp(p[0])+pd.Timedelta(minutes=15)])!=list(p[1:]) for p in native['15m']):continue
   if not {r[0] for r in native['15m']}<={r[0] for r in source['rows']['15m']}:continue
   anchored=0
   actual={r[0]:r for r in source['rows']['5m']}
   if not {r[0] for r in native['5m']}<=set(actual):continue
   for row in native['5m']:
    if row[0]>=job['first'] or actual.get(row[0])!=row:break
    anchored+=1
   if anchored<2:continue
   volume=sum(r[5] for r in native['5m']);running=0.;cuts=[]
   for k,tick in enumerate(ticks):
    running+=tick[2]
    if running==volume and 0<len(ticks)-k-1<=64 and all(t[2]>0 for t in ticks[k+1:]):cuts.append(k+1)
    if running>volume:break
   if len(cuts)!=1:continue
   cut=cuts[0];tail=ticks[cut:]
   if pd.Timestamp(tail[0][0])<end-pd.Timedelta(minutes=15):continue
   partition=_trade_partition(native['5m'],ticks[:cut],max_delay=15)
   if partition is None:continue
   _protect_visual_references(db,job,inputs,dict(first=str(left),end=str(end),source=source))
   lineage={b:{r[0]:read(db,parent_provenance_key(job['isin'],job['identity'],b),{}).get(r[0]) for r in rows} for b,rows in native.items()}
   return dict(version=RETIME_VERSION,mode='VERIFIED_NATIVE_SESSION_TAIL',isin=job['isin'],identity=job['identity'],
     first=str(left),end=str(end),job_id=job['id'],policy_digest=digest(policy),source_policy_digest=digest(sp),
     input_fingerprint=inputs['fingerprint'],before=native,source=source,source_digest=digest(source),created_at=now.isoformat(),
     native_trade_partition=partition,recovered_session_tail=tail,session_tail_version=1,lineage_before=lineage,
     zero_observations_corroborated=True,origin='Current exact visual review; complete paired GETTEX final session files; two exact leading children, unique full native prefix within15m, exact original parents; only positive final15m source events recovered, original values retained')
  except ValueError as error:
   if str(error) in ('RETIME_OTHER_CURRENT_TV_REFERENCE_CONFLICT','RETIME_CURRENT_TV_BOUND_CONTEXT_CHANGED','RETIME_VISUAL_REFERENCE_WINDOW_TOO_DENSE'):raise
  except (KeyError,TypeError,IndexError):continue
 return None


def _reviewed_partition_plan(db,job,inputs,now,policy):
 """A real timestamp redistribution, proved by every original native value."""
 from cp_controls import digest
 from cp_reconciliation import _observation_current,SOURCE_POLICY
 from cp_controls import read
 a=pd.Timestamp(job['first']);meta=job['identity'];obs=job.get('observation') or {}
 reviewed=bool(job['tf'] in ('5m','15m') and obs.get('result') in ('PRESENT','DIFFERENT') and _observation_current(job,inputs))
 sp=read(db,SOURCE_POLICY,{})
 reviewed=reviewed and sp.get('approved') is True and sp.get('version')==1 and sp.get('scope')=='registered_stocks' and sp.get('exclusive_venue')=='GETTEX'
 widths=(30,45,60,75) if reviewed and obs.get('result')=='DIFFERENT' else (30,45,60)
 windows=[(a-pd.Timedelta(minutes=offset),a+pd.Timedelta(minutes=width-offset)) for width in widths for offset in range(0,width,15)]
 for left,end in windows:
  if left.normalize()!=end.normalize() or not (left<=pd.Timestamp(job.get('interval_start',job['first']))<pd.Timestamp(job['end'])<=end):continue
  opening=None
  try:
   source=_official_window(db,job['isin'],str(left),str(end),now,allow_zero_rows=True)
   native={base:_native(db,job['isin'],meta,base,str(left),str(end)) for base in ('5m','15m')}
   if any(not native[base] or _aggregate(native[base])!=_aggregate(source['rows'][base]) for base in native):continue
   if not reviewed and [r[0] for r in native['15m']]!=[r[0] for r in source['rows']['15m']]:continue
   if native==source['rows']:continue
   # The original parent must itself be exactly built from its native children.
   if any(_aggregate([r for r in native['5m'] if pd.Timestamp(p[0])<=pd.Timestamp(r[0])<pd.Timestamp(p[0])+pd.Timedelta(minutes=15)])!=list(p[1:]) for p in native['15m']):continue
   partition=_trade_partition(native['5m'],source['trades'],max_delay=45 if reviewed else 15);sampled=None;opening=None
   if partition is None and reviewed and obs.get('result')=='DIFFERENT':
    sampled=_sampled_price_partition(native['5m'],source['trades'])
    if sampled:partition=sampled[0]
    elif job['tf']=='5m':
     opening=_opening_price_partition(native['5m'],source['trades'])
     if opening:partition=opening[0]
   if partition is None:continue
   official_stamps={r[0] for r in source['rows']['5m']};moves=[]
   if reviewed:
    # A vanished native parent is deletable only as a unique exact parent
    # relocation into an empty native slot, never a discarded aggregate.
    old_stamps={r[0] for r in native['15m']};new_stamps={r[0] for r in source['rows']['15m']};used=set();invalid=False
    for parent in native['15m']:
     if parent[0] in new_stamps:continue
     matches=[r for r in source['rows']['15m'] if r[0] not in old_stamps and list(r[1:])==list(parent[1:]) and pd.Timedelta(0)<=pd.Timestamp(parent[0])-pd.Timestamp(r[0])<=pd.Timedelta(minutes=45)]
     if len(matches)!=1 or matches[0][0] in used:
      # A flat zero parent can be recomposed into an existing real parent
      # only through its own distinct zero-quantity source events. Every
      # original child/parent and the full tape were already conserved.
      children=[(r,edge) for r,edge in zip(native['5m'],partition) if pd.Timestamp(parent[0])<=pd.Timestamp(r[0])<pd.Timestamp(parent[0])+pd.Timedelta(minutes=15)]
      events=[(k,source['trades'][k]) for r,(start,stop) in children for k in range(start,stop)]
      target={str(pd.Timestamp(t[0]).floor('15min')) for k,t in events}
      positive_single=(parent[5]>0 and len(children)==1 and len(target)==1 and events
        and _aggregate([(t[0],t[1],t[1],t[1],t[1],t[2]) for k,t in events])==list(parent[1:])
        and target<=new_stamps and all(pd.Timedelta(0)<=pd.Timestamp(parent[0])-pd.Timestamp(t)<=pd.Timedelta(minutes=15) for t in target))
      if positive_single:
       moves.append(dict(base_tf='15m',old_at=parent[0],new_at=min(target),ohlcv=list(parent[1:]),binding='unique exact single-child positive events recomposed in actual source parent',recomposed_into=sorted(target),child_event_ranges=[dict(old_at=r[0],start=edge[0],stop=edge[1]) for r,edge in children]));continue
      if (parent[5]!=0 or len(set(parent[1:5]))!=1 or not events or len({k for k,t in events})!=len(events)
          or any(t[1]!=parent[1] or t[2]!=0 for k,t in events) or not target<=new_stamps
          or any(not pd.Timedelta(0)<=pd.Timestamp(parent[0])-pd.Timestamp(t)<=pd.Timedelta(minutes=45) for t in target)):
       invalid=True;break
      moves.append(dict(base_tf='15m',old_at=parent[0],new_at=min(target),ohlcv=list(parent[1:]),binding='unique zero-quantity child events recomposed in actual source parents',recomposed_into=sorted(target),child_event_ranges=[dict(old_at=r[0],start=edge[0],stop=edge[1]) for r,edge in children]));continue
     used.add(matches[0][0]);moves.append(dict(base_tf='15m',old_at=parent[0],new_at=matches[0][0],ohlcv=list(parent[1:]),binding='unique exact parent within full native trade partition'))
    if invalid:continue
   for row,(start,stop) in zip(native['5m'],partition):
    if row[0] not in official_stamps:
     target=str(pd.Timestamp(source['trades'][start][0]).floor('5min'))
     moves.append(dict(base_tf='5m',old_at=row[0],new_at=target,ohlcv=list(row[1:]),
       binding='exact unique ordered official trade partition'))
   lineage={}
   if reviewed:
    guard_plan=dict(first=str(left),end=str(end),source=source,before=native,native_partition_proved=True,**(dict(sampled_positive_opening_burst=opening[1]) if opening else {}))
    co_reviews=_partition_co_reviews(db,job,inputs,guard_plan);guard_plan['co_review_inputs']=co_reviews
    _protect_visual_references(db,job,inputs,guard_plan)
    from history_service import parent_provenance_key
    for base,rows in native.items():
     saved=read(db,parent_provenance_key(job['isin'],meta,base),{})
     lineage[base]={r[0]:saved.get(r[0]) for r in rows}
   return dict(version=RETIME_VERSION,mode='VERIFIED_NATIVE_TRADE_PARTITION',isin=job['isin'],identity=meta,
     **(dict(reviewed_window_partition=True,source_policy_digest=digest(sp),max_native_delay_minutes=45,lineage_before=lineage) if reviewed else {}),
     first=str(left),end=str(end),job_id=job['id'],policy_digest=digest(policy),input_fingerprint=inputs['fingerprint'],
     before=native,source=source,source_digest=digest(source),created_at=now.isoformat(),relocations=moves,
     native_trade_partition=partition,zero_observations_corroborated=True,
     **(dict(sampled_positive_interior_price=sampled[1],sampled_price_version=1) if sampled else {}),
     **(dict(sampled_positive_opening_burst=opening[1],opening_burst_version=1) if opening else {}),
     **(dict(co_review_inputs=co_reviews) if reviewed and co_reviews else {}),
     origin=('Current DIFFERENT review; complete paired official tape uniquely partitions every native quantity and reproduces native prices except one positive interior extreme already counted in volume; exact native parents and conserved full5m/15m window' if sampled else 'Current DIFFERENT review; complete paired tape uniquely reproduces all native quantities and closes with one identical positive opening-price burst omitted only from sampling; exact native parents and conserved5m/15m window' if opening else 'Current visual discrepancy; complete official tape uniquely reproduces all original OHLCV with bounded native timestamp delay; conserved 5m/15m window'))
  except ValueError as error:
   if opening and str(error)=='RETIME_OTHER_CURRENT_TV_REFERENCE_CONFLICT':continue # try a whole jointly reviewed window, never ignore a conflicting fact
   if str(error) in ('RETIME_OTHER_CURRENT_TV_REFERENCE_CONFLICT','RETIME_CURRENT_TV_BOUND_CONTEXT_CHANGED','RETIME_VISUAL_REFERENCE_WINDOW_TOO_DENSE'):raise
   continue
  except (KeyError,TypeError):continue
 return None


def _reviewed_positive_extreme(db,job,inputs,now,policy):
 """Restore a missing positive tick at the opening of a native child bar.

 The reviewed 15m open/close/volume and the following neighbour stay exact.
 Only a child's missing first tick and its resulting H/L can change. This is
 an explicit price repair, not a conserved-timebase or historical-risk grant.
 """
 from cp_controls import digest
 a=pd.Timestamp(job['first']);end=a+pd.Timedelta(minutes=30);meta=job['identity']
 if a.normalize()!=end.normalize():return None
 try:
  source=_official_window(db,job['isin'],str(a),str(end),now)
  native={base:_native(db,job['isin'],meta,base,str(a),str(end)) for base in ('5m','15m')}
  if len(native['15m'])!=2 or native['15m'][1]!=source['rows']['15m'][-1]:return None
  for base in native:
   if [r[0] for r in native[base]]!=[r[0] for r in source['rows'][base]]:return None
  changed_child=False
  for base,old in native.items():
   for original,confirmed in zip(old,source['rows'][base]):
    if original==confirmed:continue
    if (pd.Timestamp(original[0])>=a+pd.Timedelta(minutes=15)
        or original[4:]!=confirmed[4:] or confirmed[2]<original[2] or confirmed[3]>original[3]):return None
    if base=='15m':
     if original[1]!=confirmed[1]:return None
     continue
    ticks=[t for t in source['trades'] if pd.Timestamp(original[0])<=pd.Timestamp(t[0])<pd.Timestamp(original[0])+pd.Timedelta(minutes=5)]
    # Exactly one initial positive tick was omitted in prices, not in volume.
    # All remaining published values must reproduce the complete native bar.
    if len(ticks)<2 or ticks[0][2]<=0 or any(t[2]<=0 for t in ticks):return None
    rest=[t[1] for t in ticks[1:]]
    if list(original[1:5])!=[rest[0],max(rest),min(rest),rest[-1]]:return None
    if sum(t[2] for t in ticks)!=original[5] or confirmed[1]!=ticks[0][1]:return None
    if any(original[k]!=confirmed[k] and confirmed[k]!=ticks[0][1] for k in (1,2,3)):return None
    changed_child=True
  if not changed_child:return None
  if any(_aggregate([r for r in native['5m'] if pd.Timestamp(p[0])<=pd.Timestamp(r[0])<pd.Timestamp(p[0])+pd.Timedelta(minutes=15)])!=list(p[1:]) for p in native['15m']):return None
  return dict(version=RETIME_VERSION,mode='VERIFIED_NATIVE_POSITIVE_OPENING_EXTREME',isin=job['isin'],identity=meta,
    first=str(a),end=str(end),job_id=job['id'],policy_digest=digest(policy),input_fingerprint=inputs['fingerprint'],
    before=native,source=source,source_digest=digest(source),created_at=now.isoformat(),
    origin='Current visual discrepancy; complete official positive first tick omitted only from native child prices, all quantities and reviewed 15m open/close preserved; original OHLCV retained')
 except (ValueError,KeyError,TypeError,IndexError):return None


def _reviewed_closing_tick(db,job,inputs,now,policy):
 """Recover one omitted final published price/quantity, never arbitrary values.

 A current visual discrepancy is only the trigger. The full official tape,
 paired publication coverage and both native TF must prove that precisely the
 LAST tick is missing. Every preceding tick, timestamp and OHLCV stays exact.
 """
 from cp_controls import read,digest
 from cp_reconciliation import SOURCE_POLICY
 source_policy=read(db,SOURCE_POLICY,{})
 if not (source_policy.get('approved') is True and source_policy.get('version')==1
     and source_policy.get('scope')=='registered_stocks'
     and source_policy.get('exclusive_venue')=='GETTEX'):return None
 a=pd.Timestamp(job['first']);end=a+pd.Timedelta(minutes=15);meta=job['identity']
 try:
  source=_official_window(db,job['isin'],str(a),str(end),now)
  ticks=source['trades']
  if len(ticks)<2 or pd.Timestamp(ticks[-1][0]).floor('5min')!=pd.Timestamp(ticks[-2][0]).floor('5min'):return None
  native={base:_native(db,job['isin'],meta,base,str(a),str(end)) for base in ('5m','15m')}
  raw=pd.DataFrame(ticks[:-1],columns=['ts','price','volume']);raw.index=pd.to_datetime(raw.pop('ts'),format='mixed')
  for base,old in native.items():
   expected=[(str(t),float(g.price.iloc[0]),float(g.price.max()),float(g.price.min()),float(g.price.iloc[-1]),float(g.volume.sum()))
     for t,g in raw.groupby(pd.Grouper(freq=base.replace('m','min'))) if not g.empty]
   if old!=expected or [r[0] for r in old]!=[r[0] for r in source['rows'][base]]:return None
  if native==source['rows']:return None
  return dict(version=RETIME_VERSION,mode='VERIFIED_NATIVE_CLOSING_TICK',isin=job['isin'],identity=meta,
    first=str(a),end=str(end),job_id=job['id'],policy_digest=digest(policy),source_policy_digest=digest(source_policy),
    input_fingerprint=inputs['fingerprint'],before=native,source=source,source_digest=digest(source),
    created_at=now.isoformat(),recovered_closing_tick=ticks[-1],
    origin='Current visual discrepancy; complete paired official GETTEX tape exactly reproduces every native 5m/15m value when only its final published tick is excluded; original values retained')
 except (ValueError,KeyError,TypeError,IndexError):return None


def _reviewed_price_samples(db,job,inputs,now,policy):
 """Full quantities, but one opening price and at most one extreme not sampled.

 Only the first 5m child may differ. Its unchanged close and full quantity
 must be exact; omitting the independently published first positive price
 and at most one isolated interior extreme must reproduce ALL old prices.
 """
 from cp_controls import read,digest
 from cp_reconciliation import SOURCE_POLICY
 sp=read(db,SOURCE_POLICY,{})
 if not (sp.get('approved') is True and sp.get('version')==1
     and sp.get('scope')=='registered_stocks' and sp.get('exclusive_venue')=='GETTEX'):return None
 a=pd.Timestamp(job['first']);end=a+pd.Timedelta(minutes=15);meta=job['identity']
 try:
  source=_official_window(db,job['isin'],str(a),str(end),now)
  native={base:_native(db,job['isin'],meta,base,str(a),str(end)) for base in ('5m','15m')}
  if len(native['15m'])!=1 or not native['5m']:return None
  for base in native:
   if [r[0] for r in native[base]]!=[r[0] for r in source['rows'][base]]:return None
   if any(old[4:]!=confirmed[4:] for old,confirmed in zip(native[base],source['rows'][base])):return None
  if native['5m'][1:]!=source['rows']['5m'][1:]:return None
  original=native['5m'][0];confirmed=source['rows']['5m'][0]
  if original==confirmed or original[1]==confirmed[1] or _aggregate(native['5m'])!=list(native['15m'][0][1:]):return None
  ticks=[t for t in source['trades'] if pd.Timestamp(original[0])<=pd.Timestamp(t[0])<pd.Timestamp(original[0])+pd.Timedelta(minutes=5)]
  if not 2<=len(ticks)<=64 or any(t[2]<=0 for t in ticks):return None
  if sum(t[2] for t in ticks)!=original[5] or original[3]<=ticks[0][1]<=original[2]:return None
  matches=[]
  for interior in [None]+[k for k in range(1,len(ticks)-1) if not original[3]<=ticks[k][1]<=original[2]]:
   removed={0} if interior is None else {0,interior}
   prices=[t[1] for k,t in enumerate(ticks) if k not in removed]
   if prices and [prices[0],max(prices),min(prices),prices[-1]]==list(original[1:5]):matches.append(removed)
  if len(matches)!=1:return None
  return dict(version=RETIME_VERSION,mode='VERIFIED_NATIVE_PRICE_SAMPLES',isin=job['isin'],identity=meta,
    first=str(a),end=str(end),job_id=job['id'],policy_digest=digest(policy),source_policy_digest=digest(sp),
    input_fingerprint=inputs['fingerprint'],before=native,source=source,source_digest=digest(source),created_at=now.isoformat(),
    omitted_price_ticks=[ticks[k] for k in sorted(matches[0])],
    origin='Current visual discrepancy; complete paired official GETTEX tape uniquely reproduces every native price when only its first positive child price and at most one isolated interior extreme are not sampled; all quantities and closes conserved; original values retained')
 except (ValueError,KeyError,TypeError,IndexError):return None


def _reviewed_price_closing_partition(db,job,inputs,native,source,now,policy):
 """Existing zero price delayed <=15m plus exactly one missing final tick.

 The unique complete prefix reproduces EVERY original child; native parents
 must exactly aggregate those children. Only independently published values
 are written, including the observed zero price. No arbitrary provider merge.
 """
 from cp_controls import read,digest
 from cp_reconciliation import SOURCE_POLICY
 sp=read(db,SOURCE_POLICY,{})
 if not (sp.get('approved') is True and sp.get('version')==1
     and sp.get('scope')=='registered_stocks' and sp.get('exclusive_venue')=='GETTEX'):return None
 ticks=source['trades'];a=pd.Timestamp(job['first'])
 if len(ticks)<2 or ticks[-1][2]<=0 or not native['5m'] or not native['15m']:return None
 if native==source['rows']:return None
 if pd.Timestamp(ticks[-1][0]).floor('5min')!=pd.Timestamp(native['5m'][-1][0]):return None
 partition=_trade_partition(native['5m'],ticks[:-1])
 if partition is None:return None
 parents={r[0] for r in native['15m']}
 if {str(pd.Timestamp(r[0]).floor('15min')) for r in native['5m']}!=parents:return None
 for parent in native['15m']:
  if _aggregate([r for r in native['5m'] if pd.Timestamp(parent[0])<=pd.Timestamp(r[0])<pd.Timestamp(parent[0])+pd.Timedelta(minutes=15)])!=list(parent[1:]):return None
 if not parents<={r[0] for r in source['rows']['15m']}:return None
 quoted=[r for r in source['rows']['15m'] if r[0]==str(a)]
 if len(quoted)!=1 or quoted[0][5]!=0 or len(set(quoted[0][1:5]))!=1:return None
 actual={r[0]:r for r in source['rows']['5m']};reassigned=[]
 for original in native['5m']:
  if original[5]>0:
   if original[0] not in actual:return None
   continue
  matches=[r for r in actual.values() if list(r[1:])==list(original[1:])
    and pd.Timedelta(0)<=pd.Timestamp(original[0])-pd.Timestamp(r[0])<=pd.Timedelta(minutes=15)]
  if len(matches)!=1 or len(set(original[1:5]))!=1:return None
  if original!=matches[0]:reassigned.append(dict(old_at=original[0],confirmed_at=matches[0][0],ohlcv=list(original[1:])))
 if not reassigned:return None
 # No additional zero-price observation may be introduced without its exact
 # already existing native value; zero quantity is never invented turnover.
 for actual_row in actual.values():
  if actual_row[5]==0 and not any(list(r[1:])==list(actual_row[1:]) for r in native['5m']):return None
 return dict(version=RETIME_VERSION,mode='VERIFIED_PRICE_CLOSING_TICK_PARTITION',isin=job['isin'],identity=job['identity'],
   first=str(a),end=str(pd.Timestamp(inputs['neighbors']['after'][0])+pd.Timedelta(minutes=15)),
   job_id=job['id'],policy_digest=digest(policy),source_policy_digest=digest(sp),input_fingerprint=inputs['fingerprint'],
   before=native,source=source,source_digest=digest(source),created_at=now.isoformat(),
   native_trade_partition=partition,recovered_closing_tick=ticks[-1],zero_price_reassignments=reassigned,
   zero_observations_corroborated=True,
   origin='Current zero-price review; complete paired official GETTEX tape uniquely reproduces all native children and parents except its final positive tick, including bounded delayed existing zero prices; original values retained')


def _primary_window_plan(db,job,inputs,now,policy):
 """Recover one exact primary parent corroborated by a bound manual review.

 Missing native quantities are explicit here; no conservation claim or TV
 price import. Every written value comes from the complete primary tape.
 """
 from cp_controls import read,digest
 from cp_reconciliation import SOURCE_POLICY,_observation_current
 from history_service import parent_provenance_key
 from pathlib import Path
 from eur_listings import stored_identity
 obs=job.get('observation') or {};confirmation=obs.get('primary_window_confirmation') or {};sp=read(db,SOURCE_POLICY,{})
 if not (job['tf']=='15m' and job['first']==job['last'] and obs.get('result')=='DIFFERENT' and _observation_current(job,inputs)
     and confirmation.get('version')==1 and confirmation.get('first')==job['first'] and confirmation.get('end')==job['end']
     and confirmation.get('matched_fields')==['timestamp','open','high','low','close','volume']
     and (confirmation.get('native_input_fingerprint')==inputs['fingerprint']
          or inputs.get('review_context_fingerprint') and confirmation.get('native_input_fingerprint')==inputs['review_context_fingerprint'])
     and sp.get('approved') is True and sp.get('version')==1 and sp.get('scope')=='registered_stocks' and sp.get('exclusive_venue')=='GETTEX'):return None
 if not stored_identity(db,inputs['info'],job['identity'],'5m'):return None
 native={b:_native(db,job['isin'],job['identity'],b,job['first'],job['end']) for b in ('5m','15m')}
 if digest(native)!=confirmation.get('native_window_digest'):return None
 missing_window=not native['5m'] and not native['15m']
 # The exact parent confirmation independently covers a wholly absent
 # native window too. Partial/mismatched existing aggregates still require
 # their original consistency checks; absence never supplies source values.
 if not missing_window:
  if not 1<=len(native['5m'])<=3 or len(native['15m'])!=1:return None
  if _aggregate(native['5m'])!=list(native['15m'][0][1:]):return None
 source=_official_window(db,job['isin'],job['first'],job['end'],now,allow_zero_rows=True)
 if digest(source)!=confirmation.get('source_digest') or len(source['rows']['15m'])!=1 or source['rows']['15m'][0][5]<=0 or native==source['rows']:return None
 for file in obs.get('evidence',[]):
  path=Path(file['path'])
  if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=file['sha256']:return None
 if not obs.get('evidence'):return None
 _protect_visual_references(db,job,inputs,dict(first=job['first'],end=job['end'],source=source))
 lineage={b:{r[0]:read(db,parent_provenance_key(job['isin'],job['identity'],b),{}).get(r[0]) for r in rows} for b,rows in native.items()}
 superseded=[dict(base_tf=b,old_at=r[0],new_at=None,ohlcv=list(r[1:]),superseded=True,binding='Exact complete primary parent corroborated against TV; this old slot is independently confirmed empty, original preserved') for b,rows in native.items() for r in rows if r[0] not in {x[0] for x in source['rows'][b]}]
 return dict(version=RETIME_VERSION,mode='VERIFIED_PRIMARY_WINDOW',reviewed_tf='15m',isin=job['isin'],identity=job['identity'],first=job['first'],end=job['end'],
   job_id=job['id'],policy_digest=digest(policy),source_policy_digest=digest(sp),input_fingerprint=inputs['fingerprint'],before=native,source=source,
   source_digest=digest(source),created_at=now.isoformat(),lineage_before=lineage,zero_observations_corroborated=True,relocations=superseded,
   primary_window_confirmation=confirmation,**({'missing_native_window':True} if missing_window else {}),origin='One exact closed15m TV parent independently corroborates complete paired primary GETTEX tape; source values only, superseded native values and lineage retained; no conservation or trading grant claimed')


def retime_plan(db,job,inputs,now):
 """Bounded verified same-session repairs, retaining original native OHLCV."""
 from cp_controls import read,digest
 from cp_reconciliation import _observation_current
 import data_quality as dq
 policy=read(db,RETIME_POLICY,{})
 if policy.get('version')!=RETIME_VERSION or policy.get('approved') is not True:return None
 meta=job['identity'];a=pd.Timestamp(job['first']);after=inputs['neighbors']['after']
 if (job['tf'] not in ('5m','15m') or meta['source']!='gettex_chart' or meta['venue']!='GETTEX'
     or meta['currency']!='EUR' or meta['timezone']!='Europe/Berlin'):return None
 if (job.get('observation') or {}).get('primary_window_confirmation'):
  return _primary_window_plan(db,job,inputs,now,policy)
 if job['tf']=='5m':
  automatic=_automatic_zero_plan(db,job,inputs,now,policy)
  if automatic:return automatic
  extreme=_reviewed_positive_price_extreme(db,job,inputs,now,policy)
  if extreme:return extreme
  observation=job.get('observation') or {}
  if observation.get('result') in (None,'ABSENT'):
   empty=_five_minute_empty_plan(db,job,inputs,now,policy)
   if empty:return empty
  return _five_minute_partition_plan(db,job,inputs,now,policy)
 observation=job.get('observation')
 # A current exact visual discrepancy may identify a missing native parent.
 # The full official tape must still uniquely reproduce EVERY original child
 # and conserve all5m/15m OHLCV; observation alone cannot supply any price.
 if (observation and observation.get('result') in ('PRESENT','DIFFERENT')
     and _observation_current(job,inputs) and (not inputs['rows'] or observation['result']=='DIFFERENT')):
  reviewed_partition=_reviewed_partition_plan(db,job,inputs,now,policy)
  if reviewed_partition:return reviewed_partition
 if inputs['rows']:
  if (len(inputs['rows'])==1 and job['last']==str(a) and observation
      and observation.get('result')=='DIFFERENT' and _observation_current(job,inputs)):
   partition=_reviewed_partition_plan(db,job,inputs,now,policy)
   if partition:return partition
   opening=_reviewed_positive_extreme(db,job,inputs,now,policy)
   if opening:return opening
   closing=_reviewed_closing_tick(db,job,inputs,now,policy)
   if closing:return closing
   samples=_reviewed_price_samples(db,job,inputs,now,policy)
   if samples:return samples
  # An explicitly reviewed existing candle can omit a real traded extreme.
  # Keep this narrower than a general provider overwrite: identical timestamps,
  # open/close/volume, unchanged neighbour and conserved complete parent window.
  before=inputs['neighbors']['before']
  if (len(inputs['rows'])!=1 or job['last']!=str(a) or not before
      or pd.Timestamp(before[0])!=a-pd.Timedelta(minutes=15)
      or before[0][:10]!=str(a)[:10] or not observation
      or observation.get('result')!='DIFFERENT' or not _observation_current(job,inputs)):return None
  first,end=before[0],str(a+pd.Timedelta(minutes=15))
  source=_official_window(db,job['isin'],first,end,now)
  native={base:_native(db,job['isin'],meta,base,first,end) for base in ('5m','15m')}
  differences=0
  for base,old in native.items():
   new=source['rows'][base]
   if [r[0] for r in old]!=[r[0] for r in new] or _aggregate(old)!=_aggregate(new):
    raise ValueError('RETIME_EXTREMA_WINDOW_NOT_CONSERVED')
   for original,confirmed in zip(old,new):
    if original==confirmed:continue
    if (original[0]<str(a) or any(original[k]!=confirmed[k] for k in (1,4,5))
        or confirmed[2]<original[2] or confirmed[3]>original[3]):
     raise ValueError('RETIME_EXTREMA_NOT_ISOLATED')
    if base=='5m':
     trades=[t for t in source['trades'] if pd.Timestamp(original[0])<=pd.Timestamp(t[0])
             <pd.Timestamp(original[0])+pd.Timedelta(minutes=5) and t[2]>0]
     if any(original[k]!=confirmed[k] and not any(t[1]==confirmed[k] for t in trades) for k in (2,3)):
      raise ValueError('RETIME_EXTREMA_POSITIVE_TRADE_REQUIRED')
    differences+=1
  if not differences:return None
  return dict(version=RETIME_VERSION,mode='VERIFIED_NATIVE_EXTREMA',isin=job['isin'],identity=meta,
    first=first,end=end,job_id=job['id'],policy_digest=digest(policy),input_fingerprint=inputs['fingerprint'],
    before=native,source=source,source_digest=digest(source),created_at=now.isoformat(),
    origin='Current visual discrepancy plus complete official positive trades; isolated H/L; exact O/C/V and conserved window')
 # A complete official empty interval can corroborate the native timebase
 # locally only when BOTH bracketing real bars and every 5m/15m row agree.
 # Reuse the same bounded receipt/revalidation path; do not rewrite prices or
 # infer absence from a provider's empty response, TV or a matching sample.
 span=empty_native_span(job,inputs)
 authority={**inputs['proof'].get('identity',{}),'absence_evidence':inputs['proof']}
 if span and dq.no_trades_proof(authority,a,a+pd.Timedelta(minutes=15),now):
  left,end,stamps=span
  source=_official_window(db,job['isin'],left,end,now,allow_zero_rows=True)
  native={base:_native(db,job['isin'],meta,base,left,end) for base in ('5m','15m')}
  if (all(native[base]==source['rows'][base] for base in native)
      and {r[0] for r in native['15m']}==stamps):
   # This branch independently proves the complete current native window.
   # A superseded ABSENT observation is not reused as evidence or permission;
   # its obsolete neighbour fingerprint cannot veto this primary-source proof.
   # A presence/difference observation still requires explicit reconciliation.
   if observation and observation.get('result')!='ABSENT':
    raise ValueError('RETIME_VISUAL_REFERENCE_CONFLICT_OR_CHANGED')
   return dict(version=RETIME_VERSION,mode='VERIFIED_NATIVE_EMPTY',isin=job['isin'],identity=meta,
    first=left,end=end,empty_first=str(a),empty_end=str(a+pd.Timedelta(minutes=15)),
    job_id=job['id'],policy_digest=digest(policy),input_fingerprint=inputs['fingerprint'],
    before=native,source=source,source_digest=digest(source),created_at=now.isoformat(),
    zero_observations_corroborated=True,
    origin='Complete official GETTEX publications; exact neighbouring 5m/15m native OHLCV and session boundary; no price replacement')
 if not after:return None
 b=pd.Timestamp(after[0])+pd.Timedelta(minutes=15)
 if not pd.Timedelta(minutes=15)<b-a<=pd.Timedelta(minutes=45) or a.normalize()!=b.normalize():return None
 zero_observed=bool(observation and observation.get('result')=='ZERO_VOLUME' and _observation_current(job,inputs))
 if observation and (observation.get('result') not in ('PRESENT','DIFFERENT','ZERO_VOLUME') or not _observation_current(job,inputs)):
  raise ValueError('RETIME_VISUAL_REFERENCE_CONFLICT_OR_CHANGED')
 source=_official_window(db,job['isin'],str(a),str(b),now,allow_zero_rows=zero_observed)
 before={base:_native(db,job['isin'],meta,base,str(a),str(b)) for base in ('5m','15m')}
 if zero_observed:
  closing=_reviewed_price_closing_partition(db,job,inputs,before,source,now,policy)
  if closing:return closing
 if any(not before[base] or _aggregate(before[base])!=_aggregate(source['rows'][base]) for base in before):
  raise ValueError('RETIME_NOT_CONSERVED')
 if zero_observed:
  observed=[r for r in source['rows']['15m'] if r[0]==str(a)]
  if len(observed)!=1 or observed[0][5]!=0 or len(set(observed[0][1:5]))!=1:
   raise ValueError('RETIME_PRICE_OBSERVATION_NOT_CONFIRMED')
  for r in source['rows']['5m']:
   if r[5]!=0:continue
   # Reassign an already published native price, not a new price guessed from
   # a missing bar. Full official files determine the exact earlier timestamp.
   if len(set(r[1:5]))!=1 or not any(n[1]==r[1] and pd.Timedelta(0)<=pd.Timestamp(n[0])-pd.Timestamp(r[0])<=pd.Timedelta(minutes=15) for n in before['5m']):
    raise ValueError('RETIME_PRICE_OBSERVATION_NATIVE_BINDING_MISSING')
 if not any(pd.Timestamp(r[0])<pd.Timestamp(after[0]) for r in source['rows']['15m']):return None
 relocations=[]
 for base,native in before.items():
  official={r[0]:list(r[1:]) for r in source['rows'][base]}
  native_stamps={r[0] for r in native};used=set()
  for at,*values in native:
   if at in official:continue
   # A missing native timestamp is NOT generally deletable. Permit only a
   # unique, exact 5m candle relocation into a previously absent native slot.
   # Complete official publications already prove the old slot empty; the
   # whole 5m/15m window must still conserve OHLCV above.
   if base!='5m' and not (zero_observed and base=='15m' and values[4]==0 and len(set(values[:4]))==1):
    raise ValueError('RETIME_WOULD_DELETE_NATIVE_CANDLE')
   matches=[stamp for stamp,v in official.items() if stamp not in native_stamps and v==values]
   if (len(matches)!=1 or matches[0] in used
       or abs(pd.Timestamp(matches[0])-pd.Timestamp(at))>pd.Timedelta(minutes=15)):
    raise ValueError('RETIME_RELOCATION_NOT_EXACT_OR_AMBIGUOUS')
   used.add(matches[0])
   relocations.append(dict(base_tf=base,old_at=at,new_at=matches[0],ohlcv=values))
 return dict(version=RETIME_VERSION,isin=job['isin'],identity=meta,first=str(a),end=str(b),
  job_id=job['id'],policy_digest=digest(policy),input_fingerprint=inputs['fingerprint'],
  before=before,source=source,source_digest=digest(source),created_at=now.isoformat(),
  **({'relocations':relocations} if relocations else {}),
  **({'mode':'VERIFIED_PRICE_OBSERVATION_TIMEBASE','zero_observations_corroborated':True,
      'price_only_note':'Published zero-quantity observations, not executed turnover or NO_TRADES'} if zero_observed else {}),
  origin='official GETTEX trade publications; TV observation is diagnostic only')


def receipt_source_current(source,receipt):
 """Exact existing receipt, scoped to its immutable historical window.

 Revalidate the entire source in _official_window first. Only equivalent
 interval geometry and an exchange notice OUTSIDE that window can differ.
 Prices, trades, required file hashes, identity and unknown facts stay exact.
 """
 import copy
 from cp_controls import digest
 try:
  saved=receipt['source'];expected=receipt['source_digest']
  # A modified receipt must not lend a forged authority to unchanged data.
  if digest(saved)!=expected:return False
  if digest(source)==expected:return True
  a,b=pd.Timestamp(receipt['first']),pd.Timestamp(receipt['end'])
  if a.tzinfo is not None or b.tzinfo is not None or pd.isna(a) or pd.isna(b) or a>=b:return False
  def scoped(value):
   result=copy.deepcopy(value);proof=result['proof']
   for field in ('no_trades','trade_intervals','zero_quantity_intervals','incomplete_publication_intervals'):
    if field not in proof:continue
    clipped=[]
    for x,y in proof[field]:
     x,y=pd.Timestamp(x),pd.Timestamp(y)
     if x.tzinfo is not None or y.tzinfo is not None or pd.isna(x) or pd.isna(y) or x>=y:raise ValueError('INVALID_EVIDENCE_RANGE')
     if x<b and y>a:clipped.append((max(x,a),min(y,b)))
    merged=[]
    for x,y in sorted(clipped):
     if merged and x<=merged[-1][1]:merged[-1]=(merged[-1][0],max(y,merged[-1][1]))
     else:merged.append((x,y))
    proof[field]=[(str(x),str(y)) for x,y in merged]
   if 'exchange_closures' in proof:
    relevant=[]
    for entry in proof['exchange_closures']:
     x,y=pd.Timestamp(entry['start']),pd.Timestamp(entry['end'])
     if pd.isna(x) or pd.isna(y) or x.tzinfo is not None or y.tzinfo is not None or x>=y:raise ValueError('INVALID_CLOSURE')
     if x<b and y>a:relevant.append(entry)
    if relevant:proof['exchange_closures']=relevant
    else:
     proof.pop('exchange_closures');proof.pop('closure_checked_at',None)
   return result
  return digest(scoped(source))==digest(scoped(saved))
 except (ValueError,KeyError,TypeError):return False


def reviewed_context_fingerprint(db,job,inputs):
 """Reuse an unchanged subject only when a current receipt proves its new neighbour.

 Reconstruct the entire original visual hash first. Every changed neighbour
 must be an exact before/after row of one disjoint, independently validated
 conserved source correction. Never renew observations or their deadlines.
 """
 from cp_reconciliation import _fingerprint,FINGERPRINT_VERSION
 obs=job.get('observation') or {};old=job.get('native_neighbors')
 if obs.get('fingerprint_version')!=FINGERPRINT_VERSION or not old:return None
 if _fingerprint(dict(inputs,neighbors=old))!=obs.get('input_fingerprint'):return None
 changed=[k for k in ('before','after') if json.dumps(old.get(k))!=json.dumps(inputs['neighbors'].get(k))]
 if not changed:return None
 prefix=RETIME_PREFIX+job['isin']+':';meta=job['identity']
 for side in changed:
  previous,current=old.get(side),inputs['neighbors'].get(side)
  if not previous or not current:return None
  first,end=min(previous[0],current[0]),max(previous[0],current[0])
  rows=db.execute("""SELECT value FROM settings WHERE key>=? AND key<?
   AND json_extract(value,'$.state')='APPLIED'
   AND json_extract(value,'$.mode')='VERIFIED_NATIVE_TRADE_PARTITION'
   AND json_extract(value,'$.reviewed_window_partition')=1
   AND json_extract(value,'$.first')<=? AND json_extract(value,'$.end')>?
   ORDER BY key LIMIT 9""",(prefix,prefix+'\uffff',first,end)).fetchall()
  if len(rows)>8:return None
  proved=False
  for raw, in rows:
   receipt=json.loads(raw)
   if receipt.get('identity')!=meta or not (receipt['end']<=job['first'] or job['end']<=receipt['first']):continue
   before=receipt.get('before',{}).get(job['tf'],[]);after=receipt.get('source',{}).get('rows',{}).get(job['tf'],[])
   if (not any(list(r)==list(previous) for r in before)
       or not any(list(r)==list(current) for r in after)
       or not retime_current(db,receipt)):continue
   proved=True;break
  if not proved:return None
 return obs['input_fingerprint']


def retime_current(db,receipt,now=None,_memo=None):
 from cp_controls import read,digest
 from eur_listings import stored_identity
 try:
  policy=read(db,RETIME_POLICY,{})
  if policy.get('approved') is not True or digest(policy)!=receipt['policy_digest']:return False
  if receipt.get('mode') in ('VERIFIED_NATIVE_CLOSING_TICK','VERIFIED_PRICE_CLOSING_TICK_PARTITION','VERIFIED_NATIVE_PRICE_SAMPLES','VERIFIED_PRIMARY_WINDOW') or receipt.get('reviewed_tf')=='5m' or receipt.get('reviewed_window_partition'):
   from cp_reconciliation import SOURCE_POLICY
   if digest(read(db,SOURCE_POLICY,{}))!=receipt.get('source_policy_digest'):return False
  row=db.execute('SELECT payload FROM instruments WHERE isin=?',(receipt['isin'],)).fetchone()
  if not row or not stored_identity(db,json.loads(row[0]),receipt['identity'],'15m'):return False
  if (receipt.get('reviewed_tf')=='5m' or receipt.get('mode')=='VERIFIED_PRIMARY_WINDOW') and not stored_identity(db,json.loads(row[0]),receipt['identity'],'5m'):return False
  kwargs={'allow_zero_rows':receipt.get('mode') in ('VERIFIED_NATIVE_EMPTY','VERIFIED_NATIVE_TRADE_PARTITION','VERIFIED_NATIVE_SESSION_TAIL','VERIFIED_PRIMARY_WINDOW','VERIFIED_PRICE_OBSERVATION_TIMEBASE','VERIFIED_PRICE_CLOSING_TICK_PARTITION') and receipt.get('zero_observations_corroborated') is True}
  if _memo is not None:kwargs['_memo']=_memo
  source=_official_window(db,receipt['isin'],receipt['first'],receipt['end'],pd.Timestamp(now or pd.Timestamp.now(tz='UTC')),**kwargs)
  if not receipt_source_current(source,receipt):return False
  return all(digest(_native(db,receipt['isin'],receipt['identity'],base,receipt['first'],receipt['end']))==digest(rows)
             for base,rows in source['rows'].items())
 except (ValueError,KeyError,TypeError):return False


def publish_retime(db,job,inputs,plan,now):
 """Caller owns BEGIN IMMEDIATE; recompute all evidence immediately before write."""
 from cp_controls import read,write,digest
 from cp_reconciliation import _input,PROGRESS
 from history_service import parent_provenance_key
 current=retime_plan(db,job,_input(db,job),now)
 if digest(current)!=digest(plan):raise ValueError('RETIME_INPUT_CHANGED')
 key=RETIME_PREFIX+job['isin']+':'+job['id']
 if read(db,key,{}):raise ValueError('RETIME_RECEIPT_ALREADY_EXISTS')
 added=changed=relocated=superseded=0;meta=job['identity'];isin=job['isin']
 revised={}
 for base,rows in plan['source']['rows'].items():
  lineage_key=parent_provenance_key(isin,meta,base);lineage=read(db,lineage_key,{})
  old_rows={r[0]:list(r[1:]) for r in plan['before'][base]}
  moves=[r for r in plan.get('relocations',[]) if r['base_tf']==base]
  for move in moves:
   at=move['old_at'];scope='market:'+isin+':'+base
   prior=(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone() or [0])[0]
   db.execute('DELETE FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? AND ts=?',
    (isin,*[meta[k] for k in IDENTITY[:4]],base,at))
   after=(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone() or [0])[0]
   revised[scope]=after
   db.execute('INSERT INTO candle_revision_history(detected_at,isin,identity,base_tf,ts,old_ohlcv,new_ohlcv,revision_before,revision_after) VALUES (?,?,?,?,?,?,?,?,?)',
    (now.isoformat(),isin,json.dumps(meta,sort_keys=True),base,at,json.dumps(dict(zip(FIELDS,old_rows[at]))),'{}',prior,after))
   lineage.pop(at,None)
   if move.get('superseded'):superseded+=1
   else:relocated+=1
  for at,*values in rows:
   old=old_rows.get(at)
   # The entire corrected window retains lineage, even equal native rows.
   lineage[at]=dict(official_timebase=key,inputs=[],digests=[],ohlcv=values)
   if old==values:continue
   scope='market:'+isin+':'+base
   prior=(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone() or [0])[0]
   db.execute('INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(isin,*[meta[k] for k in IDENTITY[:4]],base,at,*values))
   after=(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone() or [0])[0]
   revised[scope]=after
   db.execute('INSERT INTO candle_revision_history(detected_at,isin,identity,base_tf,ts,old_ohlcv,new_ohlcv,revision_before,revision_after) VALUES (?,?,?,?,?,?,?,?,?)',
    (now.isoformat(),isin,json.dumps(meta,sort_keys=True),base,at,json.dumps(dict(zip(FIELDS,old))) if old else '{}',json.dumps(dict(zip(FIELDS,values))),prior,after))
   added+=int(old is None and at not in {r['new_at'] for r in moves});changed+=int(old is not None)
  write(db,lineage_key,lineage)
 write(db,key,{**plan,'state':'APPLIED','added':added,'changed':changed,'relocated':relocated,'superseded':superseded})
 from cp_reconciliation import _queue_repaired_scopes
 _queue_repaired_scopes(db,key,plan,now)
 progress=read(db,PROGRESS+isin,{});progress['analysis_requested_at']=now.isoformat();progress['native_source_published_at']=now.isoformat();write(db,PROGRESS+isin,progress)
 if revised:
  from cp_assessment import request_from_db
  request_from_db(db,isin,market_revisions=revised)
 return dict(state='APPLIED',receipt_key=key,added=added,changed=changed,relocated=relocated,superseded=superseded,first=plan['first'],end=plan['end'])


def retimed_rows(db,isin,meta,base):
 """Keep a later provider response from undoing an independently proven repair."""
 from cp_controls import read
 from history_service import parent_provenance_key
 records=read(db,parent_provenance_key(isin,meta,base),{});result={}
 for key in {p['official_timebase'] for p in records.values() if p.get('official_timebase')}:
  receipt=read(db,key,{})
  # Corroboration is not an override: a later real native candle must be saved
  # and invalidate the receipt, rather than being suppressed as absent.
  if receipt.get('mode')=='VERIFIED_NATIVE_EMPTY':continue
  if receipt and retime_current(db,receipt):
   values={r[0]:list(r[1:]) for r in receipt['source']['rows'][base]}
   for at in pd.date_range(receipt['first'],receipt['end'],freq=base.replace('m','min'),inclusive='left'):
    result[str(at)]=values.get(str(at))
 return result

def install(db):
 db.execute("CREATE TABLE IF NOT EXISTS candle_revision_history(id INTEGER PRIMARY KEY, detected_at TEXT NOT NULL, isin TEXT NOT NULL, identity TEXT NOT NULL, base_tf TEXT NOT NULL, ts TEXT NOT NULL, old_ohlcv TEXT NOT NULL, new_ohlcv TEXT NOT NULL, revision_before INTEGER NOT NULL, revision_after INTEGER NOT NULL)")

def check(info,store,download,now=None):
 live_clock=now is None
 now=pd.Timestamp(now if now is not None else pd.Timestamp.now(tz='UTC'));isin=info['isin']
 due=store.setting('cp_revision_source_next',None)
 if due and now<pd.Timestamp(due):return {'state':'COOLDOWN'}
 retry=store.setting('cp_source_retry:proxy',None)
 if retry and now<pd.Timestamp(retry):return {'state':'SOURCE_COOLDOWN'}
 candidates=[]
 for m in info.get('eur_listings',[]):
  from eur_listings import venue_allowed
  if not venue_allowed(info,m):continue
  if m.get('isin')!=isin or m.get('status')!='CONFIRMED' or m.get('source')!='proxy' or m.get('currency')!='EUR' or not all(m.get(k) for k in IDENTITY):continue
  meta={k:m[k] for k in IDENTITY}
  for base in ('5m','15m','1H','1D'):
   key='cp_revision:'+hashlib.sha256(json.dumps([isin,meta,base],sort_keys=True).encode()).hexdigest()
   state=store.setting(key,{})
   if state.get('retry_after') and now<pd.Timestamp(state['retry_after']):continue
   candidates.append((state.get('checked_at',''),base,key,meta,state))
 for _,base,key,meta,state in sorted(candidates,key=lambda x:(x[0],x[1],x[2])):
  where='isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=?'
  args=[isin,*[meta[k] for k in ('source','venue','currency','symbol')],base]
  scopes=['instrument:'+isin,'market:'+isin+':'+base,'setting:data_quality:'+isin+':'+meta['symbol']+':'+base,'setting:history_identity:'+isin,'setting:interval_evidence:'+isin]
  def revisions(db):return {s:(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(s,)).fetchone() or [0])[0] for s in scopes}
  with store.connect() as db:
   db.execute('BEGIN')
   persisted=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
   if not persisted or json.loads(persisted[0])!=info:return {'state':'STALE_IDENTITY'}
   token=revisions(db)
   query='SELECT ts,open,high,low,close,volume FROM series WHERE '+where;params=list(args)
   if state.get('cursor'):query+=' AND ts<?';params.append(state['cursor'])
   cutoff=intraday_retention_start(base,now)
   if cutoff is not None:query+=' AND ts>=?';params.append(str(cutoff.tz_convert(meta['timezone']).tz_localize(None)))
   rows=db.execute(query+' ORDER BY ts DESC LIMIT 32',params).fetchall()
  if not rows:
   store.set_setting(key,{'checked_at':now.isoformat(),'retry_after':(now+pd.Timedelta(days=1)).isoformat(),'cursor':None,'state':'NO_ELIGIBLE_ROWS'});continue
  stored_stamps={pd.Timestamp(r[0]):r[0] for r in rows}
  old=pd.DataFrame(rows,columns=['ts',*FIELDS]).set_index('ts').sort_index();old.index=pd.to_datetime(old.index);old.attrs={**meta,'timeframe':base}
  try:old=old.loc[endpoints(old)<=now].copy()
  except (ValueError,KeyError) as error:
   store.set_setting(key,{'checked_at':now.isoformat(),'retry_after':(now+pd.Timedelta(days=1)).isoformat(),'cursor':state.get('cursor'),'state':'UNCONFIRMED','reason':'CALENDAR: '+str(error)});continue
  if old.empty:continue
  # Shared reservation bounds historical requests across all instruments/restarts.
  with store.connect() as db:
   db.execute('BEGIN IMMEDIATE')
   v=db.execute("SELECT value FROM settings WHERE key='cp_revision_source_next'").fetchone()
   if v and now<pd.Timestamp(json.loads(v[0])):return {'state':'COOLDOWN'}
   db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('cp_revision_source_next',json.dumps((now+pd.Timedelta(minutes=15)).isoformat())))
  result={'state':'UNCONFIRMED','identity':meta,'base':base,'checked_at':now.isoformat(),'retry_after':(now+pd.Timedelta(days=1)).isoformat(),'cursor':state.get('cursor'),'count':len(old),'changed':0}
  try:
   fresh=download(meta['symbol'],{'1D':'1d','1H':'60m','5m':'5m','15m':'15m'}[base],old.index[0].tz_localize(meta['timezone']).tz_convert('UTC'),end=endpoints(old)[-1],store=store,expected=meta)
   if any(fresh.attrs.get(k)!=meta[k] for k in IDENTITY):raise ValueError('REVISION_IDENTITY_MISMATCH')
   validate(fresh);fresh.attrs['timeframe']=base
   if fresh.index.tz is not None:fresh.index=fresh.index.tz_convert(meta['timezone']).tz_localize(None)
   if not old.index.isin(fresh.index).all():raise ValueError('REVISION_MISSING_OR_SHIFTED_TIMESTAMPS')
   fresh=fresh.loc[old.index].copy();fresh.attrs={**meta,'timeframe':base}
   if (endpoints(fresh)>now).any():raise ValueError('REVISION_FORMING_BAR')
   import data_quality
   report=data_quality.audit(fresh,base,expected=meta,now=now,check_stale=False)
   if report['state']!='READY':raise ValueError('REVISION_CANDIDATE_'+report['state'])
   detected=pd.Timestamp.now(tz='UTC') if live_clock else now
   result['detected_at']=detected.isoformat()
   changed=[t for t in old.index if any(float(old.loc[t,k])!=float(fresh.loc[t,k]) for k in FIELDS)]
   with store.connect() as db:
    db.execute('BEGIN IMMEDIATE')
    if revisions(db)!=token:raise ValueError('REVISION_INPUT_CHANGED')
    current=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
    if not current or json.loads(current[0])!=info:raise ValueError('REVISION_IDENTITY_CHANGED')
    for t in changed:
     before=(db.execute('SELECT revision FROM input_revisions WHERE scope=?',('market:'+isin+':'+base,)).fetchone() or [0])[0]
     values=[float(fresh.loc[t,k]) for k in FIELDS]
     updated=db.execute('UPDATE series SET open=?,high=?,low=?,close=?,volume=? WHERE '+where+' AND ts=?',values+args+[stored_stamps[t]])
     if updated.rowcount!=1:raise ValueError('REVISION_ROW_NOT_UNIQUE')
     after=(db.execute('SELECT revision FROM input_revisions WHERE scope=?',('market:'+isin+':'+base,)).fetchone() or [0])[0]
     db.execute('INSERT INTO candle_revision_history(detected_at,isin,identity,base_tf,ts,old_ohlcv,new_ohlcv,revision_before,revision_after) VALUES (?,?,?,?,?,?,?,?,?)',(detected.isoformat(),isin,json.dumps(meta,sort_keys=True),base,str(t),json.dumps(dict(zip(FIELDS,[float(old.loc[t,k]) for k in FIELDS]))),json.dumps(dict(zip(FIELDS,values))),before,after))
    result.update(state='UPDATED' if changed else 'UNCHANGED',changed=len(changed),cursor=str(old.index[0]),input_revisions_before=token,input_revisions_after=revisions(db))
    if changed:
     from cp_assessment import request_from_db
     request_from_db(db,isin,market_revisions={'market:'+isin+':'+base:after})
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(result)))
   return result
  except Exception as error:
   result.update(reason=type(error).__name__+': '+str(error))
   # Failed probes never rewrite the operational DQ report or certify READY.
   store.set_setting(key,result);return result
 return {'state':'NO_CONFIRMED_PROXY_WINDOW','gettex':'BLOCKED: authoritative trade corrections need stable trade IDs/replacement semantics; candle-only patch would be overwritten'}
