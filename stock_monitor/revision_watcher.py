"""Bounded same-listing revision check inside CP maintenance, never a new worker."""
import json,hashlib
import pandas as pd
from history_backfill import FIELDS,validate,intraday_retention_start
from timeframes import endpoints

IDENTITY=('source','venue','currency','symbol','timezone')

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
