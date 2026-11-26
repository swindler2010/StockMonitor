"""CandlePilot assessment on its owned read worker, using the existing dispatcher.

The feed writer keeps its durable requests; this worker never downloads or
changes candles. All-TF rules and final revision checks remain in calculation_window.
"""
import json
import time

PREFIX='cp_assessment_request:'
CURSOR='cp_assessment_rotation'


def request(store,isin,valid=None,explicit=False):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if valid is not None and not valid():raise ValueError('EVIDENCE_REQUEST_EXPIRED')
        value=request_from_db(db,isin,explicit=explicit)
    return dict(deferred=True,isin=isin,requested_at=value['requested_at'])


def request_from_db(db,isin,*,explicit=False,market_revisions=None):
    """Enqueue new candle inputs in the same transaction as their commit."""
    key=PREFIX+isin
    row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    value=json.loads(row[0]) if row else dict(isin=isin,requested_at=time.time())
    previous=value.get('market_revisions',{})
    revisions={**previous,**{key:max(previous.get(key,0),value) for key,value in (market_revisions or {}).items()}}
    changed=explicit and not value.get('explicit_recheck_at')
    if changed:value['explicit_recheck_at']=time.time()
    if revisions!=previous:
        value['market_revisions']=revisions
        changed=True
    if row is None or changed:
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value)))
    return value


def choose(db,now=None):
    from paper_hourly import CONFIG,read
    now=time.time() if now is None else now
    pending=[json.loads(v) for (v,) in db.execute('SELECT value FROM settings WHERE key LIKE ?',(PREFIX+'%',))]
    pending=[r for r in pending if r.get('retry_at',0)<=now]
    if not pending:return None
    cfg=read(db,CONFIG,{})
    priority=set(cfg.get('identities',{})) if cfg.get('enabled') and cfg.get('approved') else set()
    streak=read(db,CURSOR,0)
    # Two priority turns, then oldest ordinary request. No all-stock fan-out.
    # Explicit evidence reconciliation shares the existing 2:1 fair rotation.
    # It must not wait behind days of unrelated background archive work.
    preferred=[r for r in pending if (bool(r.get('explicit_recheck_at')) or r['isin'] in priority)==(streak<2)]
    explicit=[r for r in preferred if r.get('explicit_recheck_at')]
    if explicit:
        # A failed/expired explicit request keeps its original evidence marker,
        # but must yield to other waiting explicit requests after its attempt.
        # Otherwise the oldest marker monopolizes every preferred turn forever.
        return min(explicit,key=lambda r:(r.get('last_attempt_at',r['explicit_recheck_at']),r['explicit_recheck_at'],r['isin']))
    # Compare actual waiting age. A missing last_attempt_at is not time zero:
    # recurrent new requests must not permanently outrank an older due retry.
    return min(preferred or pending,key=lambda r:(r.get('last_attempt_at',r['requested_at']),r['requested_at'],r['isin']))


def finish(store,item,result=None,error=None):
    from calculation_window import publish_eligibility
    from cp_reconciliation import complete_analysis_request
    from paper_hourly import CONFIG,read
    isin=item['isin'];expired=False
    if error is None:
        result=publish_eligibility(store,{'isin':isin},result)
        if result and not result.get('error'):
            expired=result.get('publication_state')=='EXPIRED'
            if not expired:complete_analysis_request(store,isin,item.get('analysis_requested_at'),result)
        elif result:error=result.get('error')
    now=time.time()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        key=PREFIX+isin;old=read(db,key,{})
        if old.get('requested_at')!=item['requested_at']:return
        cfg=read(db,CONFIG,{})
        streak=read(db,CURSOR,0)+1 if item.get('explicit_recheck_at') or isin in cfg.get('identities',{}) else 0
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(CURSOR,json.dumps(streak)))
        if (old.get('market_revisions')!=item.get('market_revisions')
            or old.get('explicit_recheck_at')!=item.get('explicit_recheck_at')):
            # A concurrently committed request still needs its own result.
            # Give other due requests a turn even if this instrument is busy.
            old['last_attempt_at']=now
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(old)))
            return
        if error:
            old.pop('retry_reason',None)
            old.update(last_attempt_at=now,retry_at=now+60,error=str(error))
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(old)))
        elif expired:
            # Keep independent closed proofs for diagnostics, but do not
            # acknowledge a fresh assessment or rely on another feed event.
            # The existing retry delay and rotation prevent a hot retry loop.
            old.pop('error',None)
            old.update(last_attempt_at=now,retry_at=now+60,retry_reason='EXPIRED_AT_PUBLICATION')
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(old)))
        else:db.execute('DELETE FROM settings WHERE key=?',(key,))


class Runtime:
    def __init__(self,app):self.app=app;self.busy=False
    def start(self):self.app.root.after(500,self.tick)
    def tick(self):
        app=self.app
        if app.closing:return
        if not self.busy:
            self.busy=True
            def work():
                with app.store.connect() as db:
                    item=choose(db)
                    if item is None:return
                    row=db.execute('SELECT payload FROM instruments WHERE isin=?',(item['isin'],)).fetchone()
                    info=json.loads(row[0]) if row else None
                if not info:
                    finish(app.store,item,error='INSTRUMENT_NOT_FOUND');return
                item['analysis_requested_at']=app.store.setting('cp_reconciliation_progress:'+item['isin'],{}).get('analysis_requested_at')
                try:
                    result=app.compute_eligibility.calculate('eligibility',{'info':info})
                    finish(app.store,item,result)
                except Exception as error:finish(app.store,item,error=error)
            def done(_):self.busy=False
            if app.job(work,done,done,lane='eligibility',key='cp_assessment') is False:self.busy=False
        app.root.after(5000,self.tick)
