"""Bound direct read concurrency/rate; transport-only, never an execution gate."""
import threading,time
class ReadAccess:
    def __init__(self,rate=100,burst=500,concurrency=8,clock=time.monotonic):
        self.rate=rate;self.burst=burst;self.capacity=concurrency;self.clock=clock
        self.lock=threading.Lock();self.tokens=float(burst);self.at=clock();self.active=0;self.rejected=0;self.audit_failures=0;self.audited=0
    def acquire(self):
        with self.lock:
            now=self.clock();self.tokens=min(self.burst,self.tokens+max(0,now-self.at)*self.rate);self.at=now
            if self.active>=self.capacity or self.tokens<1:self.rejected+=1;return False
            self.active+=1;self.tokens-=1;return True
    def release(self):
        with self.lock:self.active=max(0,self.active-1)
    def record_audit(self,success):
        with self.lock:
            if success:self.audited+=1
            else:self.audit_failures+=1
    def status(self):
        with self.lock:return dict(active=self.active,concurrency_limit=self.capacity,requests_per_second=self.rate,burst=self.burst,rejected=self.rejected,audited=self.audited,audit_failures=self.audit_failures)
