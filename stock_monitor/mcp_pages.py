"""Bounded read leases for MCP transport only; never analytics admission leases."""
from collections import OrderedDict
import hashlib
import json
import threading
import time


class ReadPages:
    def __init__(self, ttl=120, max_bytes=64*1024*1024, max_entries=16, clock=time.monotonic):
        self.ttl=ttl;self.max_bytes=max_bytes;self.max_entries=max_entries;self.clock=clock
        self.entries=OrderedDict();self.lock=threading.Lock();self.bytes=0

    def read(self,section,expected,load):
        # The cache is per Bridge instance and section. No continuation silently
        # switches to a new publication or another reader's latest response.
        with self.lock:
            now=self.clock()
            for key,entry in list(self.entries.items()):
                if now-entry[2]>=self.ttl:
                    self.bytes-=entry[3];del self.entries[key]
            if expected:
                entry=self.entries.get((section,expected))
                if entry is None:raise ValueError('PAGE_SESSION_EXPIRED')
                return entry[0],expected,entry[1],max(0,self.ttl-(now-entry[2]))
        value,metadata=load()
        raw=json.dumps([value,metadata],ensure_ascii=False,allow_nan=False,sort_keys=True).encode()
        if len(raw)>self.max_bytes:raise ValueError('PAGE_SESSION_TOO_LARGE')
        revision=hashlib.sha256(raw).hexdigest()
        # Deserialize once to own a detached value, never retain live Tk objects.
        value,metadata=json.loads(raw)
        with self.lock:
            key=(section,revision)
            if key not in self.entries:
                while self.entries and (self.bytes+len(raw)>self.max_bytes or len(self.entries)>=self.max_entries):
                    _,old=self.entries.popitem(last=False);self.bytes-=old[3]
                self.entries[key]=(value,metadata,self.clock(),len(raw));self.bytes+=len(raw)
            entry=self.entries[key]
            return entry[0],revision,entry[1],max(0,self.ttl-(self.clock()-entry[2]))
