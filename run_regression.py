"""Isolated publication regression. Run from a source checkout, never a live profile.

Example: python run_regression.py --root . --artifacts <existing-temporary-dir>
The runner blocks external network, non-temporary SQLite, GUI and subprocesses.
It permits only loopback listeners created in this process on ephemeral ports.
"""
import argparse, datetime, json, os, socket, sys, tempfile, time, unittest, weakref
from pathlib import Path
from urllib.parse import unquote, urlsplit

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root',type=Path,default=Path.cwd())
parser.add_argument('--artifacts',type=Path,required=True)
parser.add_argument('--modules',nargs='*')
args=parser.parse_args()
ROOT=args.root.resolve();ARTIFACTS=args.artifacts.resolve()
if not ARTIFACTS.is_dir():parser.error('--artifacts must be an existing temporary directory')
if not (ROOT/'stock_monitor').is_dir():parser.error('--root must contain stock_monitor')
if ARTIFACTS.is_relative_to(ROOT/'test-data'):parser.error('Live profile cannot hold test artifacts')
os.environ.update(TEMP=str(ARTIFACTS),TMP=str(ARTIFACTS),MPLCONFIGDIR=str(ARTIFACTS),MPLBACKEND='Agg',PYTHONDONTWRITEBYTECODE='1')
sys.dont_write_bytecode=True;tempfile.tempdir=str(ARTIFACTS)
original_mkdtemp=tempfile.mkdtemp
def temporary_directory(suffix=None,prefix=None,dir=None):
    return original_mkdtemp(suffix=suffix,prefix='publication-test-'+(prefix or ''),dir=ARTIFACTS)
tempfile.mkdtemp=temporary_directory
sys.path[:0]=[str(ROOT),str(ROOT/'stock_monitor')]
blocked=[];listeners=weakref.WeakSet()
def local_address(address):
    return isinstance(address,tuple) and address[0] in ('127.0.0.1','::1','localhost')
original_bind=socket.socket.bind
def bind(sock,address):
    if not local_address(address) or address[1]!=0:
        raise RuntimeError('REGRESSION_FORBIDS_NON_EPHEMERAL_LISTENER')
    result=original_bind(sock,address);listeners.add(sock);return result
socket.socket.bind=bind
def test_listener(address):
    if not local_address(address):return False
    for sock in list(listeners):
        try:
            if sock.getsockname()[1]==address[1]:return True
        except OSError:pass
    return False
def sqlite_path(value):
    value=str(value)
    if value.startswith('file:'):
        value=unquote(urlsplit(value).path)
        if os.name=='nt' and len(value)>2 and value[0]=='/' and value[2]==':':value=value[1:]
    return Path(value).resolve()
def guard(event,values):
    reason=None
    if event in ('socket.connect','socket.connect_ex') and not test_listener(values[1]):reason='EXTERNAL_OR_UNOWNED_NETWORK'
    if event in ('subprocess.Popen','os.system'):reason='SUBPROCESS_NOT_IN_UNIT_SCOPE'
    if event=='sqlite3.connect':
        value=str(values[0])
        if value!=':memory:' and 'mode=memory' not in value and not sqlite_path(value).is_relative_to(ARTIFACTS):reason='NON_TEMP_SQLITE'
    if event=='open' and len(values)>2:
        value,mode,flags=values
        if isinstance(value,(str,bytes,os.PathLike)) and os.fsdecode(value).lower() not in ('nul','/dev/null'):
            writing=(mode and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT))
            if writing and not Path(os.fsdecode(value)).resolve().is_relative_to(ARTIFACTS):reason='NON_TEMP_WRITE'
    if reason:
        blocked.append(dict(event=event,reason=reason));raise RuntimeError('REGRESSION_BLOCKED:'+reason)
sys.addaudithook(guard)
import tkinter
def no_gui(*args,**kwargs):raise RuntimeError('REGRESSION_FORBIDS_GUI')
tkinter.Tk=no_gui

