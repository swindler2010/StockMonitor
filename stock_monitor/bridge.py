"""Loopback bridge. Tk state is accessed only by callbacks on the UI thread."""
import json
import secrets
import threading
import time
import hashlib
from bridge_queue import CommandQueue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Bridge:
    def __init__(self, directory, events, dispatch, writable=lambda:False):
        self.directory=directory; self.events=events; self.dispatch=dispatch
        self.token=secrets.token_urlsafe(32); self.state={'ready':False}
        self.last_client=0; self.lock=threading.Lock()
        self.instance_id=secrets.token_hex(16)
        self.audit_lock=threading.Lock()
        from mcp_read_access import ReadAccess
        self.read_access=ReadAccess()
        self.commands=CommandQueue(self.instance_id,events,dispatch,writable,audit=self.audit)
        from mcp_pages import ReadPages
        self.read_pages=ReadPages()
        self.published_at=None
        self.selected_analysis=None
        self.summary_previous=None
        self.diagnostic_pipeline={'status':'NOT_STARTED','attempts':0,'accepted':0,'rejected':0,'failed':0}
        self.revision_observations={};self.revision_rejections=[]
        bridge=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def respond(self,code,value):
                raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
                if getattr(self,'read_action',None):
                    event=dict(kind='read_access',at=time.time(),instance_id=bridge.instance_id,
                        action=self.read_action,status_code=code,response_bytes=len(raw),
                        query_hash=getattr(self,'read_query_hash',None),
                        seconds=round(time.monotonic()-self.request_started,6))
                    try:bridge.audit(event);bridge.read_access.record_audit(True)
                    except OSError:bridge.read_access.record_audit(False)
                self.send_response(code);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)));self.end_headers()
                try:self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError):pass
            def do_POST(self):
                self.request_started=time.monotonic();self.read_action=None;acquired=False
                self.connection.settimeout(15)
                if self.path!='/bridge' or self.headers.get('Origin'):
                    self.respond(403,{'error':'Forbidden'});return
                if not secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+bridge.token):
                    self.respond(401,{'error':'Unauthorized'});return
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=16384:raise ValueError('Invalid request size')
                    obj=json.loads(self.rfile.read(length));action=obj['action'];args=obj.get('arguments',{})
                    if not isinstance(args,dict):raise ValueError('Arguments must be an object')
                    bridge.last_client=time.time()
                    from mcp_routes import route,decorate
                    if action=='snapshot':
                        from mcp_server import validate_arguments
                        validate_arguments(action,args)
                    action,args,read_prefix=route(action,args)
                    if action in ('snapshot','archive_catalog','raw_archive','read_summary','health','list_instruments','command_status','revision_diagnostics','decision_journal','get_indicators','read_analytics'):
                        self.read_action=action
                        self.read_query_hash=hashlib.sha256(json.dumps([action,args],sort_keys=True).encode()).hexdigest()
                        acquired=bridge.read_access.acquire()
                        if not acquired:
                            self.respond(429,dict(code='READ_RATE_LIMIT',retry_after_seconds=1));return
                    if action=='snapshot':
                        from mcp_projection import page,digest,compact_identity
                        from mcp_server import validate_arguments
                        validate_arguments(action,args)
                        with bridge.lock:
                            state=bridge.state;published=bridge.published_at
                        section=args.get('section','snapshot')
                        def load():
                            from mcp_projection import persisted
                            if section=='quality_evidence':
                                from dq_evidence import collect
                                value=collect(bridge.directory/'stock_monitor.sqlite3',args.get('evidence_isin'),args.get('evidence_page',0))
                                return value,{'published_at':published}
                            if section=='freshness':
                                from freshness_view import collect
                                value=collect(bridge.directory)
                                value['automatic_check']=dict(getattr(bridge,'freshness_activity',{'state':'NOT_STARTED'}))
                                return value,{'published_at':bridge.published_at}
                            if section=='snapshot':
                                return bridge.snapshot_state()
                            value=persisted(bridge.directory/'stock_monitor.sqlite3') if section=='persisted' else bridge.identity(include_candlepilot=False)
                            return value,{'published_at':bridge.published_at}
                        if section=='candlepilot_health':
                            from cp_bridge_diagnostics import Diagnostics
                            with bridge.lock:
                                if not hasattr(bridge,'cp_diagnostics'):bridge.cp_diagnostics=Diagnostics(cache_path=bridge.directory/'cp-diagnostic-baseline.sqlite3')
                            result=bridge.cp_diagnostics.read(bridge.directory/'stock_monitor.sqlite3',bridge.instance_id,args)
                            self.respond(200,result);return
                        expected=args.get('expected_revision')
                        lease_section=(section+':'+str(args.get('evidence_isin'))+':'+str(args.get('evidence_page',0)) if section=='quality_evidence' else section)
                        value,revision,metadata,remaining=bridge.read_pages.read(lease_section,expected,load)
                        published=metadata.get('published_at')
                        result=page(value,args,revision)
                        result.update(instance_id=bridge.instance_id,version='0.16-dev',scope='test',snapshot_published_at=published,
                            page_lease_remaining_seconds=remaining,page_semantics='Pinned read-only publication; does not extend analytics/signal/trade readiness',
                            is_latest_publication=bridge.publication_is_latest(metadata),current_selection=state.get('selected'),current_analytics_status=state.get('analytics_status'))
                        if args.get('section','snapshot')=='snapshot' and not args.get('path'):
                            from mcp_projection import freshness
                            result.update(ready=value.get('ready',False),selected=value.get('selected'),
                                analytics_freshness=freshness(bridge.directory/'stock_monitor.sqlite3',value.get('_input_revisions')),
                                runtime=dict(instance_id=bridge.instance_id,version='0.16-dev',scope='test',bridge='running',snapshot_published_at=published,mcp_api_revision=2))
                    elif action=='archive_catalog':
                        from mcp_projection import archive_catalog,page
                        from mcp_server import validate_arguments
                        validate_arguments('snapshot',args)
                        rows,revision,metadata,remaining=bridge.read_pages.read('archive_catalog',args.get('expected_revision'),lambda:(archive_catalog(bridge.directory/'stock_monitor.sqlite3'),{}))
                        result=page(rows,args,revision)
                        result.update(instance_id=bridge.instance_id,read_only=True,execution_permission=False,
                            saved_series_count=len(rows),stored_rows_by_table={t:sum(r['count'] for r in rows if r['storage_table']==t) for t in ('series','candles')},
                            registered_instrument_required=False,readiness_required=False,market_open_required=False,page_lease_remaining_seconds=remaining)
                    elif action=='raw_archive':
                        from mcp_projection import raw_archive
                        from mcp_server import validate_arguments
                        validate_arguments('snapshot',{k:v for k,v in args.items() if k!='archive_identity'})
                        result=raw_archive(bridge.directory/'stock_monitor.sqlite3',args['archive_identity'],args)
                        result.update(instance_id=bridge.instance_id,read_only=True,execution_permission=False)
                    elif action=='read_summary':
                        from mcp_projection import read_summary,page
                        from mcp_server import validate_arguments
                        validate_arguments('snapshot',args)
                        captured_state,captured_metadata=bridge.snapshot_state()
                        captured_published=captured_metadata.get('published_at')
                        with bridge.lock:
                            captured_state={**captured_state,'diagnostic_pipeline':dict(bridge.diagnostic_pipeline),
                                'cp_activity':dict(getattr(bridge,'cp_activity',{}))}
                            previous_summary=bridge.summary_previous
                        def load_summary():
                            from mcp_observability import baseline
                            value=read_summary(bridge.directory/'stock_monitor.sqlite3',captured_state,previous_summary)
                            with bridge.lock:bridge.summary_previous=baseline(value)
                            return value,{'published_at':captured_published,'selected_publication':captured_metadata['selected_publication']} if 'selected_publication' in captured_metadata else {'published_at':captured_published}
                        expected=args.get('expected_revision')
                        if not expected:
                            deferred,expected=bridge.read_pages.begin_deferred('read_summary')
                            if deferred=='TIMED_OUT':
                                self.respond(200,dict(status='UNAVAILABLE',reason_code='READ_DIAGNOSTIC_TIMEOUT',section='read_summary',
                                    retry_after_seconds=5,reason='Existing summary exceeded 60 seconds; no duplicate calculation or zero counts.'));return
                            if deferred!='READY':
                                self.respond(200,{'status':'INDETERMINATE','data_status':'NOT_AVAILABLE_YET',
                                    'section':'read_summary','instance_id':bridge.instance_id,'retry_after_seconds':2,
                                    'reason':'READ_ONLY_SUMMARY_IN_PROGRESS; counts are unknown until completed'})
                                self.wfile.flush()
                                if deferred=='START':
                                    bridge.read_pages.finish_deferred('read_summary',load_summary)
                                return
                        value,revision,metadata,remaining=bridge.read_pages.read('read_summary',expected,
                            load_summary)
                        result=page(value,args,revision)
                        result.update(instance_id=bridge.instance_id,version='0.16-dev',scope='test',
                            page_lease_remaining_seconds=remaining,snapshot_published_at=metadata.get('published_at'),
                            is_latest_publication=bridge.publication_is_latest(metadata),
                            execution_permission=False,read_only=True)
                    elif action=='revision_diagnostics':
                        from mcp_server import validate_arguments
                        from revision_diagnostics import read
                        validate_arguments(action,args)
                        with bridge.lock:
                            observations=dict(bridge.revision_observations);rejections=list(bridge.revision_rejections);pipeline=dict(bridge.diagnostic_pipeline)
                        result=read(bridge.directory/'stock_monitor.sqlite3',args,observations,rejections,pipeline,bridge.instance_id)
                    elif action=='health':
                        result=bridge.identity(include_diagnostics=False)
                        result['read_access']=bridge.read_access.status()
                        from mcp_routes import routes
                        result['read_routes']=routes()
                    elif action=='list_instruments':
                        from mcp_projection import instruments
                        from mcp_server import validate_arguments
                        validate_arguments(action,args)
                        from mcp_projection import page
                        rows,revision,metadata,remaining=bridge.read_pages.read('catalog',args.get('expected_revision'),lambda:(instruments(bridge.directory/'stock_monitor.sqlite3',{},True),{}))
                        result=page(rows,args,revision)
                        result.update(instance_id=bridge.instance_id,version='0.16-dev',scope='test',instrument_total=len(rows),
                            stock_total=sum(r['kind']!='benchmark' for r in rows),benchmark_total=sum(r['kind']=='benchmark' for r in rows),
                            analysis_ready_required=False,page_lease_remaining_seconds=remaining,page_semantics='Pinned read-only catalog; saved provenance is not current admission')
                    elif action=='command_status':
                        from mcp_projection import receipt
                        from mcp_server import validate_arguments
                        validate_arguments(action,args)
                        result=receipt(bridge.command_status(args),{k:args[k] for k in ('path','offset','limit','expected_revision') if k in args})
                    elif action in ('refresh','select','set_levels','add_instrument','remove_instrument','update_position','record_decision','review_decision','read_analytics','paper_trade','get_indicators','set_indicators','reconcile_dq_evidence'):
                        from mcp_server import validate_arguments
                        validate_arguments(action,args)
                        result=bridge.commands.submit(action,args,obj.get('command',{}))
                        from mcp_projection import receipt
                        result=receipt(result)
                    elif action=='decision_journal':
                        done=threading.Event();box={}
                        def call():
                            try:box['result']=bridge.dispatch(action,args)
                            except Exception as exc:box['error']=str(exc)
                            finally:done.set()
                        bridge.events.put((call,()))
                        if not done.wait(10):raise TimeoutError('UI busy; command may still complete')
                        if 'error' in box:raise ValueError(box['error'])
                        result=box['result']
                    else:raise ValueError('Unknown action')
                    self.respond(200,decorate(result,read_prefix,bridge.instance_id))
                except Exception as exc:
                    safe={'INVALID_READ_ROUTE','PAGE_SESSION_EXPIRED','PAGE_SESSION_TOO_LARGE','INVALID_JSON_POINTER','STALE_PAGE','REVISION_REQUIRED','PATH_NOT_FOUND','INVALID_PAGE','EXACT_STORED_IDENTITY_REQUIRED','ARCHIVE_RANGE_REQUIRES_CALCULATED_VIEW'}
                    code=str(exc) if str(exc) in safe else 'DATA_STORE_BUSY' if type(exc).__name__=='OperationalError' else 'BRIDGE_REQUEST_FAILED'
                    self.respond(400,{'code':code,'error':code})
                finally:
                    if acquired:bridge.read_access.release()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.directory.mkdir(parents=True,exist_ok=True)
        self.descriptor=directory/'bridge.json'
        self.descriptor.write_text(json.dumps({'url':f'http://127.0.0.1:{self.server.server_port}/bridge','token':self.token,'instance_id':self.instance_id,'scope':'test','version':'0.16-dev','mcp_api_revision':2,'revision_diagnostics_version':1}),encoding='utf-8')
        threading.Thread(target=self.server.serve_forever,daemon=True).start()

    def command_status(self,args):
        """Read receipts, never retry execution; durable fills survive restarts."""
        cid=args.get('command_id')
        if not isinstance(cid,str) or not 1<=len(cid)<=128:
            raise ValueError('Command ID required')
        if args.get('instance_id')!=self.instance_id:
            return dict(command_id=cid,status='BLOCKED',code='WRONG_INSTANCE',
                        read_back_required=True,execution='UNCONFIRMED')
        try:return self.commands.status(cid)
        except ValueError as error:
            if str(error)!='Unknown command ID':raise
        import sqlite3
        from contextlib import closing
        path=(self.directory/'stock_monitor.sqlite3').resolve()
        try:
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                from paper_account import saved_fill
                row=saved_fill(db,cid)
            if row:
                return dict(command_id=cid,action='paper_trade',status='COMPLETED',
                            receipt_source='persisted_paper_fill',
                            result=dict(status='COMPLETED',fill=json.loads(row[2]),idempotent=True,archived=row[3]))
        except (sqlite3.Error,ValueError):
            return dict(command_id=cid,status='UNAVAILABLE',code='RECEIPT_STORE_UNAVAILABLE',
                        execution='UNCONFIRMED',read_back_required=True)
        return dict(command_id=cid,status='NOT_FOUND',code='RECEIPT_NOT_RETAINED',
                    execution='NO_PERSISTED_PAPER_FILL',read_back_required=True,
                    reason='No receipt in this instance or fill in this TEST database. Prior rejection/expiry is unconfirmed; do not resubmit automatically.')

    def publish(self,value):
        with self.lock:
            self.state=value;self.published_at=time.time()
            self.diagnostic_pipeline['revision_diagnostics_error']=value.get('_revision_trace_error')
            for event_id,trace in value.get('_revision_trace',{}).items():
                self.revision_observations.pop(event_id,None)
                self.revision_observations[event_id]={**trace,'published_at':self.published_at}
            while len(self.revision_observations)>100:self.revision_observations.pop(next(iter(self.revision_observations)))

    def publish_selected_analysis(self,selected,engine,revisions):
        """An already validated chart result, independent of the watchlist job.

        Called only after the existing UI selection/revision guards. No frame,
        SQL query or indicator calculation runs here or in the Tk callback.
        """
        if not isinstance(engine,dict) or not isinstance(revisions,dict):return
        with self.lock:
            if self.state.get('selected')!=selected:return
            self.selected_analysis={'selected':dict(selected),'trading_engine':engine,
                '_input_revisions':dict(revisions),'completed_at':time.time()}

    def snapshot_state(self):
        """Expose finished selected analysis without granting execution."""
        from mcp_projection import freshness
        with self.lock:
            state=self.state;published=self.published_at;selected=self.selected_analysis
        if not selected or selected['selected']!=state.get('selected'):
            return state,{'published_at':published}
        current=freshness(self.directory/'stock_monitor.sqlite3',selected['_input_revisions'])
        if current.get('status')!='CURRENT':
            return {**state,'selected_analysis_status':'STALE','selected_analysis_freshness':current},{'published_at':published}
        engine=selected['trading_engine']
        return {**state,'ready':engine.get('status')=='READY',
            'analytics_status':'COMPLETED','reason':'Selected chart analysis completed independently of the watchlist',
            'selected_analysis':engine,'selected_analysis_status':'CURRENT',
            'selected_analysis_completed_at':selected['completed_at'],
            'execution_permission':False,'_input_revisions':selected['_input_revisions'],
            'analysis_publication_scope':'SELECTED_CHART; watchlist pipeline reported separately'}, {'published_at':selected['completed_at'],'selected_publication':dict(selected['selected'])}

    def publication_is_latest(self,metadata):
        with self.lock:
            if 'selected_publication' in metadata:
                current=self.selected_analysis
                return bool(current and current['selected']==self.state.get('selected')==metadata['selected_publication']
                    and current['completed_at']==metadata.get('published_at'))
            return metadata.get('published_at')==self.published_at

    def revision_rejected(self,state,reason):
        with self.lock:
            self.revision_rejections.append({'at':time.time(),'reason':reason,
                'revisions':{k:v for k,v in state.get('_input_revisions',{}).items() if k.startswith('market:')}})
            self.revision_rejections=self.revision_rejections[-32:]

    def diagnostic_event(self,status,**evidence):
        with self.lock:
            counters={'RUNNING':'attempts','ACCEPTED':'accepted','REJECTED':'rejected','FAILED':'failed'}
            key=counters.get(status)
            if key:self.diagnostic_pipeline[key]+=1
            self.diagnostic_pipeline.update(status=status,updated_at=time.time(),**evidence)
            if status in ('ACCEPTED','REJECTED','FAILED'):
                self.diagnostic_pipeline['last_outcome']={'status':status,'at':time.time(),**evidence}
            if not self.state.get('ready') and status in ('RUNNING','REJECTED','FAILED'):
                # No analytics were published: expose the actual outcome,
                # rather than leaving a failed computation labelled PENDING.
                self.state={**self.state,'analytics_status':status,
                            'reason':evidence.get('cause') or 'Background analytics running'}

    def snapshot_progress(self,progress):
        # Called from the existing dispatcher reader, never performs Tk/DB work.
        allowed={k:progress[k] for k in ('phase','isin','completed','total','elapsed_seconds') if k in progress}
        with self.lock:
            if self.diagnostic_pipeline.get('status')=='RUNNING':
                self.diagnostic_pipeline.update(progress=allowed,updated_at=time.time())

    def selection_changed(self,selected):
        """Publish cheap UI truth immediately; never relabel old analytics."""
        with self.lock:
            if self.state.get('selected')==selected:return
            self.state={'version':'0.16-dev','ready':False,'selected':dict(selected),
                        'analytics_status':'PENDING','reason':'Background analytics for this selection are not published yet'}
            self.published_at=time.time()

    def identity(self,include_diagnostics=True,include_candlepilot=True):
        from mcp_server import TOOLS
        path=str((self.directory/'stock_monitor.sqlite3').resolve())
        if not include_diagnostics:
            # Liveness must not wait for archive/account reads. A responding
            # bridge is not evidence of current analytics or trading admission.
            with self.lock:
                published=self.published_at
                pipeline={k:self.diagnostic_pipeline.get(k) for k in
                          ('status','attempts','accepted','rejected','failed','updated_at','progress')}
            return {'version':'0.16-dev','mcp_api_revision':2,'scope':'test',
                    'instance_id':self.instance_id,'environment_id':hashlib.sha256(path.encode()).hexdigest()[:16],
                    'database_path':path,'endpoint':f'http://127.0.0.1:{self.server.server_port}/bridge',
                    'bridge':'running','snapshot_published_at':published,
                    'snapshot_age_seconds':None if published is None else max(0,time.time()-published),
                    'diagnostic_pipeline':pipeline,'capabilities':[t['name'] for t in TOOLS],
                    'readiness':'NOT_EVALUATED_BY_HEALTH',
                    'candlepilot_health':{'status':'NOT_READ','tool':'stock_monitor_snapshot','section':'candlepilot_health'},
                    'paper_account':{'status':'NOT_READ','tool':'stock_monitor_snapshot','section':'runtime','path':'/paper_account'},
                    'real_account':{'status':'NOT_READ','tool':'stock_monitor_snapshot','section':'runtime','path':'/real_account'},
                    'detail_delivery':{'status':'EXPLICIT_READ_REQUIRED','reason':'Health checks connection only; no archive/account query or readiness grant.'}}
        from paper_account import status
        try:paper=status(path)
        except Exception:paper={'status':'UNAVAILABLE','virtual':True}
        from real_portfolio import status as real_status
        try:real=real_status(path)
        except Exception:real={'status':'UNAVAILABLE','account':'V1','execution_permission':False}
        cp_health={'status':'NOT_READ','tool':'stock_monitor_snapshot','section':'candlepilot_health',
            'reason':'PAPER runtime does not run the separate full-history CandlePilot diagnostic'}
        if include_candlepilot:
            from candlepilot_health import collect
            try:
                cp_health=collect(path,self.state)
                cp_health['activity']=getattr(self,'cp_activity',{'state':'UNKNOWN'})
            except Exception as error:cp_health={'status':'UNAVAILABLE','reason':type(error).__name__}
        return {'candlepilot_health':cp_health,'paper_account':paper,'real_account':real,'version':'0.16-dev','mcp_api_revision':2,'scope':'test','instance_id':self.instance_id,
                'environment_id':hashlib.sha256(path.encode()).hexdigest()[:16],
                'database_path':path,'endpoint':f'http://127.0.0.1:{self.server.server_port}/bridge',
                'bridge':'running','snapshot_published_at':self.published_at,
                'diagnostic_pipeline':dict(self.diagnostic_pipeline),
                'snapshot_age_seconds':None if self.published_at is None else max(0,time.time()-self.published_at),
                'capabilities':[t['name'] for t in TOOLS]}

    def audit(self,event):
        # No arguments, tokens, provider output or portfolio values in this log.
        with self.audit_lock:
            with (self.directory/'bridge-command-audit.jsonl').open('a',encoding='utf-8') as file:
                file.write(json.dumps(event,allow_nan=False)+'\n')

    def close(self):
        if hasattr(self,'cp_diagnostics'):self.cp_diagnostics.close()
        self.commands.close()
        try:
            if json.loads(self.descriptor.read_text())['token']==self.token:self.descriptor.unlink()
        except (OSError,ValueError,KeyError):pass
        threading.Thread(target=self.server.shutdown,daemon=True).start()


