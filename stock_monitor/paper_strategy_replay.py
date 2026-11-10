"""Explicit saved-data evaluator. Never downloads or touches the main account.

As-of closed data only; current archive revisions and currently approved
historical risk policy, not a reconstruction of originally published data.
Publication latency is an explicit scenario assumption, not a measurement.
"""
import copy,json,sqlite3,time
from pathlib import Path
from contextlib import contextmanager,closing
import pandas as pd
from storage import Store
from performance_cache import ReadCache
from snapshot_worker import SnapshotContext,Value
from desktop014 import Desktop014
from paper_strategy_shadow import ALLOWED,validate,event_from
from paper_hourly import authority,VERSION,digest
import paper_strategy_v2 as model

class ReadOnly(Store):
    def __init__(self,path):
        self.path=Path(path).resolve();self.directory=self.path.parent;self.read_cache=ReadCache(self.path)
    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
            db.execute('PRAGMA query_only=ON');yield db

def calculate_at(store,info,asof):
    from calculation_window import _candidate_proof
    from cp_controls import manual_context
    with store.connect() as db:cfg=authority(db,info['isin'])
    identity=cfg['identities'][info['isin']]
    class Context(SnapshotContext):
        def user_state(self,instrument):
            state=copy.deepcopy(super().user_state(instrument));state['position']={'quantity':0};return state
        def frame_for(self,instrument,tf):
            return self.closed_frame(instrument,Desktop014._frame_uncached(self,instrument,tf,required_identity=identity),tf)
    context=Context();context.store=store
    for key,value in dict(watchlist=[info],drafts={},feed_errors={},symbol=info['name'],timeframe='1H',period='Max',ai_config={}).items():
        setattr(context,key,Value(value) if key in ('symbol','timeframe','period') else value)
    context.closed_specs={};context.set_analysis_scope(asof)
    with store.read_cache.track_inputs():
        store.read_cache.depend('instrument:'+info['isin'])
        engine=context.trading_analysis(info,analysis_only=True)
        proof=_candidate_proof(store,list(context.closed_specs.values()),store.read_cache.dependencies())
        manual=manual_context(store,info['isin'],identity,'1H',proof)
    return dict(isin=info['isin'],identity=identity,timeframe='1H',execution_context=dict(isin=info['isin'],
        status='MANUAL_RISK_ACCEPTED' if manual else 'HOURLY_DEMO_READY',analysis_ready=engine['status']=='READY',trading_engine=engine,manual_risk=manual),
        hourly=dict(version=VERSION,epoch=cfg['epoch'],config_digest=digest(cfg),proof=proof,required_tf=['1H','4H','1D']))

def run(path,start,end,*,lag_seconds=900):
    """Chronological shared 15m execution tape; hourly CORE recomputed as needed."""
    import data_quality as dq
    from analytics import bar_ends
    store=ReadOnly(path);tape=[];infos={};cache={};states={n:model.book() for n in ('V1','V2','V2_NO_SMART_EXIT')}
    report=dict(version=model.VERSION,start=start,end=end,publication_lag_assumption_seconds=lag_seconds,
        limitations=['Current saved revisions/permissions; not original point-in-time vendor data',
            'Publication delay is assumed; no claim of measured latency','Open positions marked; not forcibly liquidated'],
        inputs={},blocks={},events=[],candidates=[],snapshots=0,observations=0)
    try:
        with store.connect() as db:
            for isin in ALLOWED:
                info=json.loads(db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()[0]);infos[isin]=info
                identity=authority(db,isin)['identities'][isin]
                rows=db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? AND ts>=? AND ts<? ORDER BY ts',
                    (isin,identity['source'],identity['venue'],identity['currency'],identity['symbol'],'15m',start,end)).fetchall()
                frame=pd.DataFrame(rows,columns=['ts',*dq.FIELDS]).set_index('ts');frame.index=pd.to_datetime(frame.index);frame.attrs={**identity,'timeframe':'15m'}
                ends=bar_ends(frame)
                report['inputs'][isin]=dict(bars=len(rows),identity=identity)
                for (opening,row),closed in zip(frame.iterrows(),ends):
                    if pd.isna(closed):continue
                    tape.append((closed,isin,opening,row.to_dict(),identity))
        for closed,isin,opening,values,identity in sorted(tape,key=lambda x:(x[0],x[1])):
            available=closed+pd.Timedelta(seconds=lag_seconds);info=infos[isin]
            # GETTEX hourly closes are anchored 07:30, including the 23:00 tail.
            local=closed.tz_convert('Europe/Berlin');bucket=(str(local.date()),int((local.hour*60+local.minute-450)//60),local.hour==23)
            try:
                if cache.get(isin,{}).get('bucket')!=bucket:
                    cache[isin]=dict(bucket=bucket,result=calculate_at(store,info,closed));report['snapshots']+=1
                r=copy.deepcopy(cache[isin]['result']);r['hourly'].update(checked_at=available.isoformat(),expires_at=(available+pd.Timedelta(seconds=60)).isoformat())
                frame=pd.DataFrame([values],index=pd.DatetimeIndex([opening]));frame.attrs={**identity,'timeframe':'15m'}
                bar=dict(values,closed=True,open_at=opening.tz_localize('Europe/Berlin').isoformat(),close_at=closed.isoformat())
                quality=dq.audit(frame,'15m',identity,available)
                # dq.audit records wall-clock execution time, even for an as-of
                # audit. This isolated replay receipt uses the simulation clock.
                quality={**quality,'checked_at':available.isoformat()}
                r['execution_quote']=dict(isin=isin,identity=identity,timeframe='15m',bars=[bar],data_quality=quality)
                report['candidates'].append(dict(available_at=available.isoformat(),result=r))
                with store.connect() as db:validate(db,info,r,available.timestamp())
                event=event_from(r,bar,available.isoformat());report['observations']+=1
                trial=copy.deepcopy(states);records_all=[]
                for name,state in trial.items():
                    records=model.advance(state,'V1' if name=='V1' else 'V2',event,smart_exit=name!='V2_NO_SMART_EXIT')
                    records_all.extend(dict(book=name,**e) for e in records)
                states=trial;report['events'].extend(records_all)
            except Exception as error:
                reason=type(error).__name__+': '+str(error);report['blocks'][reason]=report['blocks'].get(reason,0)+1
        report['books']={n:model.metrics(s) for n,s in states.items()};report['states']=states
        return report
    finally:store.read_cache.close()

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--db',required=True);parser.add_argument('--start',required=True);parser.add_argument('--end',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();started=time.monotonic();report=run(args.db,args.start,args.end);report['seconds']=time.monotonic()-started
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('snapshots','observations','blocks','books','seconds')},ensure_ascii=True))
