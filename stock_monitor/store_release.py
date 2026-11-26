"""Store delivery only: isolated data copy and versioned package path repair.

Never install, change Windows trust or write the original EXE database/profile.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
from contextlib import closing
from urllib.parse import urlparse

_instance_handles={}

class AlreadyRunning(RuntimeError):
    """Expected second launch; never treat it as a startup crash."""
    pass

def claim_instance(directory):
    if os.name!='nt':return
    import ctypes
    import hashlib
    from ctypes import wintypes
    name='Local\\StockMonitorStore-'+hashlib.sha256(str(Path(directory).resolve()).casefold().encode()).hexdigest()
    if name in _instance_handles:return
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=wintypes.HANDLE
    handle=kernel.CreateMutexW(None,False,name)
    error=ctypes.get_last_error()
    if not handle:raise OSError(error,'Unable to lock Store application')
    if error==183:
        kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle(handle)
        raise AlreadyRunning('Stock Monitor Store уже запущен.')
    _instance_handles[name]=handle


def bridge_active(directory):
    """Conservatively refuse migration if the recorded local port is listening."""
    descriptor=Path(directory)/'bridge.json'
    if not descriptor.exists():return False
    obj=json.loads(descriptor.read_text(encoding='utf-8'))
    url=urlparse(obj['url'])
    if url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port:
        raise ValueError('Invalid local bridge descriptor')
    try:
        with socket.create_connection(('127.0.0.1',url.port),timeout=1):return True
    except ConnectionRefusedError:return False
    except socket.timeout:return True


def migrate_data(source,destination,allow_import=True):
    source=Path(source);destination=Path(destination)
    if source.resolve()==destination.resolve():raise ValueError('Store data must be separate')
    claim_instance(destination)
    destination.mkdir(parents=True,exist_ok=True)
    # claim_instance owns the per-directory Windows mutex. A persisted bridge
    # port may have been reused by an unrelated process after reboot; a TCP
    # listener is not proof that this application is running.
    target=destination/'stock_monitor.sqlite3'
    if target.exists() or not allow_import:return False
    original=source/'stock_monitor.sqlite3'
    if not original.is_file():return False
    if bridge_active(source):raise RuntimeError('Перед первым запуском Store-версии закройте Stock Monitor 0.14. Данные 0.14 не изменены.')
    pending=destination/'migration.pending.sqlite3'
    # No immutable=1: backup must include committed WAL data.
    try:
        with closing(sqlite3.connect(original.resolve().as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(pending)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Database integrity failure')
            if 'payload' not in [r[1] for r in dst.execute('PRAGMA table_info(instruments)')]:raise ValueError('Unsupported source database')
        # These are DPAPI ciphertext, not plaintext service keys. copy2 retains
        # file contents; the same Windows account is required for decryption.
        secrets=source/'secrets'
        if secrets.exists():
            output=destination/'secrets';output.mkdir(exist_ok=True)
            for key in secrets.glob('key_*.dpapi'):
                shutil.copyfile(key,output/key.name)
        pending.replace(target)
        (destination/'migration-from-exe.json').write_text(json.dumps({'source':str(original),'version':'0.15','method':'sqlite-backup-read-only','original_modified':False}),encoding='utf-8')
        return True
    finally:
        pending.unlink(missing_ok=True)


def prepare_runtime(store,app_dir,allow_discovery=True,prepare=None):
    from tunnel_runtime import discover_existing
    if prepare is None:
        from tunnel_setup import prepare_tunnel
        prepare=prepare_tunnel
    existing=store.setting('chatgpt_tunnel',None)
    if existing is None and allow_discovery:existing=discover_existing()
    if not existing or not existing.get('enabled'):return existing or {}
    app_dir=Path(app_dir);folder=store.directory/'chatgpt-tunnel'
    if folder.is_dir() and os.name=='nt':
        from tunnel_setup import physical_directory
        folder=physical_directory(folder)
    expected='"'+str((app_dir/'StockMonitorBridge.exe').resolve()).replace('\\','/')+'"'
    if Path(existing['directory']).resolve()!=folder.resolve():
        # Copy profile/key into this Store instance. prepare_tunnel restricts
        # the new secret directory before copying; original stays untouched.
        config=prepare(app_dir,store.directory,existing)
    else:
        profile_path=folder/'profiles'/'stock-monitor-managed.yaml'
        profile=json.loads(profile_path.read_text(encoding='utf-8-sig'))
        profile['mcp']={'commands':[{'channel':'main','command':expected}]}
        temporary=profile_path.with_suffix('.pending')
        temporary.write_text(json.dumps(profile,indent=2),encoding='utf-8');temporary.replace(profile_path)
        config={**existing,'executable':str((app_dir/'tunnel-client/tunnel-client.exe').resolve()),'expected_command':expected}
    store.set_setting('chatgpt_tunnel',config)
    return config
