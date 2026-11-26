"""Isolated CPU calculations: private pipes, read-only SQLite, no Tk calls."""
import os,pickle,sqlite3,subprocess,sys,threading,time
from contextlib import contextmanager
from pathlib import Path


class SnapshotReads:
    """One owned immutable WAL view across bounded requests of one cycle."""
    def __init__(self,store):
        self.store=store;self.manager=None;self.captured=None;self.cycle=None;self.view_id=None
    def close(self):
        manager=self.manager
        self.manager=None;self.captured=None;self.cycle=None;self.view_id=None
        if manager is not None:manager.__exit__(None,None,None)
    @contextmanager
    def scope(self,cycle=None,last=False):
        from performance_cache import frozen_store
        if cycle is None:
            self.close()
            with frozen_store(self.store) as captured:yield captured,None
            return
        if not isinstance(cycle,str) or not 0<len(cycle)<=128:raise ValueError('INVALID_SNAPSHOT_CYCLE')
        if self.cycle!=cycle:
            self.close();self.manager=frozen_store(self.store)
            try:self.captured=self.manager.__enter__()
            except Exception:self.manager=None;raise
            import uuid
            self.cycle=cycle;self.view_id=uuid.uuid4().hex
        try:yield self.captured,self.view_id
        except BaseException:
            self.close();raise
        finally:
            if last:self.close()

def receive_readonly(process,deadline):
    """Read a framed private reply with a deadline, including partial replies.

    No helper thread and no effect on the serial writer's protocol/lifetime.
    """
    fd=process.stdout.fileno()
    if os.name=='nt':
        import ctypes,msvcrt
        available=ctypes.c_ulong();kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        peek=kernel.PeekNamedPipe
        peek.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong,ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong),ctypes.c_void_p]
        handle=msvcrt.get_osfhandle(fd)
    def exact(size):
        parts=[];left=size
        while left:
            if time.monotonic()>=deadline:raise TimeoutError('READ_CALCULATION_TIMEOUT')
            if os.name=='nt':
                if not peek(handle,None,0,None,ctypes.byref(available),None):raise EOFError()
                ready=available.value
            else:
                import select
                ready=65536 if select.select([fd],[],[],0)[0] else 0
            if not ready:
                if process.poll() is not None:raise EOFError()
                time.sleep(.01);continue
            block=os.read(fd,min(left,ready,65536))
            if not block:raise EOFError()
            parts.append(block);left-=len(block)
        return b''.join(parts)
    size=int.from_bytes(exact(8),'big')
    if not 0<size<=512*1024*1024:raise ValueError('READ_REPLY_SIZE_LIMIT')
    return pickle.loads(exact(size))

class Client:
    def __init__(self,path,feed_updates=False):
        self.feed_updates=feed_updates
        self.path=str(path);self.process=None;self.lock=threading.Lock();self.closed=False;self.epoch=0;self.active=False;self.active_kind=None
        self.timeouts={'snapshot':180,'eligibility':90,'chart':90,'read_analytics':90,'feed':90,'paper_hourly':90}

    def calculate(self,kind,values,last_client=None,progress=None):
        with self.lock:
            if self.closed:raise RuntimeError('Calculation worker is closed')
            epoch=self.epoch;self.active=True;self.active_kind=kind;process=None
            try:
                if self.process is None:
                    interpreter=getattr(sys,'_base_executable',sys.executable)
                    command=[sys.executable,'--compute-worker'] if getattr(sys,'frozen',False) else [interpreter,'-u',str(Path(__file__).resolve())]
                    if self.feed_updates:command.append('--feed-updates')
                    env=dict(os.environ);env['PYTHONPATH']=os.pathsep.join(sys.path)
                    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
                    if kind=='snapshot' and not self.feed_updates:flags|=getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0)
                    self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                        env=env,creationflags=flags)
                process=self.process
                if self.closed or epoch!=self.epoch:
                    process.terminate();raise RuntimeError('Superseded read-only calculation')
                request_values={**values,'_read_progress_protocol':1} if kind=='snapshot' and not self.feed_updates else values
                pickle.dump((self.path,kind,request_values,last_client),process.stdin,protocol=5);process.stdin.flush()
                if self.feed_updates:ok,result=pickle.load(process.stdout)
                else:
                    budget=self.timeouts.get(kind,90)
                    if kind=='snapshot':budget=min(1800,max(budget,20*len(values.get('_snapshot_targets',values.get('watchlist',[])))))
                    deadline=time.monotonic()+budget
                    while True:
                        ok,result=receive_readonly(process,deadline)
                        if ok!='PROGRESS':break
                        if progress:progress(result)
                if epoch!=self.epoch:raise RuntimeError('Superseded read-only calculation')
                if not ok:raise RuntimeError(result)
                return result
            except TimeoutError:
                if not self.feed_updates:
                    self.epoch+=1
                    if process is not None and process.poll() is None:process.terminate()
                raise RuntimeError('READ_CALCULATION_TIMEOUT:'+kind) from None
            except (EOFError,BrokenPipeError,OSError):
                raise RuntimeError('Superseded read-only calculation' if epoch!=self.epoch else 'Calculation worker stopped unexpectedly') from None
            finally:
                self.active=False;self.active_kind=None
                if process is not None and (self.closed or epoch!=self.epoch or process.poll() is not None):
                    if process.poll() is None:process.terminate()
                    process.wait(timeout=5);process.stdin.close();process.stdout.close()
                    if self.process is process:self.process=None

    def interrupt(self,kind=None):
        # Never interrupt a writer mid-job. Only disposable readonly calculations.
        if self.feed_updates or not self.active or (kind is not None and self.active_kind!=kind):return False
        self.epoch+=1
        process=self.process
        if process is not None and process.poll() is None:process.terminate()
        return True

    def close(self):
        self.closed=True
        # Let the serial writer finish its transaction/request. calculate's
        # finally closes it after receiving the response; readonly workers are
        # disposable. Never terminate a writer halfway through a CP turn.
        if self.feed_updates and self.active:return
        process=self.process
        if process is not None and process.poll() is None:process.terminate()

