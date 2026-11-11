"""Bounded sequential RSS transport. No scraping, cookie or limit bypass."""
import hashlib,json,time,threading
from pathlib import Path
from urllib.parse import urlsplit

class FeedError(Exception):
    def __init__(self,code,retry_at=None):self.code,self.retry_at=code,retry_at;super().__init__(code)

class FeedClient:
    def __init__(self,directory,session=None,clock=time.time,sleep=time.sleep):
        import requests
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.session=session or requests.Session();self.clock=clock;self.sleep=sleep
        self.last={};self.blocked={};self.lock=threading.Lock();self.last_receipt={}

    def __call__(self,url):
        host=urlsplit(url).hostname
        if urlsplit(url).scheme!='https' or host not in {'feeds.finance.yahoo.com','news.google.com','www.apple.com','www.ecb.europa.eu','www.federalreserve.gov'}:
            raise FeedError('SOURCE_NOT_ALLOWLISTED')
        key=hashlib.sha256(url.encode()).hexdigest();path=self.directory/(key+'.json')
        with self.lock:
            now=self.clock();cached={}
            cooldown=self.directory/('cooldown-'+host+'.json')
            try:self.blocked[host]=max(self.blocked.get(host,0),json.loads(cooldown.read_text()).get('retry_at',0))
            except (OSError,ValueError,TypeError):pass
            try:cached=json.loads(path.read_text(encoding='utf-8'))
            except (OSError,ValueError):pass
            if now-cached.get('at',0)<1800 and cached.get('body'):
                self.last_receipt={'status':'CACHE','at':cached['at'],'url':url}
                return cached['body'].encode('utf-8')
            if self.blocked.get(host,0)>now:raise FeedError('PROVIDER_COOLDOWN',self.blocked[host])
            delay=2-(now-self.last.get(host,0))
            if delay>0:self.sleep(delay)
            self.last[host]=self.clock()
            headers={'User-Agent':'StockMonitor/0.17 personal RSS reader','Accept':'application/rss+xml, application/atom+xml, application/xml, text/xml'}
            if cached.get('etag'):headers['If-None-Match']=cached['etag']
            if cached.get('modified'):headers['If-Modified-Since']=cached['modified']
            try:
                with self.session.get(url,timeout=(4,10),headers=headers,stream=True,allow_redirects=False) as response:
                    status=response.status_code
                    if status in (429,403):
                        retry=response.headers.get('Retry-After','3600')
                        try:seconds=max(1800,min(86400,int(retry)))
                        except ValueError:seconds=3600
                        self.blocked[host]=self.clock()+seconds
                        cooldown.write_text(json.dumps({'retry_at':self.blocked[host],'status':status}),encoding='utf-8')
                        raise FeedError('HTTP_'+str(status),self.blocked[host])
                    if status==304 and cached.get('body'):body=cached['body']
                    elif status==200:
                        parts=[];size=0;started=self.clock()
                        for chunk in response.iter_content(16384):
                            size+=len(chunk)
                            if size>2_000_000:raise FeedError('FEED_TOO_LARGE')
                            if self.clock()-started>20:raise FeedError('FEED_DEADLINE')
                            parts.append(chunk)
                        body=b''.join(parts).decode('utf-8-sig')
                    else:raise FeedError('HTTP_'+str(status))
                    value={'at':self.clock(),'body':body,'etag':response.headers.get('ETag',cached.get('etag')),'modified':response.headers.get('Last-Modified',cached.get('modified'))}
                    temp=path.with_suffix('.pending');temp.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8');temp.replace(path)
                    self.last_receipt={'status':'HTTP_'+str(status),'at':value['at'],'url':url}
                    return body.encode('utf-8')
            except FeedError:raise
            except Exception as exc:raise FeedError(type(exc).__name__) from None
