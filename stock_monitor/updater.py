"""GitHub release updater: explicit trusted repository, HTTPS, release asset digest."""
import hashlib
import re
from pathlib import Path
import requests
import json
import tempfile
from urllib.parse import urlparse,unquote

def validate_release(release):
    """Validate a release again at the download boundary, before any file/network I/O."""
    tag=release['version'];v=version(tag)
    name=release['name']
    match=re.fullmatch(r'StockMonitor-(\d+\.\d+(?:\.\d+)?)-Setup\.exe',name)
    if not match or version(match[1])!=v:raise ValueError('INSTALLER_VERSION_MISMATCH')
    url=urlparse(release['url'])
    parts=url.path.split('/')
    if (url.scheme!='https' or url.netloc!='github.com' or url.query or url.fragment or
        len(parts)!=7 or parts[3:5]!=['releases','download'] or
        unquote(parts[5])!=tag or unquote(parts[6])!=name):raise ValueError('INVALID_RELEASE_URL')
    repo=repository('/'.join(parts[1:3]))
    if release.get('repository',repo)!=repo:raise ValueError('REPOSITORY_MISMATCH')
    if not re.fullmatch(r'[a-fA-F0-9]{64}',release['sha256']):raise ValueError('INVALID_RELEASE_DIGEST')
    size=release['size']
    if type(size)!=int or not 0<size<=300_000_000:raise ValueError('INVALID_RELEASE_SIZE')
    return repo

def repository(value):
    value=value.strip().removeprefix('https://github.com/').rstrip('/')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',value):raise ValueError('Укажите GitHub в виде владелец/репозиторий.')
    return value

def version(value):
    if not re.fullmatch(r'v?\d+\.\d+(?:\.\d+)?',value):raise ValueError('Неподдерживаемый номер версии')
    parts=tuple(map(int,value.lstrip('v').split('.')))
    return parts+(0,)*(3-len(parts))

def latest(repo,current):
    repo=repository(repo)
    response=requests.get(f'https://api.github.com/repos/{repo}/releases/latest',headers={'Accept':'application/vnd.github+json'},timeout=25)
    if response.status_code==404:return None
    response.raise_for_status();release=response.json()
    if release.get('draft') or release.get('prerelease') or version(release['tag_name'])<=version(current):return None
    candidates=[a for a in release.get('assets',[]) if re.fullmatch(r'StockMonitor-[0-9.]+-Setup\.exe',a['name'])]
    if len(candidates)!=1:raise ValueError('В релизе не найден единственный установщик StockMonitor-версия-Setup.exe.')
    asset=candidates[0];url=asset['browser_download_url'];digest=asset.get('digest','')
    if not url.startswith(f'https://github.com/{repo}/releases/download/') or not re.fullmatch(r'sha256:[a-fA-F0-9]{64}',digest):raise ValueError('Нет проверяемого SHA256 или адрес установщика не соответствует репозиторию.')
    if not 0<asset['size']<=300_000_000:raise ValueError('Недопустимый размер установщика')
    result={'version':release['tag_name'],'url':url,'sha256':digest[7:].lower(),'size':asset['size'],'name':asset['name'],'repository':repo}
    validate_release(result)
    return result

def download(release,directory,progress=None):
    validate_release(release)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    # Each attempt owns a new directory. Never overwrite or clean an earlier download.
    attempt=Path(tempfile.mkdtemp(prefix='download-',dir=directory))
    destination=attempt/release['name'];partial=attempt/'installer.part'
    receipt=attempt/'verification.json'
    digest=hashlib.sha256();size=0
    try:
        if progress:progress(0,release['size'])
        with requests.get(release['url'],stream=True,timeout=(15,45)) as response:
            response.raise_for_status()
            if not response.url.startswith('https://'):raise ValueError('HTTPS required')
            with partial.open('xb') as output:
                for chunk in response.iter_content(64*1024):
                    size+=len(chunk)
                    if size>release['size']:raise ValueError('Размер загрузки превышает заявленный')
                    digest.update(chunk);output.write(chunk)
                    if progress:progress(size,release['size'])
        if size!=release['size'] or digest.hexdigest()!=release['sha256'].lower():raise ValueError('Проверка SHA256 установщика не пройдена')
        partial.rename(destination)
        receipt.write_text(json.dumps({'status':'VERIFIED','version':release['version'],'name':release['name'],
                           'bytes':size,'sha256':digest.hexdigest(),'installation':'NOT_STARTED'},indent=2),encoding='utf-8')
        return destination
    except Exception as exc:
        # Retain partial evidence; only the owner may authorize later cleanup.
        receipt.write_text(json.dumps({'status':'FAILED','error_type':type(exc).__name__,
                           'bytes':size,'installation':'NOT_STARTED'},indent=2),encoding='utf-8')
        raise

