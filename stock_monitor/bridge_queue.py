"""Bounded per-instance command receipts; expired queued work never executes."""
import hashlib
import json
import math
import threading
import time
from concurrent.futures import Future

def read_error_code(error):
    """Allowlisted existing read causes only; no raw worker/provider exceptions."""
    text=str(error).split(' [stage=',1)[0]
    if ': ' in text:text=text.split(': ',1)[1]
    allowed={'STALE_PAGE','REVISION_REQUIRED','IDENTITY_MISMATCH','UNKNOWN_INSTRUMENT','INVALID_PAGE','RANGE_REQUIRES_TIMEZONE','EXACT_STORED_IDENTITY_REQUIRED','ARCHIVE_RANGE_REQUIRES_CALCULATED_VIEW','ARCHIVE_CLOSED_STATUS_UNASSESSED'}
    return text if text in allowed else 'READ_FAILED'


class CommandQueue:
    def __init__(self, instance_id, events, dispatch, writable, limit=1024, audit=lambda event:None):
        self.instance_id=instance_id;self.events=events;self.dispatch=dispatch
        self.writable=writable;self.limit=limit;self.records={};self.lock=threading.RLock();self.closed=False
        self.audit=audit

    def record_event(self, r):
        self.audit({'at':time.time(),'instance_id':self.instance_id,'command_hash':hashlib.sha256(r['command_id'].encode()).hexdigest(),
                    'action':r['action'],'status':r['status']})

    def status(self, command_id):
        with self.lock:
            r=self.records.get(command_id)
            if r is None:raise ValueError('Unknown command ID')
            return {k:r[k] for k in ('command_id','action','status','result') if k in r}

    def proposal_valid(self,command_id):
        with self.lock:
            r=self.records.get(command_id)
            return bool(r and not self.closed and self.writable() and time.monotonic()<r['expires'] and r['status'] in ('RUNNING','AWAITING_APPROVAL'))

    def proposal_result(self,command_id,status,result):
        if status not in ('AWAITING_SAVE','REJECTED','EXPIRED','FAILED'):raise ValueError('Invalid proposal status')
        with self.lock:
            r=self.records.get(command_id)
            if not r or r['action']!='set_levels':raise ValueError('Unknown proposal')
            r.update(status=status,result=result);self.record_event(r);r['done'].set()

    def confirm_saved_levels(self,instruments):
        """Called only after the existing explicit save transaction succeeded."""
        by_isin={i['isin']:i for i in instruments}
        with self.lock:
            for r in self.records.values():
                if r['action']!='set_levels' or r['status']!='AWAITING_SAVE':continue
                expected=r['result'];item=by_isin.get(expected['isin'],{})
                matches=all(item.get('levels',{}).get(k)==v for k,v in expected['levels'].items())
                matches=matches and item.get('levels_source')==expected['source'] and item.get('levels_currency',item.get('currency','EUR'))==expected['currency']
                r.update(status='COMPLETED' if matches else 'SUPERSEDED',result={**expected,'saved':matches,'readback':'EXPLICIT_SAVE_COMMITTED' if matches else 'SAVED_VALUES_DIFFER'})
                self.record_event(r)

    def submit(self, action, arguments, envelope, wait=10):
        if envelope.get('instance_id')!=self.instance_id:raise ValueError('Wrong instance')
        cid=envelope.get('command_id');deadline=envelope.get('deadline')
        if not isinstance(cid,str) or not 1<=len(cid)<=128:raise ValueError('Command ID required')
        if isinstance(deadline,bool) or not isinstance(deadline,(int,float)) or not math.isfinite(deadline):raise ValueError('Deadline required')
        signature=hashlib.sha256(json.dumps([action,arguments,deadline],sort_keys=True,allow_nan=False).encode()).hexdigest()
        with self.lock:
            if self.closed:raise ValueError('Instance closing')
            record=self.records.get(cid)
            if record is not None:
                if record['signature']!=signature:raise ValueError('Command ID reused with different input')
                return self.status(cid)
            remaining=deadline-time.time()
            if not 0<remaining<=120:raise ValueError('Expired or excessive deadline')
            if len(self.records)>=self.limit:raise ValueError('Command receipt capacity reached; reconnect to a new instance')
            record={'command_id':cid,'action':action,'status':'QUEUED','signature':signature,'done':threading.Event(),'expires':time.monotonic()+remaining}
            self.record_event(record)  # Fail closed if the receipt cannot be recorded.
            self.records[cid]=record
        def call():
            with self.lock:
                if record['status']!='QUEUED':return
                if self.closed or time.monotonic()>=record['expires']:
                    record['status']='EXPIRED';record['done'].set();return
                record['status']='RUNNING'
            try:
                if action not in ('read_analytics','get_indicators') and not self.writable():raise PermissionError('Application edits are disabled')
                self.record_event(record)
                dispatch_args={**arguments,'_deadline':deadline} if action in ('paper_trade','read_analytics') else arguments
                if action in ('set_levels','set_indicators'):dispatch_args={**arguments,'_command_id':cid}
                result=self.dispatch(action,dispatch_args)
                if action in ('read_analytics','paper_trade','set_indicators') and isinstance(result,Future):
                    def finish(future):
                        with self.lock:
                            if action=='set_indicators':
                                try:
                                    answer=future.result()
                                    record.update(status='COMPLETED',result=answer)
                                except Exception:record.update(status='FAILED',result={'code':'INDICATORS_WRITE_FAILED','saved':False})
                                self.record_event(record);record['done'].set();return
                            if self.closed or time.monotonic()>=record['expires']:record.update(status='EXPIRED')
                            else:
                                try:
                                    answer=future.result()
                                    if action=='read_analytics' and isinstance(answer,dict):
                                        from datetime import datetime,timezone
                                        answer.setdefault('timestamps',{})['mcp_published_at']=datetime.now(timezone.utc).isoformat()
                                    record.update(status='COMPLETED',result=answer)
                                except Exception as error:record.update(status='FAILED',result={
                                    'error':'Command failed; inspect receipt and local diagnostics before retrying',
                                    'code':'PAPER_FAILED' if action=='paper_trade' else read_error_code(error),
                                    'execution':'UNCONFIRMED' if action=='paper_trade' else 'NOT_APPLICABLE'})
                            self.record_event(record);record['done'].set()
                    result.add_done_callback(finish)
                    return
                awaiting=action=='set_levels' and isinstance(result,dict) and result.get('status')=='awaiting user approval'
                with self.lock:record.update(status='AWAITING_APPROVAL' if awaiting else 'COMPLETED',result=result)
            except Exception:
                # Exceptions can contain provider credentials/URLs; do not expose them.
                with self.lock:record.update(status='FAILED',result={'error':'Command rejected or failed; inspect local application diagnostics'})
            finally:
                if not (action in ('read_analytics','paper_trade','set_indicators') and isinstance(locals().get('result'),Future)):
                    try:self.record_event(record)
                    finally:record['done'].set()
        self.events.put((call,()))
        if not record['done'].wait(min(wait,remaining)):
            with self.lock:
                if record['status']=='QUEUED':
                    record['status']='EXPIRED';record['done'].set();self.record_event(record)
                elif record['status']=='RUNNING':return {'command_id':cid,'action':action,'status':'INDETERMINATE','read_back_required':True}
        return self.status(cid)

    def close(self):
        with self.lock:
            self.closed=True
            for r in self.records.values():
                if r['status']=='QUEUED':r['status']='EXPIRED';r['done'].set()
