"""Coalesced all-watchlist diagnostics on the existing Dispatcher misc lane.

No market writes, no network, no new threads, no admission decisions.
Only Tk-side callbacks access this object; work reads the databases off Tk.
"""
from datetime import datetime,timezone
from time import perf_counter

class FreshnessRuntime:
    def __init__(self,app):
        self.app=app;self.requested=0;self.accepted=0;self.busy=False
        self.latest=None;self.reason=None;self.failures=0
        self.read_seconds=None;self.apply_seconds=None
        self.next_automatic_at=0.;self.pending_timer=None;self.urgent=False
    def publish_status(self,state):
        status={'state':state,'requested':self.requested,'accepted':self.accepted,
                'busy':self.busy,'failures':self.failures,'reason':self.reason,
                'read_seconds':self.read_seconds,'apply_seconds':self.apply_seconds,
                'at':datetime.now(timezone.utc).isoformat(),'scope':'all current stocks; diagnostics only'}
        bridge=getattr(self.app,'bridge',None)
        if bridge is not None:bridge.freshness_activity=status
    def request(self,reason='MANUAL'):
        if getattr(self.app,'closing',False):return False
        self.requested+=1;self.reason=reason
        self.urgent|=not (reason.startswith('FEED_COMPLETED:') or reason=='VISIBLE_AGE_TICK')
        if self.busy:self.publish_status('PENDING_NEWER_INPUT');return True
        return self.start()
    def schedule_pending(self,delay):
        if self.pending_timer is not None:return
        def followup():
            self.pending_timer=None;self.start()
        self.pending_timer=self.app.root.after(max(0,int(delay*1000)),followup)

    def start(self):
        if getattr(self.app,'closing',False):return False
        if self.busy or self.accepted==self.requested:return True
        if not self.urgent and perf_counter()<self.next_automatic_at:
            self.publish_status('PENDING_NEWER_INPUT')
            self.schedule_pending(self.next_automatic_at-perf_counter());return True
        if self.pending_timer is not None:
            self.app.root.after_cancel(self.pending_timer);self.pending_timer=None
        self.urgent=False
        from freshness_view import collect
        def watchlist_ids():
            return tuple(sorted(i['isin'] for i in getattr(self.app,'watchlist',[]) if i.get('kind')!='benchmark'))
        captured_watchlist=watchlist_ids()
        sequence=self.requested;self.busy=True;self.publish_status('RUNNING')
        def done(value):
            self.busy=False
            if getattr(self.app,'closing',False):return
            same=watchlist_ids()==captured_watchlist
            if not same:
                # The writer is never cancelled. Only obsolete publication is
                # rejected; one follow-up reads the current watchlist and data.
                if not same:self.requested+=1;self.reason='WATCHLIST_CHANGED';self.urgent=True
                self.publish_status('SUPERSEDED');self.app.root.after(0,self.start);return
            # A diagnostic is explicitly as-of, never a trading permission.
            # Accept its coherent snapshot despite later feeds; do not discard
            # every report and run continuously while ingestion is active.
            self.accepted=sequence;self.latest=value
            self.next_automatic_at=perf_counter()+60
            value['_newer_inputs_pending']=sequence!=self.requested
            self.read_seconds=value.get('_automatic_read_seconds')
            started=perf_counter()
            listener=getattr(self.app,'_freshness_listener',None)
            if listener:listener(value)
            self.apply_seconds=perf_counter()-started
            self.publish_status('AVAILABLE_PENDING_NEWER_INPUT' if value['_newer_inputs_pending'] else 'AVAILABLE')
            if self.accepted!=self.requested:self.schedule_pending(0 if self.urgent else 60)
        def failed(_):
            self.busy=False;self.failures+=1;self.next_automatic_at=perf_counter()+60;self.publish_status('UNAVAILABLE')
            listener=getattr(self.app,'_freshness_error',None)
            if listener:listener(None)
            # No retry spin: the next feed/manual/timer event retries.
        def work():
            started=perf_counter();value=collect(self.app.store.directory)
            value['_automatic_read_seconds']=perf_counter()-started
            return value
        try:
            accepted=self.app.job(work,done,failed,
                                  key='freshness-all',lane='misc')
        except Exception as error:
            import logging
            logging.getLogger(__name__).warning('FRESHNESS_DIAGNOSTICS_FAILED %s',type(error).__name__)
            failed(None);return False
        if not accepted:failed(None)
        return bool(accepted)

def runtime(app):
    if not hasattr(app,'_freshness_runtime'):app._freshness_runtime=FreshnessRuntime(app)
    return app._freshness_runtime

def feed_completed(app,info):
    if info.get('_eligibility_only') or getattr(app,'closing',False):return
    try:runtime(app).request('FEED_COMPLETED:'+info['isin'])
    except Exception as error:
        # Instrumentation must never prevent the normal feed/DQ completion.
        import logging
        logging.getLogger(__name__).warning('FRESHNESS_DIAGNOSTICS_FAILED %s',type(error).__name__)