DEFAULT=[
 'test_paper_hourly','test_paper_sizing','test_paper_strategy_v2',
 'test_cp_automatic_repair','test_cp_repair_fairness','test_cp_review_stability','test_cp_source_acceptance',
 'test_cp_fresh_fair_share','test_cp_trade_priority','test_cp_demo_priority','test_cp_focus_base',
 'test_cp_history_slot_release','test_cp_fresh_completion','test_cp_reserved_base','test_cp_reviewed_ranges',
 'test_live_candle','test_daily_live_slot','test_daily_normalization',
 'test_timeframes','test_data_quality','test_revision_watcher','test_candle_schedule',
 'test_history_depth','test_input_revisions','test_cp_health_completion','test_smart_eligibility',
 'test_trading_engine','test_p1_watchlist_save','test_save_exit','test_rejected_history_retry',
 'test_partial_indicators','test_partial_independent_core','test_display_history_depth','test_reviewed_entry_source',
 'test_mcp_readback','test_mcp_diagnostics','test_store_stale_bridge','test_snapshot_interleaved','test_snapshot_error_stage',
]
EXCLUDED={
 'test_cp_automatic_repair.AutomaticRepair.test_saved_real_gettex_missing_row_is_recovered_once_then_warmup_stays_blocked':'Requires private archived real-candle fixture',
 'test_cp_automatic_repair.AutomaticRepair.test_changed_inputs_during_real_recovery_prevent_ohlcv_write':'Requires private archived real-candle fixture',
 'test_input_revisions.InputRevisionTests.test_real_worker_publication_ignores_journal_but_checks_dependencies':'Subprocess integration requires separate guarded process runner',
 'test_candle_schedule.Schedule.test_long_run_all_base_tf_and_history_turns':'Long legacy simulation excluded from bounded suite; previous publication audit records separate result',
}
def flatten(suite):
    for test in suite:
        if isinstance(test,unittest.TestSuite):yield from flatten(test)
        else:yield test
def clean(text):
    return str(text).replace(str(ROOT),'<project>').replace(str(ARTIFACTS),'<artifacts>')
class Result(unittest.TextTestResult):
    def __init__(self,*a,**kw):super().__init__(*a,**kw);self.cases={}
    def startTest(self,test):super().startTest(test);self.cases[test.id()]=dict(test=test.id(),status='RUNNING',issues=[])
    def mark(self,test,status,error=None):
        row=self.cases[test.id()];rank={'RUNNING':0,'PASS':1,'SKIP':2,'FAIL':3,'ERROR':4}
        if rank[status]>rank[row['status']]:row['status']=status
        if error:row['issues'].append(clean(error))
    def addSuccess(self,test):super().addSuccess(test);self.mark(test,'PASS')
    def addSkip(self,test,reason):super().addSkip(test,reason);self.mark(test,'SKIP',reason)
    def addFailure(self,test,err):super().addFailure(test,err);self.mark(test,'FAIL',err[1])
    def addError(self,test,err):super().addError(test,err);self.mark(test,'ERROR',err[1])
    def addSubTest(self,test,subtest,err):
        super().addSubTest(test,subtest,err)
        if err:self.mark(test,'FAIL' if issubclass(err[0],test.failureException) else 'ERROR',str(subtest)+': '+str(err[1]))

start=time.time();report=dict(generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),scope='Isolated fixtures; no live application DB, providers, GUI or account',modules=[],excluded=EXCLUDED,blocked_io=blocked);seen=set()
with (ARTIFACTS/'run_regression.txt').open('w',encoding='utf-8') as log:
    for module in args.modules or DEFAULT:
        tests=[]
        for test in flatten(unittest.defaultTestLoader.loadTestsFromName(module)):
            if test.id() in seen or test.id() in EXCLUDED:continue
            seen.add(test.id());tests.append(test)
        t=time.time();log.write('\nMODULE '+module+'\n')
        result=unittest.TextTestRunner(stream=log,verbosity=2,resultclass=Result).run(unittest.TestSuite(tests))
        row=dict(module=module,seconds=round(time.time()-t,3),cases=list(result.cases.values()))
        report['modules'].append(row);report['elapsed_seconds']=round(time.time()-start,3)
        (ARTIFACTS/'run_regression.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        log.flush();print(module,json.dumps({s:sum(c['status']==s for c in row['cases']) for s in ('PASS','FAIL','ERROR','SKIP')}),flush=True)
print('TOTAL_UNIQUE',len(seen),'SECONDS',round(time.time()-start,3),flush=True)
sys.exit(1 if any(c['status'] in ('FAIL','ERROR') for r in report['modules'] for c in r['cases']) else 0)
