"""Reuse pure calculations in the existing bounded worker cache.

Exact input bytes, including all OHLCV/metadata, flags and the as-of clock.
No readiness, revision, calendar or execution gate is relaxed or cached here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import pickle
import pandas as pd

_active=ContextVar('calculation_reuse',default=None)


@contextmanager
def using(cache):
    token=_active.set(cache)
    try:yield
    finally:_active.reset(token)


def exact(function):
    @wraps(function)
    def call(*args,**kwargs):
        cache=_active.get()
        # Functions with implicit wall clocks cannot reuse a previous result.
        now=kwargs.get('now',args[3] if function.__name__=='audit' and len(args)>3
            else args[2] if function.__name__=='_safe_features' and len(args)>2 else None)
        if cache is None or now is None:return function(*args,**kwargs)
        # DataFrame pickle includes mutable pandas index caches. Serialize
        # logical values/types/metadata instead, never those internal caches.
        frame=args[0]
        values=(tuple(str(t) for t in frame.dtypes),tuple(frame.columns),
            type(frame.index).__name__,str(frame.index.dtype),tuple(frame.index.names),
            frame.index.tolist(),pd.DataFrame(frame,copy=False).to_numpy().tolist(),frame.attrs)
        payload=pickle.dumps((values,args[1:],kwargs),protocol=5)
        key=('pure_calculation_v1',function.__module__,function.__name__,hashlib.sha256(payload).digest())
        return cache.get(key,lambda:function(*args,**kwargs),category='analytics',content_addressed=True)
    return call
