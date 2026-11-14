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


def _official_window(db,isin,first,end,now,allow_zero_rows=False):
 """Require complete native publication evidence and exact trade-built OHLCV."""
 import data_quality as dq
 from cp_controls import read
 proof=read(db,'interval_evidence:'+isin,{})
 meta=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin')
 if proof.get('isin')!=isin or proof.get('identity')!=meta:raise ValueError('RETIME_OFFICIAL_IDENTITY')
 a,b=pd.Timestamp(first),pd.Timestamp(end)
 if b.tz_localize(meta['timezone']).tz_convert('UTC')+pd.Timedelta(minutes=15)>now:raise ValueError('RETIME_NOT_CLOSED')
 fields=('no_trades','trade_intervals','zero_quantity_intervals','incomplete_publication_intervals')
 scoped={k:v for k,v in proof.items() if k not in (*fields,'reference','checked_at','limits')}
 for field in fields:scoped[field]=[p for p in proof.get(field,[]) if pd.Timestamp(p[0])<b and pd.Timestamp(p[1])>a]
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
 raw=pd.DataFrame(trades,columns=['ts','price','volume']);raw.index=pd.to_datetime(raw.pop('ts'))
 if raw.index.has_duplicates:raise ValueError('RETIME_AMBIGUOUS_TRADE_ORDER')
 if not (raw['price'].gt(0).all() and raw['volume'].ge(0).all()):raise ValueError('RETIME_INVALID_TRADES')
 for left in pd.date_range(a,b,freq='5min',inclusive='left'):
  right=left+pd.Timedelta(minutes=5)
  part=raw[(raw.index>=left)&(raw.index<right)]
  # A published zero-quantity price observation is not an empty interval.
  # It may corroborate an IDENTICAL existing native bar, never create a trade
  # or authorize a price correction. Keep its original zero evidence intact.
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
 return dict(rows=result,trades=trades,proof=scoped)


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


def retime_plan(db,job,inputs,now):
 """Recognise only conserved, same-session redistribution, at most 45 minutes."""
 from cp_controls import read,digest
 from cp_reconciliation import _observation_current
 import data_quality as dq
 policy=read(db,RETIME_POLICY,{})
 if policy.get('version')!=RETIME_VERSION or policy.get('approved') is not True:return None
 meta=job['identity'];a=pd.Timestamp(job['first']);after=inputs['neighbors']['after']
 if (job['tf']!='15m' or meta['source']!='gettex_chart' or meta['venue']!='GETTEX'
     or meta['currency']!='EUR' or meta['timezone']!='Europe/Berlin'):return None
 observation=job.get('observation')
 if inputs['rows']:
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
   if observation and (observation.get('result')!='ABSENT' or not _observation_current(job,inputs)):
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
 if observation and (observation.get('result') not in ('PRESENT','DIFFERENT') or not _observation_current(job,inputs)):
  raise ValueError('RETIME_VISUAL_REFERENCE_CONFLICT_OR_CHANGED')
 source=_official_window(db,job['isin'],str(a),str(b),now)
 before={base:_native(db,job['isin'],meta,base,str(a),str(b)) for base in ('5m','15m')}
 if any(not before[base] or _aggregate(before[base])!=_aggregate(source['rows'][base]) for base in before):
  raise ValueError('RETIME_NOT_CONSERVED')
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
   if base!='5m':raise ValueError('RETIME_WOULD_DELETE_NATIVE_CANDLE')
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
  origin='official GETTEX trade publications; TV observation is diagnostic only')


def retime_current(db,receipt,now=None):
 from cp_controls import read,digest
 from eur_listings import stored_identity
 try:
  policy=read(db,RETIME_POLICY,{})
  if policy.get('approved') is not True or digest(policy)!=receipt['policy_digest']:return False
  row=db.execute('SELECT payload FROM instruments WHERE isin=?',(receipt['isin'],)).fetchone()
  if not row or not stored_identity(db,json.loads(row[0]),receipt['identity'],'15m'):return False
  source=_official_window(db,receipt['isin'],receipt['first'],receipt['end'],pd.Timestamp(now or pd.Timestamp.now(tz='UTC')),
   allow_zero_rows=receipt.get('mode')=='VERIFIED_NATIVE_EMPTY' and receipt.get('zero_observations_corroborated') is True)
  if digest(source)!=receipt['source_digest']:return False
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
 added=changed=relocated=0;meta=job['identity'];isin=job['isin']
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
   db.execute('INSERT INTO candle_revision_history(detected_at,isin,identity,base_tf,ts,old_ohlcv,new_ohlcv,revision_before,revision_after) VALUES (?,?,?,?,?,?,?,?,?)',
    (now.isoformat(),isin,json.dumps(meta,sort_keys=True),base,at,json.dumps(dict(zip(FIELDS,old_rows[at]))),'{}',prior,after))
   lineage.pop(at,None);relocated+=1
  for at,*values in rows:
   old=old_rows.get(at)
   # The entire corrected window retains lineage, even equal native rows.
   lineage[at]=dict(official_timebase=key,inputs=[],digests=[],ohlcv=values)
   if old==values:continue
   scope='market:'+isin+':'+base
   prior=(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone() or [0])[0]
   db.execute('INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(isin,*[meta[k] for k in IDENTITY[:4]],base,at,*values))
   after=(db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone() or [0])[0]
   db.execute('INSERT INTO candle_revision_history(detected_at,isin,identity,base_tf,ts,old_ohlcv,new_ohlcv,revision_before,revision_after) VALUES (?,?,?,?,?,?,?,?,?)',
    (now.isoformat(),isin,json.dumps(meta,sort_keys=True),base,at,json.dumps(dict(zip(FIELDS,old))) if old else '{}',json.dumps(dict(zip(FIELDS,values))),prior,after))
   added+=int(old is None and at not in {r['new_at'] for r in moves});changed+=int(old is not None)
  write(db,lineage_key,lineage)
 write(db,key,{**plan,'state':'APPLIED','added':added,'changed':changed,'relocated':relocated})
 progress=read(db,PROGRESS+isin,{});progress['analysis_requested_at']=now.isoformat();write(db,PROGRESS+isin,progress)
 return dict(state='APPLIED',receipt_key=key,added=added,changed=changed,relocated=relocated,first=plan['first'],end=plan['end'])


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
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(result)))
   return result
  except Exception as error:
   result.update(reason=type(error).__name__+': '+str(error))
   # Failed probes never rewrite the operational DQ report or certify READY.
   store.set_setting(key,result);return result
 return {'state':'NO_CONFIRMED_PROXY_WINDOW','gettex':'BLOCKED: authoritative trade corrections need stable trade IDs/replacement semantics; candle-only patch would be overwritten'}
