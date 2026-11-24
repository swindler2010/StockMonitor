"""Feed-supplied previews only; bounded public reads and local image cache."""
import hashlib,io,json,time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit,urljoin
from PIL import Image,ImageOps
from logos016 import public_url


def image_url(value):
    from news_engine import safe_url
    value=safe_url(value)
    return value if value.startswith('https://') and len(value)<=4096 else ''


def feed_image(item):
    candidates=[]
    for node in item.iter():
        tag=node.tag.rsplit('}',1)[-1]
        if tag in ('thumbnail','content') and node.get('url') and (tag=='thumbnail' or node.get('medium')=='image' or node.get('type','').startswith('image/')):
            candidates.append(node.get('url'))
        if tag in ('enclosure','link') and node.get('type','').startswith('image/'):
            candidates.append(node.get('url') or node.get('href'))
    return next((url for x in candidates if (url:=image_url(x))), '')


def load(url,directory,session=None,clock=time.time):
    """Decode/resize in a worker. No article scraping or generated artwork."""
    import requests
    url=image_url(url)
    if not url:raise ValueError('NO_SOURCE_PREVIEW')
    directory=Path(directory);key=hashlib.sha256(url.encode()).hexdigest()
    path=directory/('news-preview-'+key+'.png');note=path.with_suffix('.json');now=clock()
    try:
        if 0<=now-json.loads(note.read_text())['at']<86400:
            with Image.open(path) as im:
                if im.size!=(160,96):raise ValueError('BAD_CACHED_PREVIEW')
                return im.convert('RGB').copy()
    except (OSError,ValueError,KeyError,TypeError):pass
    client=session or requests;started=clock()
    for _ in range(4):
        public_url(url);host=urlsplit(url).hostname
        cooldown=directory/('news-preview-cooldown-'+hashlib.sha256(host.encode()).hexdigest()+'.json')
        try:
            if json.loads(cooldown.read_text())['until']>clock():raise RuntimeError('PREVIEW_SOURCE_COOLDOWN')
        except (OSError,ValueError,KeyError):pass
        with client.get(url,timeout=(3,5),stream=True,allow_redirects=False,
                        headers={'User-Agent':'StockMonitor/0.16 public feed preview','Accept':'image/*'}) as response:
            if response.status_code in (429,403):
                retry=response.headers.get('Retry-After','3600')
                try:until=clock()+max(60,int(retry))
                except (ValueError,TypeError):
                    try:until=max(clock()+60,parsedate_to_datetime(retry).timestamp())
                    except (ValueError,TypeError,OverflowError):until=clock()+3600
                cooldown.write_text(json.dumps({'until':until}),encoding='utf-8')
                raise RuntimeError('PREVIEW_SOURCE_COOLDOWN')
            if response.is_redirect:
                url=image_url(urljoin(url,response.headers.get('Location','')))
                if not url:raise ValueError('INVALID_PREVIEW_REDIRECT')
                continue
            response.raise_for_status();parts=[];size=0
            for chunk in response.iter_content(16384):
                size+=len(chunk)
                if size>1_500_000 or clock()-started>20:raise ValueError('PREVIEW_LIMIT')
                parts.append(chunk)
            with Image.open(io.BytesIO(b''.join(parts))) as im:
                if im.width*im.height>4_000_000 or im.width<48 or im.height<48:raise ValueError('PREVIEW_DIMENSIONS')
                result=ImageOps.fit(im.convert('RGB'),(160,96),Image.Resampling.LANCZOS)
            result.save(path,format='PNG');note.write_text(json.dumps({'at':clock()}),encoding='utf-8')
            # Only this bounded, reproducible preview cache; no feed evidence.
            previews=sorted((p for p in directory.glob('news-preview-*.png') if len(p.stem.removeprefix('news-preview-'))==64 and all(c in '0123456789abcdef' for c in p.stem.removeprefix('news-preview-'))),key=lambda p:p.stat().st_mtime)
            for old in previews[:-48]:
                if old.resolve().parent!=directory.resolve():continue
                old.unlink(missing_ok=True);old.with_suffix('.json').unlink(missing_ok=True)
            return result
    raise ValueError('PREVIEW_REDIRECT_LIMIT')
