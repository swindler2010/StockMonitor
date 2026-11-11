"""Supervise an existing official tunnel profile without reading its secret file."""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time


def discover_existing(home=None):
    """Reuse only the user's previously configured Stock Monitor runtime."""
    try:
        home=Path(home) if home else Path.home()
        record=json.loads((home/'.local/state/tunnel-client/processes.yaml').read_text(encoding='utf-8'))['stock-monitor']
        match=re.match(r"^'([^']+)'(?:\s|$)",record['command'])
        if not match:return {}
        executable=Path(match.group(1))
        if executable.name.lower()!='tunnel-client.exe' or executable.parent.name!='tunnel-client':return {}
        directory=executable.parent.parent
        profile=directory/'profiles'/'stock-monitor-managed.yaml'
        config=json.loads(profile.read_text(encoding='utf-8-sig'))
        if not executable.is_file() or config['control_plane']['tunnel_id']!=record['tunnel_id']:return {}
        return {'enabled':True,'directory':str(directory),'profile':'stock-monitor-managed','alias':'stock-monitor'}
    except (OSError,ValueError,KeyError,TypeError):return {}


def health_ready(payload, now=None):
    """Local liveness is insufficient: require a recent successful cloud poll.

    Three minutes allows several normal long-poll cycles and scheduling jitter.
    Missing/nonfinite/future timestamps must not claim a working ChatGPT route.
    """
    import math
    if not isinstance(payload,dict):return False
    if payload.get('result')!='ok' or payload.get('readyz',{}).get('ok') is not True:return False
    poll=payload.get('control_plane_poll',{})
    if not isinstance(poll,dict) or poll.get('ok') is not True:return False
    value=poll.get('value')
    if isinstance(value,bool):return False
    try:stamp=float(value)
    except (TypeError,ValueError):return False
    age=(time.time() if now is None else now)-stamp
    return math.isfinite(age) and -5<=age<=180


class TunnelRuntime:
    def __init__(self, config, runner=subprocess.run, submit=None):
        self.config=dict(config or {})
        self.runner=runner
        self.submit=submit
        self.state={'connected':False,'status':'not_configured','checked_at':None}
        self.lock=threading.Lock()
        self.busy=False
        self.last_attempt=0

    def snapshot(self):
        with self.lock:return dict(self.state)

    def poll(self):
        with self.lock:
            if self.busy:return
            self.busy=True
        try:
            if self.submit is not None:
                accepted=self.submit(self._work)
                if accepted is False:
                    with self.lock:self.busy=False
                return accepted
            threading.Thread(target=self._work,daemon=True).start()
            return True
        except Exception:
            with self.lock:self.busy=False
            self._set(False,'scheduling_error')
            return False

    def _run(self, executable, args, cwd, timeout=15):
        options={'cwd':cwd,'capture_output':True,'text':True,'encoding':'utf-8','errors':'replace','timeout':timeout}
        if os.name=='nt':options['creationflags']=subprocess.CREATE_NO_WINDOW
        # Never use shell=True. Credentials stay in their protected local file.
        return self.runner([str(executable),*args],**options)

    def _work(self):
        startup_lock=None
        try:
            if not self.config.get('enabled'):
                self._set(False,'disabled');return
            folder=Path(self.config['directory']).resolve()
            if os.name=='nt':
                import msvcrt
                startup_lock=(folder/'.stock-monitor-start.lock').open('a+b')
                startup_lock.seek(0,2)
                if startup_lock.tell()==0:startup_lock.write(b'0');startup_lock.flush()
                startup_lock.seek(0)
                try:msvcrt.locking(startup_lock.fileno(),msvcrt.LK_NBLCK,1)
                except OSError:
                    startup_lock.close();startup_lock=None
                    self._set(False,'another_instance_checking');return
            executable=Path(self.config.get('executable',folder/'tunnel-client'/'tunnel-client.exe'))
            profile_name=self.config.get('profile','stock-monitor-managed')
            alias=self.config.get('alias','stock-monitor')
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',profile_name) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',alias):
                raise ValueError('Invalid profile name')
            profile_path=folder/'profiles'/(profile_name+'.yaml')
            # Official runtimes connect writes JSON into its .yaml profile file.
            profile=json.loads(profile_path.read_text(encoding='utf-8-sig'))
            health_file=profile['health']['url_file']
            expected=self.config.get('expected_command')
            if expected:
                metadata=Path.home()/'.local/state/tunnel-client/processes.yaml'
                try:record=json.loads(metadata.read_text(encoding='utf-8')).get(alias,{})
                except FileNotFoundError:record={}
                if record and record.get('target_value')!=expected:
                    if record.get('tunnel_id')!=profile['control_plane']['tunnel_id']:raise ValueError('Alias belongs to another tunnel')
                    stopped=self._run(executable,['runtimes','stop',alias],folder,30)
                    if stopped.returncode!=0:self._set(False,'old_runtime_stop_failed');return
            def health():
                result=self._run(executable,['health','--url-file',health_file,'--require-control-plane-poll','--json'],folder)
                try:payload=json.loads(result.stdout)
                except ValueError:return False
                return result.returncode==0 and health_ready(payload)
            if health():self._set(True,'connected');return
            if time.monotonic()-self.last_attempt<60:
                self._set(False,'disconnected');return
            self.last_attempt=time.monotonic()
            control=profile['control_plane'];key_ref=control['api_key']
            if not key_ref.startswith('file:'):raise ValueError('Expected protected local key file reference')
            commands=profile['mcp']['commands']
            if len(commands)!=1 or commands[0].get('channel')!='main':raise ValueError('Unsupported tunnel profile')
            tunnel_id=control['tunnel_id']
            if not re.fullmatch(r'tunnel_[A-Za-z0-9]+',tunnel_id):raise ValueError('Invalid tunnel identifier')
            if control.get('base_url')!='https://api.openai.com':raise ValueError('Unexpected control plane')
            self._set(False,'connecting')
            result=self._run(executable,['runtimes','connect','--alias',alias,'--tunnel-id',tunnel_id,'--profile',profile_name,'--profile-dir',str(folder/'profiles'),'--runtime-api-key',key_ref,'--mcp-command',commands[0]['command']],folder,60)
            if result.returncode!=0:
                self._set(False,'start_failed');return
            connected=health()
            self._set(connected,'connected' if connected else 'disconnected')
        except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
            # Do not put raw CLI output or configuration (possibly credentials) in snapshots.
            self._set(False,'configuration_or_process_error')
        finally:
            if startup_lock is not None:
                startup_lock.seek(0)
                msvcrt.locking(startup_lock.fileno(),msvcrt.LK_UNLCK,1)
                startup_lock.close()
            with self.lock:self.busy=False

    def _set(self,connected,status):
        with self.lock:self.state={'connected':connected,'status':status,'checked_at':time.time()}