def diagnostic_error(error):
    # Locations only: no locals, source lines, credentials or full paths.
    frames=[];tb=error.__traceback__
    while tb is not None:
        code=tb.tb_frame.f_code
        frames.append(f'{Path(code.co_filename).name}:{code.co_name}:{tb.tb_lineno}')
        tb=tb.tb_next
    return type(error).__name__+': '+str(error)+' [stage='+'>'.join(frames[-8:])+']'

def serve():
    from storage import Store
    from performance_cache import ReadCache
    class ExistingStore(Store):
        def __init__(self,path):
            self.path=Path(path)
            if not self.path.is_file():raise FileNotFoundError(path)
            self.directory=self.path.parent;self.read_cache=ReadCache(path)
    class ReadOnlyStore(ExistingStore):
        _serial_readonly=True
        @contextmanager
        def connect(self):
            db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)
            try:
                db.execute('PRAGMA query_only=ON');yield db
            finally:db.close()
    feed_updates='--feed-updates' in sys.argv
    store=None;feed_context=None;snapshot_reads=None
    while True:
        try:path,kind,values,last_client=pickle.load(sys.stdin.buffer)
        except EOFError:break
        try:
            calculation_started=time.monotonic()
            if feed_updates != (kind=='feed_update'):raise ValueError('Worker capability mismatch')
            if store is None:store=(ExistingStore if feed_updates else ReadOnlyStore)(path)
            if str(store.path)!=path:raise ValueError('Worker database cannot change')
            if feed_updates:
                from feed_worker import FeedContext
                if feed_context is None:feed_context=FeedContext(store)
                result=feed_context.calculate(values['info'])
            elif kind=='feed_plan':
                from candle_schedule import planned_feeds
                result=planned_feeds(store,values['watchlist'],now=values.get('now'),focus=values.get('focus'))
            elif kind=='watchlist_order':
                from watchlist_order import collect
                result=collect(store.path,values['watchlist'],now=values.get('now'))
            elif kind=='eligibility':
                from calculation_window import calculate_eligibility
                result=calculate_eligibility(store,values['info'])
                if result is None:raise ValueError('ELIGIBILITY_INSTRUMENT_NOT_FOUND')
                store.read_cache.assert_current(result.get('_input_revisions',{}))
            elif kind=='real_v1':
                from analytical_read import calculate_consistent
                from real_portfolio import calculate as real_advice
                result=calculate_consistent(store,values,real_advice)
            elif kind in ('chart','read_analytics','feed'):
                from analytical_read import calculate_consistent
                if kind=='chart':
                    from chart_worker import prepare
                    result=calculate_consistent(store,values,prepare)
                elif kind=='feed':
                    from chart_worker import prepare_feed
                    result=calculate_consistent(store,values,prepare_feed)
                else:result=calculate_consistent(store,values)
            elif kind=='snapshot':
                from snapshot_worker import calculate,validate_snapshot
                from performance_cache import frozen_store
                from datetime import datetime,timezone
                wants_progress=values.pop('_read_progress_protocol',None)==1
                # A bounded all-list cycle shares one original closed cutoff.
                # Later batches must not include candles formed after it.
                cycle_at=values.pop('_analysis_cycle_at',None)
                cycle_id=values.pop('_snapshot_cycle_id',None)
                last_batch=values.pop('_snapshot_last_batch',False)
                values={**values,'analysis_at':cycle_at or datetime.now(timezone.utc).isoformat()}
                def progress(value):
                    payload=pickle.dumps(('PROGRESS',value),protocol=5)
                    sys.stdout.buffer.write(len(payload).to_bytes(8,'big'));sys.stdout.buffer.write(payload);sys.stdout.buffer.flush()
                if snapshot_reads is None:snapshot_reads=SnapshotReads(store)
                with snapshot_reads.scope(cycle_id,last_batch) as (captured_store,view_id):
                    with captured_store.read_cache.track_inputs() as dependencies:
                        captured=values.get('instrument_revisions',{})
                        captured_store.read_cache.assert_current(captured)
                        captured_store.read_cache._record(captured)
                        result=calculate(captured_store,values,last_client,progress=progress if wants_progress else None)
                        captured_store.read_cache.assert_current(dependencies)
                    result['_captured_input_revisions']=dict(dependencies)
                    if view_id is not None:result['_snapshot_view_id']=view_id
                result=validate_snapshot(store,result)
            elif kind=='paper_hourly':
                from paper_hourly import calculate_snapshot
                from performance_cache import InputRevisionChanged
                try:result=calculate_snapshot(store,values)
                except InputRevisionChanged:result=calculate_snapshot(store,values)
            else:raise ValueError('Unknown calculation')
            result.setdefault('_calculation_timing',{}).update(compute_seconds=round(time.monotonic()-calculation_started,3),finished_at=time.time())
            response=(True,result)
        except Exception as error:response=(False,diagnostic_error(error))
        if feed_updates:pickle.dump(response,sys.stdout.buffer,protocol=5)
        else:
            payload=pickle.dumps(response,protocol=5)
            sys.stdout.buffer.write(len(payload).to_bytes(8,'big'));sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    if snapshot_reads is not None:snapshot_reads.close()
    if store is not None:store.read_cache.close()

if __name__=='__main__':serve()
