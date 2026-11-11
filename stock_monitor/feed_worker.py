"""Run the existing scheduled feed implementation without a Tk interpreter loop."""
import queue
import threading
from contextlib import nullcontext
from app import StockMonitor

class FeedContext(StockMonitor):
    def __init__(self,store):
        # Deliberately skip UI initialization, migration, timers and Bridge.
        self.store=store;self.closing=False
        self.store.defer_eligibility=True
        self.feed_slots=nullcontext();self.events=queue.Queue()
        self.http_cache={};self.http_lock=threading.Lock()

    def calculate(self,info):
        from eur_listings import OWNER_HISTORY_POLICY,owner_history_info
        info=owner_history_info(info,self.store.setting(OWNER_HISTORY_POLICY,{}))
        local={}
        if not info.get('_gettex_poll_only'):
            # The existing short history routes return before _run_feed. Give
            # them a small local slice too, without waiting for HTTP or opening
            # a chart. Fresh-price reservations remain acquisition-only.
            try:
                if info.get('_eligibility_only') or info.get('_review_only'):
                    from gettex_history import accumulate_cached_batch
                    local['cached_history']=accumulate_cached_batch(self.store,self._parse_gettex_csv,max_pairs=2,max_seconds=1)
                from history_service import inspect_changed_official_archive
                local['archive_quality']=inspect_changed_official_archive(self.store)
                from cp_diagnostic_cleanup import turn as normalize_diagnostics
                local['diagnostic_cleanup']=normalize_diagnostics(self.store,info)
            except Exception as error:
                local['error']=str(error)
        planned=None
        if not info.get('_gettex_poll_only'):
            # Use the last saved calculation dependencies before this normal
            # writer turn, so repair need not wait another complete universe
            # rotation. The native repair rechecks current DQ/identity itself.
            try:
                from cp_controls import automatic_repair_turn
                planned=automatic_repair_turn(self.store,info)
            except Exception as error:planned={'state':'ERROR','reason':str(error)}
        result=self._calculate(info)
        if planned is not None:
            result['cp_automatic_repair']=planned
            if planned.get('state')=='ERROR':result.setdefault('errors',[]).append('CP automatic repair: '+planned['reason'])
        if local:result['cp_local_history']=local
        if local.get('error'):result.setdefault('errors',[]).append('CP local history: '+local['error'])
        fresh=bool(info.get('_gettex_poll_only'))
        from cp_controls import automatic_turn
        result['cp_auto_risk']=automatic_turn(self.store,info)
        # One bounded diagnostic stage on the existing serial writer. It never
        # controls Desktop, requests prices or changes factual DQ.
        from cp_reconciliation import turn
        try:
            if not fresh:result['cp_reconciliation']=turn(self.store,info)
            from cp_controls import tv_risk_turn
            result['cp_tv_risk']=tv_risk_turn(self.store,info)
            from cp_controls import reviewed_risk_turn
            result['cp_reviewed_risk']=reviewed_risk_turn(self.store,info)
            requested=result['cp_reviewed_risk'].get('analysis_requested_at') or result['cp_tv_risk'].get('analysis_requested_at') or result.get('cp_reconciliation',{}).get('analysis_requested_at')
            if fresh and not requested:
                # Resume an already durable request after restart, including an
                # unchanged provider reply. Only the read worker acknowledges it.
                requested=self.store.setting('cp_reconciliation_progress:'+info['isin'],{}).get('analysis_requested_at')
            repair=result.get('cp_priority') or {}
            if repair.get('state')=='COMPLETED':
                requested=max(requested or '',repair['finished_at'])
            if requested and (fresh or repair.get('state')=='COMPLETED'):
                # Keep the lightweight exact-scope risk revalidation above:
                # new market revisions revoke its old receipts immediately.
                # Defer historical reconciliation to history slots. Preserve its
                # request until the owned read worker completes the assessment;
                # the fresh writer must not calculate or publish readiness.
                from cp_reconciliation import PROGRESS,read,write
                with self.store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    progress=read(db,PROGRESS+info['isin'],{})
                    progress['analysis_requested_at']=max(progress.get('analysis_requested_at') or requested,requested)
                    write(db,PROGRESS+info['isin'],progress)
            if requested and fresh:
                # Queue metadata only. Waiting for this ISIN's history slot used
                # to leave a working stock stale despite regular fresh feeds.
                # Existing request/choose keep deduplication, retry and fairness.
                from cp_assessment import request
                request(self.store,info['isin'])
                result['cp_analysis_deferred']=requested
            elif requested:
                from calculation_window import eligibility_turn
                from cp_reconciliation import complete_analysis_request
                assessment=eligibility_turn(self.store,info)
                complete_analysis_request(self.store,info['isin'],requested,assessment)
        except Exception as error:
            result['cp_reconciliation']={'state':'ERROR','reason':str(error)}
        if not fresh and (planned or {}).get('state')=='AWAITING_ASSESSMENT':
            try:
                from cp_controls import automatic_repair_turn
                result['cp_automatic_repair']=automatic_repair_turn(self.store,info)
            except Exception as error:
                result['cp_automatic_repair']={'state':'ERROR','reason':str(error)}
                result.setdefault('errors',[]).append('CP automatic repair: '+str(error))
        # Repair/review/eligibility turns can return before _run_feed. They
        # still consume the reserved history slot; otherwise three live jobs
        # disable fresh acquisition until a full archive turn happens to run.
        if not info.get('_gettex_poll_only'):
            from candle_schedule import FRESH_ROTATION,history_finished
            if self.store.setting(FRESH_ROTATION,{}).get('consecutive',0):
                history_finished(self.store)
        return result

    def _calculate(self,info):
        from calculation_window import eligibility_turn, eligibility_due
        import pandas as pd
        from cp_controls import service_request
        # Fresh-price slots are reserved by the session planner. An old repair
        # (even a cooldown-only visit) must not consume them before acquisition.
        # The same persisted request runs on the next ordinary/history turn.
        urgent=None if info.get('_gettex_poll_only') else service_request(info,self.store,self._recover_gettex_range)
        if urgent is not None:
            # Same existing serial writer; return to the normal queue afterwards.
            return {'errors':[],'cp_priority':urgent}
        if info.get('_review_only'):
            from cp_controls import historical_review_turn
            review=historical_review_turn(self.store,info)
            if review.get('state')=='COMPLETE':eligibility_turn(self.store,info)
            return {'errors':[],'cp_review':review}
        if info.get('_eligibility_only'):
            from eur_listings import ensure_selected_proof
            ensure_selected_proof(info,self.store)
            from cp_controls import historical_review_turn
            historical_review_turn(self.store,info)
            eligibility_turn(self.store,info)
            return {'errors':[]}
        self._run_feed(info)
        callback,(received,errors)=self.events.get_nowait()
        if received['isin']!=info['isin']:raise ValueError('Feed result identity mismatch')
        if not info.get('_gettex_poll_only'):
            if info.get('kind')!='benchmark':
                from eur_listings import ensure_selected_proof
                ensure_selected_proof(info,self.store)
                from cp_controls import historical_review_turn
                historical_review_turn(self.store,info)
            # A queued feed for an expired ISIN cannot be enqueued twice. Service
            # one due assessment at this existing writer boundary, without
            # waiting for that ISIN's network/history turn or extending its TTL.
            due=eligibility_due(self.store,self.store.instruments(),pd.Timestamp.now(tz='UTC'))
            for urgent in due:eligibility_turn(self.store,urgent)
            if info.get('kind')!='benchmark' and not any(i['isin']==info['isin'] for i in due):
                eligibility_turn(self.store,info)
        return {'errors':errors}
