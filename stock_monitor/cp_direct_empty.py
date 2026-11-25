"""Due own-source confirmation on the existing CandlePilot writer only."""
def pending(db,isin,limit):
    """Changed own-source evidence should not wait behind the whole archive.

    This is a bounded nomination only. The existing writer still calls
    _input/_classify and checks current fingerprints before saving a result.
    No provider request, NO_TRADES proof or deadline is created here.
    """
    import bisect,json
    from cp_reconciliation import PREFIX,_input,read,SOURCE_POLICY
    import pandas as pd
    if limit<=0:return []
    policy=read(db,SOURCE_POLICY,{})
    if not (policy.get('approved') is True and policy.get('version')==1 and policy.get('scope')=='registered_stocks' and policy.get('exclusive_venue')=='GETTEX'):return []
    saved=db.execute("SELECT json_extract(value,'$.identity','$.basis','$.no_trades','$.incomplete_publication_intervals','$.zero_quantity_intervals') FROM settings WHERE key=?",('interval_evidence:'+isin,)).fetchone()
    if not saved:return []
    try:
        meta,basis,empty,incomplete,zero=json.loads(saved[0])
        expected=dict(source='gettex',symbol=isin,venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
        if meta!=expected or basis!='complete_published_trade_files' or not empty:return []
        intervals=sorted((a,b) for a,b in empty if isinstance(a,str) and isinstance(b,str) and a<b)
        starts=[a for a,b in intervals]
        uncertain=(incomplete or [])+(zero or [])
    except (ValueError,TypeError):return []
    prefix=PREFIX+isin+':'
    candidates=db.execute("""SELECT key,value FROM settings WHERE key>=? AND key<?
      AND json_extract(value,'$.identity.source')='gettex'
      AND json_extract(value,'$.identity.venue')='GETTEX'
      AND json_extract(value,'$.identity.currency')='EUR'
      AND json_extract(value,'$.identity.symbol')=?
      AND json_extract(value,'$.identity.timezone')='Europe/Berlin'
      AND json_extract(value,'$.tf') IN ('5m','15m')
      AND json_extract(value,'$.first')=json_extract(value,'$.last')
      AND json_extract(value,'$.observation') IS NULL
      AND json_extract(value,'$.finding.state') IN ('UNCONFIRMED')
      AND json_extract(value,'$.status') IN ('AWAITING_USER','AWAITING_SOURCE','QUEUED')
      AND julianday(COALESCE(json_extract(value,'$.next_check'),json_extract(value,'$.created_at')))<=julianday('now')
      ORDER BY json_extract(value,'$.first') DESC,key LIMIT 128""",(prefix,prefix+'\uffff',isin)).fetchall()
    result=[];clock=pd.Timestamp.now(tz='UTC');memo={} if db.in_transaction else None
    for key,raw in candidates:
        try:
            job=json.loads(raw);left,right=job['interval_start'],job['end'];index=bisect.bisect_right(starts,left)-1
            if job['identity']!=meta or index<0 or intervals[index][1]<right:continue
            if any(a<right and b>left for a,b in uncertain):continue
            if pd.Timestamp(right).tz_localize(meta['timezone']).tz_convert('UTC')+pd.Timedelta(minutes=15)>clock:continue
            if _input(db,job,_memo=memo)['fingerprint']==job.get('input_fingerprint'):continue
            result.append((key,raw))
            if len(result)>=limit:break
        except (ValueError,TypeError,KeyError):continue
    return result
