"""Migrate an existing private tunnel during installation, without logging keys."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid


def physical_directory(directory):
    """Resolve MSIX redirected directories before handing paths to OS tools."""
    if os.name != 'nt':
        return Path(directory).resolve()
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(directory), 0, 7, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not size:
            raise ctypes.WinError(ctypes.get_last_error())
        if size >= len(buffer):
            raise ValueError('Resolved directory path is too long')
        path = buffer.value
        if path.startswith('\\\\?\\UNC\\'):
            path = '\\\\' + path[8:]
        elif path.startswith('\\\\?\\'):
            path = path[4:]
        return Path(path)
    finally:
        kernel.CloseHandle(handle)


def prepare_tunnel(destination, data, existing):
    if not existing or not existing.get('enabled'):return None
    old=Path(existing['directory']).resolve()
    old_profile=old/'profiles'/(existing.get('profile','stock-monitor-managed')+'.yaml')
    profile=json.loads(old_profile.read_text(encoding='utf-8-sig'))
    control=profile['control_plane']
    if control.get('base_url')!='https://api.openai.com' or not re.fullmatch(r'tunnel_[A-Za-z0-9]+',control.get('tunnel_id','')):
        raise ValueError('Unexpected tunnel control plane')
    ref=control['api_key']
    if not ref.startswith('file:'):raise ValueError('Expected protected local key file')
    source_key=Path(ref[5:])
    if not source_key.is_absolute():source_key=old/source_key
    executable=Path(destination)/'tunnel-client'/'tunnel-client.exe'
    bridge=Path(destination)/'StockMonitorBridge.exe'
    if not executable.is_file() or not bridge.is_file():raise ValueError('Missing packaged tunnel or MCP bridge')
    folder=Path(data)/'chatgpt-tunnel'
    folder.mkdir(parents=True,exist_ok=True)
    folder=physical_directory(folder)
    private=folder/('private-'+uuid.uuid4().hex)
    private.mkdir(parents=True,exist_ok=False)
    if os.name!='nt':raise RuntimeError('Windows account ACL required')
    flags={'creationflags':subprocess.CREATE_NO_WINDOW,'check':True,'capture_output':True}
    # Get the identity of the installing Windows user; no hard-coded SID.
    result=subprocess.run(['whoami.exe','/user','/fo','csv','/nh'],text=True,**flags)
    sid=re.search(r'S-1-5-[0-9-]+',result.stdout)
    if sid is None:raise RuntimeError('Cannot identify Windows account')
    # New key directory: remove inherited access before writing any secret.
    subprocess.run(['icacls.exe',str(private),'/inheritance:r','/grant:r','*'+sid.group(0)+':(OI)(CI)F'],**flags)
    key=private/'runtime-key.txt'
    if source_key.resolve()!=key.resolve():
        temporary=private/'runtime-key.pending'
        try:
            shutil.copyfile(source_key,temporary)
            subprocess.run(['icacls.exe',str(temporary),'/inheritance:r','/grant:r','*'+sid.group(0)+':F'],**flags)
            temporary.replace(key)
        finally:
            temporary.unlink(missing_ok=True)
    command='"'+str(bridge.resolve()).replace('\\','/')+'"'
    profile['control_plane']['api_key']='file:'+str(key.resolve()).replace('\\','/')
    profile['mcp']={'commands':[{'channel':'main','command':command}]}
    profile['health']={'listen_addr':'127.0.0.1:0','url_file':str(folder/'health.url')}
    profile['log']={'file':str(folder/'tunnel.log'),'format':'json','level':'info'}
    profile['admin_ui']={'open_browser':False}
    profiles=folder/'profiles';profiles.mkdir(exist_ok=True)
    target=profiles/'stock-monitor-managed.yaml';temporary=target.with_suffix('.pending')
    temporary.write_text(json.dumps(profile,indent=2),encoding='utf-8');temporary.replace(target)
    return {'enabled':True,'directory':str(folder),'executable':str(executable.resolve()),'profile':'stock-monitor-managed','alias':'stock-monitor','expected_command':command}
