"""Bounded background dispatch; callbacks are delivered by the existing Tk pump."""
import json
import os
import threading
import time
from pathlib import Path


def resource_sample(previous=None):
    """Read Windows counters, never synchronously from Tk."""
    if os.name!='nt':return {'cpu':None,'ram':None},None
    import ctypes
    from ctypes import wintypes
    class Memory(ctypes.Structure):
        _fields_=[('length',wintypes.DWORD),('load',wintypes.DWORD)]+[(n,ctypes.c_ulonglong) for n in ('total','avail','page','avail_page','virtual','avail_virtual','extended')]
    mem=Memory();mem.length=ctypes.sizeof(mem)
    ram=mem.load if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)) else None
    idle=wintypes.FILETIME();kernel=wintypes.FILETIME();user=wintypes.FILETIME()
    cpu=None;current=None
    if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle),ctypes.byref(kernel),ctypes.byref(user)):
        def value(x):return (x.dwHighDateTime<<32)|x.dwLowDateTime
        current=(value(idle),value(kernel)+value(user))
        if previous and current[1]>previous[1]:cpu=max(0.,min(100.,100*(1-(current[0]-previous[0])/(current[1]-previous[1]))))
    return {'cpu':cpu,'ram':ram},current


class Dispatcher:
    def __init__(self,deliver,path=None,workers=4,sample=resource_sample):
        self.deliver=deliver;self.path=Path(path) if path else None
        self.limit=max(2,workers);self.sample=sample;self.cv=threading.Condition()
        self.pending={};self.running={};self.sequence=0;self.closed=False
        self.visible=None;self.positions=set();self.completed={};self.resume=set()
        self.loaded=False;self.dirty=False;self.persistence_error=None;self.timings={};self.pressure={'cpu':None,'ram':None};self.busy_until=0
        self.counts={'submitted':0,'completed':0,'failed':0,'deduplicated':0,'cancelled':0}
        self.threads=[threading.Thread(target=self._worker,daemon=True,name=f'performance-{i}') for i in range(self.limit)]
        self.monitor=threading.Thread(target=self._monitor,daemon=True,name='performance-monitor');self.monitor.start()
        for thread in self.threads:thread.start()

    def focus(self,isin,positions=()):
        with self.cv:self.visible=isin;self.positions=set(positions);self.cv.notify_all()

    def sqlite_busy(self):
        with self.cv:self.busy_until=time.monotonic()+10;self.cv.notify_all()

    def submit(self,work,done,failed,*,key=None,lane='misc',isin=None,durable=False,generation=None,only_if_due=False):
        with self.cv:
            if self.closed:return False
            self.sequence+=1;key=key or ('job',self.sequence)
            if key in self.pending or key in self.running:
                self.counts['deduplicated']+=1;return False
            prior=self.completed.get(str(key),{})
            if only_if_due and self.loaded and str(key) not in self.resume and prior.get('generation')==generation and 0<=time.time()-prior.get('at',0)<60:return False
            self.pending[key]=dict(key=key,work=work,done=done,failed=failed,lane=lane,isin=isin,durable=durable,generation=generation,sequence=self.sequence,queued=time.monotonic())
            self.counts['submitted']+=1;self.dirty|=durable;self.cv.notify_all();return True

    def cancel(self,key):
        # Only queued readonly chart work. A running writer is never cancelled.
        with self.cv:
            task=self.pending.get(key)
            if task is None or task['lane']!='chart' or task['durable']:return False
            self.pending.pop(key);self.counts['cancelled']+=1
            self.deliver(task['failed'],'Superseded read-only calculation')
            self.cv.notify_all();return True

    def prioritize_feed(self,key,priority=1.5):
        """Promote one queued owner request. Never interrupt a running writer."""
        with self.cv:
            task=self.pending.get(key)
            if task is None or task['lane']!='feed_update':return False
            task['owner_priority']=max(1.1,min(1.5,float(priority)));self.cv.notify_all();return True

    def _rank(self,task):
        lane=task['lane'];isin=task['isin']
        priority=0 if lane=='chart' else .5 if lane=='trading' else 1 if lane=='read' else 2 if isin and isin==self.visible else 3 if isin in self.positions else 4 if lane=='misc' else 5 if lane=='snapshot' else 6
        if lane=='feed_update' and task.get('owner_priority'):priority=float(task['owner_priority'])
        if lane in ('snapshot','eligibility'):priority=5  # FIFO between long readonly jobs.
        # CP session polling must not starve older feed/history turns behind
        # an always-changing visible instrument. Preserve chart/read priority.
        if lane=='feed_update' and time.monotonic()-task['queued']>=300:priority=min(priority,2)
        # Previously interrupted feeds precede ordinary background feeds only.
        resumed=0 if str(task['key']) in self.resume else 1
        return priority,resumed,task['sequence']

    def _capacity(self):
        high=(self.pressure.get('cpu') or 0)>=90 or (self.pressure.get('ram') or 0)>=85 or time.monotonic()<self.busy_until
        return 2 if high else self.limit

    def _worker(self):
        while True:
            with self.cv:
                while True:
                    if self.closed:return
                    lanes={v['lane'] for v in self.running.values()}
                    capacity=self._capacity()
                    # A long all-stock read must not fill the fourth worker:
                    # it used to leave a new chart waiting for network jobs.
                    # Keep one slot for chart work at normal capacity. Under
                    # pressure retain the existing two-slot policy so reads
                    # and the single writer can still progress without a chart.
                    choices=[t for t in self.pending.values() if (t['lane'] not in lanes or t['lane']=='misc') and
                             not (t['lane'] in ('snapshot','eligibility') and lanes & {'snapshot','eligibility'}) and
                             (t['lane'] in ('chart','trading') or (t['lane']=='read' and capacity<=2) or len(self.running)<capacity-1)]
                    if self.loaded and choices and len(self.running)<capacity:
                        task=min(choices,key=self._rank);key=task['key'];self.pending.pop(key);self.running[key]=task;break
                    self.cv.wait(.1)
            error=None;result=None;started=time.monotonic()
            try:result=task['work']()
            except Exception as exc:error=str(exc)
            with self.cv:
                self.running.pop(key,None)
                elapsed=time.monotonic()-started;self.timings[task['lane']]=round(elapsed,3)
                if self.closed and task['durable']:self.resume.add(str(key))
                if not self.closed:
                    self.counts['failed' if error else 'completed']+=1
                    if task['durable']:
                        if error:self.resume.add(str(key))
                        else:self.completed[str(key)]={'at':time.time(),'generation':task['generation']};self.resume.discard(str(key))
                        self.dirty=True
                    callback=task['failed'] if error else task['done']
                    self.deliver(callback,error if error else result)
                self.cv.notify_all()

    def snapshot(self):
        with self.cv:
            return {**self.counts,'persistence_error':self.persistence_error,'last_seconds':dict(self.timings),'queued':len(self.pending),'running':len(self.running),'capacity':self._capacity(),'pressure':dict(self.pressure),'sqlite_busy':time.monotonic()<self.busy_until,'lanes':{lane:sum(t['lane']==lane for t in self.pending.values()) for lane in {t['lane'] for t in self.pending.values()}},'oldest_wait_seconds':round(max((time.monotonic()-t['queued'] for t in self.pending.values()),default=0),3)}

    def _monitor(self):
        if self.path:
            try:
                state=json.loads(self.path.read_text(encoding='utf-8'))
                if state.get('schema')==1:
                    self.completed={k:v for k,v in state.get('completed',{}).items() if isinstance(v,dict) and isinstance(v.get('at'),(int,float))}
                    self.resume={k for k in state.get('pending',[]) if isinstance(k,str)}
            except (OSError,ValueError,TypeError,AttributeError):pass
        with self.cv:self.loaded=True;self.cv.notify_all()
        previous=None
        while True:
            with self.cv:
                pending={str(t['key']) for t in [*self.pending.values(),*self.running.values()] if t['durable']}|self.resume
                completed={k:v for k,v in self.completed.items() if time.time()-v.get('at',0)<86400}
                dirty=self.dirty;self.dirty=False;closed=self.closed
            if self.path and (dirty or closed):
                try:
                    tmp=self.path.with_suffix('.tmp');tmp.write_text(json.dumps({'schema':1,'pending':sorted(pending),'completed':completed}),encoding='utf-8');tmp.replace(self.path)
                    self.persistence_error=None
                except OSError as error:self.persistence_error=type(error).__name__;self.dirty=True
            if closed:return
            try:pressure,previous=self.sample(previous)
            except Exception:pressure={'cpu':None,'ram':None}
            with self.cv:self.pressure=pressure;self.cv.wait(.5)

    def close(self):
        with self.cv:self.closed=True;self.dirty=True;self.cv.notify_all()
