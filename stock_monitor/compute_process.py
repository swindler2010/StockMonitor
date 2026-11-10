"""Isolated CPU calculations: private pipes, read-only SQLite, no Tk calls."""
import os,pickle,sqlite3,subprocess,sys,threading,time
from contextlib import contextmanager
from pathlib import Path

class Client:
    def __init__(self,path,feed_updates=False):
        self.feed_updates=feed_updates
        self.path=str(path);self.process=None;self.lock=threading.Lock();self.closed=False;self.epoch=0;self.active=False;self.active_kind=None

    def calculate(self,kind,values,last_client=None):
        with self.lock:
            if self.closed:raise RuntimeError('Calculation worker is closed')
            epoch=self.epoch;self.active=True;self.active_kind=kind;process=None
            try:
                if self.process is None:
                    interpreter=getattr(sys,'_base_executable',sys.executable)
                    command=[sys.executable,'--compute-worker'] if getattr(sys,'frozen',False) else [interpreter,'-u',str(Path(__file__).resolve())]
                    if self.feed_updates:command.append('--feed-updates')
                    env=dict(os.environ);env['PYTHONPATH']=os.pathsep.join(sys.path)
                    self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                        env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                process=self.process
                if self.closed or epoch!=self.epoch:
                    process.terminate();raise RuntimeError('Superseded read-only calculation')
                pickle.dump((self.path,kind,values,last_client),process.stdin,protocol=5);process.stdin.flush()
                ok,result=pickle.load(process.stdout)
                if epoch!=self.epoch:raise RuntimeError('Superseded read-only calculation')
                if not ok:raise RuntimeError(result)
                return result
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
        @contextmanager
        def connect(self):
            db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)
            try:
                db.execute('PRAGMA query_only=ON');yield db
            finally:db.close()
    feed_updates='--feed-updates' in sys.argv
    store=None;feed_context=None
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
            elif kind=='eligibility':
                from calculation_window import calculate_eligibility
                result=calculate_eligibility(store,values['info'])
                if result is None:raise ValueError('ELIGIBILITY_INSTRUMENT_NOT_FOUND')
                store.read_cache.assert_current(result.get('_input_revisions',{}))
            elif kind=='snapshot':
                from snapshot_worker import calculate,validate_snapshot
                from performance_cache import frozen_store
                from datetime import datetime,timezone
                values={**values,'analysis_at':datetime.now(timezone.utc).isoformat()}
                with frozen_store(store) as captured_store:
                    with captured_store.read_cache.track_inputs() as dependencies:
                        captured=values.get('instrument_revisions',{})
                        captured_store.read_cache.assert_current(captured)
                        captured_store.read_cache._record(captured)
                        result=calculate(captured_store,values,last_client)
                        captured_store.read_cache.assert_current(dependencies)
                    result['_captured_input_revisions']=dict(dependencies)
                result=validate_snapshot(store,result)
            else:
                with store.read_cache.track_inputs() as dependencies:
                    captured=values.get('instrument_revisions',{})
                    store.read_cache.assert_current(captured)
                    store.read_cache._record(captured)
                    if kind=='chart':
                        from chart_worker import prepare
                        result=prepare(store,values)
                    elif kind=='snapshot':
                        from snapshot_worker import calculate
                        result=calculate(store,values,last_client)
                    elif kind=='read_analytics':
                        from analytical_read import calculate
                        result=calculate(store,values)
                    elif kind=='feed':
                        from chart_worker import prepare_feed
                        result=prepare_feed(store,values)
                    elif kind=='paper_hourly':
                        from paper_hourly import calculate
                        result=calculate(store,values)
                    else:raise ValueError('Unknown calculation')
                    store.read_cache.assert_current(dependencies)
                    result['_input_revisions']=dict(dependencies)
            result.setdefault('_calculation_timing',{}).update(compute_seconds=round(time.monotonic()-calculation_started,3),finished_at=time.time())
            response=(True,result)
        except Exception as error:response=(False,diagnostic_error(error))
        pickle.dump(response,sys.stdout.buffer,protocol=5);sys.stdout.buffer.flush()
    if store is not None:store.read_cache.close()

if __name__=='__main__':serve()
