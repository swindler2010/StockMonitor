"""Bounded UI latency diagnostics; no market data or callback arguments logged."""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback


def install(root, directory):
    original = tk.CallWrapper.__call__
    events = queue.Queue(maxsize=256)
    main_id = threading.get_ident()
    last_tick = time.monotonic()
    maximum = 0.0
    stopped = threading.Event()
    path = directory / 'ui-performance.jsonl'

    def emit(kind, **fields):
        try: events.put_nowait(dict(kind=kind, at=time.time(), pid=os.getpid(), **fields))
        except queue.Full: pass

    def wrapped(wrapper, *args):
        started = time.monotonic()
        try: return original(wrapper, *args)
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= .1:
                func=wrapper.func
                if getattr(func,'__qualname__','')=='Misc.after.<locals>.callit':
                    cells=dict(zip(func.__code__.co_freevars,func.__closure__ or ()))
                    if 'func' in cells:func=cells['func'].cell_contents
                emit('callback', name=getattr(func, '__qualname__', type(func).__name__), seconds=round(elapsed, 4))

    root._performance_emit=emit
    tk.CallWrapper.__call__ = wrapped

    def heartbeat():
        nonlocal last_tick, maximum
        now = time.monotonic()
        lag = max(0, now-last_tick-.1)
        maximum = max(maximum, lag)
        if lag >= .25: emit('heartbeat', seconds=round(lag, 4), maximum=round(maximum, 4))
        last_tick = now
        root.after(100, heartbeat)

    def writer():
        sampled = 0;queue_sampled=0
        while not stopped.is_set() or not events.empty():
            now = time.monotonic()
            dispatcher=getattr(root,'_performance_dispatcher',None)
            if dispatcher is not None and now-queue_sampled>=5:
                queue_sampled=now;emit('work_queue',**dispatcher.snapshot())
            if now-last_tick > 1 and now-sampled > 1 and not stopped.is_set():
                sampled = now
                frame = sys._current_frames().get(main_id)
                if frame:
                    frames=sys._current_frames()
                    emit('blocked_stack', seconds=round(now-last_tick, 4), stack=[f'{f.filename}:{f.lineno}:{f.name}' for f in traceback.extract_stack(frame)[-14:]],
                         threads={t.name+':'+str(t.ident):[f'{f.filename}:{f.lineno}:{f.name}' for f in traceback.extract_stack(frames[t.ident])[-8:]] for t in threading.enumerate() if t.ident in frames and t.ident!=threading.get_ident()})
            try: event = events.get(timeout=.2)
            except queue.Empty: continue
            try:
                if path.exists() and path.stat().st_size > 2_000_000:
                    path.replace(path.with_suffix('.previous.jsonl'))
                with path.open('a', encoding='utf-8') as out: out.write(json.dumps(event)+'\n')
            except OSError: pass

    def destroyed(event):
        if event.widget is root:
            emit('closed', maximum=round(maximum, 4))
            stopped.set()
            tk.CallWrapper.__call__ = original

    root.bind('<Destroy>', destroyed, add='+')
    emit('started')
    threading.Thread(target=writer, daemon=True, name='ui-performance').start()
    root.after(100, heartbeat)


class RenderTiming:
    """Small stage timings; only slow renders enter the bounded diagnostics queue."""
    def __init__(self,root):
        self.emit=getattr(root,'_performance_emit',None)
        self.started=self.last=time.perf_counter();self.stages={}
    def mark(self,name):
        now=time.perf_counter();self.stages[name]=round(now-self.last,5);self.last=now
    def finish(self):
        elapsed=time.perf_counter()-self.started
        if self.emit and elapsed>=.1:self.emit('render_stages',seconds=round(elapsed,5),stages=self.stages)
