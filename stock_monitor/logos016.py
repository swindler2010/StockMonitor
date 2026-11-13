from i18n import tr
"""Small company site icons; network and decoding stay off the UI thread."""
import hashlib,io,time,json,ipaddress,socket
from pathlib import Path
from urllib.parse import urlparse,urljoin,quote
from PIL import Image,ImageDraw

def placeholder(name):
    image=Image.new('RGBA',(24,24),'#334155')
    ImageDraw.Draw(image).text((8,5),(name or '?')[0].upper(),fill='white')
    return image

def public_url(url):
    parsed=urlparse(url)
    host=parsed.hostname
    if parsed.scheme not in ('http','https') or not host or '.' not in host or parsed.username or parsed.password or parsed.port not in (None,80,443):
        raise ValueError('No public company website')
    addresses=socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Non-public icon address')
    return url


def download(url,limit=1024*1024):
    """Bounded public image/HTML read, including ordinary company redirects."""
    import requests
    for _ in range(5):
        public_url(url)
        with requests.get(url,headers={'User-Agent':'Mozilla/5.0 StockMonitor/0.16'},
                          timeout=(3,5),stream=True,allow_redirects=False) as response:
            if response.is_redirect:
                url=urljoin(url,response.headers['Location']);continue
            response.raise_for_status();chunks=[];size=0
            for chunk in response.iter_content(16384):
                size+=len(chunk)
                if size>limit:raise ValueError('Icon response too large')
                chunks.append(chunk)
            return b''.join(chunks),url
    raise ValueError('Too many icon redirects')


def icon_image(data):
    with Image.open(io.BytesIO(data)) as image:
        if image.width*image.height>4_000_000:raise ValueError('Icon dimensions too large')
        if hasattr(image,'ico'):
            image=image.ico.getimage(max(image.ico.sizes(),key=lambda s:s[0]*s[1]))
        result=image.convert('RGBA');result.thumbnail((24,24),Image.Resampling.LANCZOS)
        canvas=Image.new('RGBA',(24,24));canvas.paste(result,((24-result.width)//2,(24-result.height)//2),result)
        return canvas


def site_icons(html,base):
    from html.parser import HTMLParser
    class Links(HTMLParser):
        def __init__(self):super().__init__();self.icons=[]
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs);rel=attrs.get('rel','').lower()
            if tag=='link' and 'icon' in rel and attrs.get('href'):
                url=urljoin(base,attrs['href'])
                if not urlparse(url).path.lower().endswith('.svg'):self.icons.append(url)
    parser=Links();parser.feed(html)
    return list(dict.fromkeys(parser.icons))[:3]


def fetch(symbol,directory,website=None,isin=None):
    import requests,yfinance as yf
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    yf.set_tz_cache_location(str(directory.parent/'provider_cache'))
    key=hashlib.sha256(symbol.encode()).hexdigest();path=directory/(key+'.png');failed=directory/(key+'.missing-v3.json');meta=directory/(key+'.json')
    cached=None
    if path.exists() and time.time()-path.stat().st_mtime<30*86400:
        try:
            with Image.open(path) as image:
                if image.width*image.height>4_000_000:raise ValueError('Cached icon dimensions too large')
                cached=image.convert('RGBA').copy()
                return cached
        except (OSError,ValueError):
            # Corrupt cache is not a UI failure; keep file and try the provider.
            pass
    # A failed refresh must never remove a previously usable icon.
    if path.exists():
        try:cached=icon_image(path.read_bytes())
        except (OSError,ValueError):pass
    if failed.exists() and time.time()-failed.stat().st_mtime<3600:return cached
    try:
        if not website and meta.exists():website=json.loads(meta.read_text(encoding='utf-8')).get('website')
        if not website:
            # Yahoo uses a hyphen for US class-share profile symbols.
            lookup=symbol.replace('.','-') if symbol.endswith(('.A','.B','.C')) else symbol
            website=yf.Ticker(lookup).get_info().get('website','')
        if website and not urlparse(website).scheme:website='https://'+website
        public_url(website or '')
        host=urlparse(website).hostname
        candidates=[urljoin(website,'/favicon.ico')]
        # The public cached favicon is artwork only, never a market-data source.
        candidates.append('https://icons.duckduckgo.com/ip3/'+quote(host,safe='')+'.ico')
        errors=[]
        for stage in range(2):
            for url in candidates:
                try:
                    data,actual=download(url);canvas=icon_image(data)
                    tmp=path.with_suffix('.tmp');canvas.save(tmp,format='PNG');tmp.replace(path)
                    meta.write_text(json.dumps(dict(website=website,url=actual,checked_at=time.time()),ensure_ascii=False),encoding='utf-8')
                    return canvas
                except Exception as error:errors.append(type(error).__name__)
            if stage==0:
                try:
                    html,base=download(website,limit=2*1024*1024)
                    candidates=site_icons(html.decode('utf-8',errors='replace'),base)
                except Exception as error:errors.append(type(error).__name__);break
        raise ValueError('No decodable company icon: '+','.join(errors))
    except Exception as error:
        # Some secondary listings have no company website in Yahoo at all.
        # Resolve artwork by exact ISIN, never by a guessed lookalike ticker.
        from market import valid_isin
        if valid_isin(isin):
            try:
                url='https://api.elbstream.com/logos/isin/'+quote(isin,safe='')+'?format=png&size=64'
                data,actual=download(url);canvas=icon_image(data)
                tmp=path.with_suffix('.tmp');canvas.save(tmp,format='PNG');tmp.replace(path)
                meta.write_text(json.dumps(dict(website=website,url=actual,isin=isin,
                    attribution='Logos provided by Elbstream',checked_at=time.time()),ensure_ascii=False),encoding='utf-8')
                return canvas
            except Exception as fallback:error=ValueError(str(error)+'; artwork: '+type(fallback).__name__)
        failed.write_text(json.dumps(dict(website=website,error=str(error)[:300],checked_at=time.time()),ensure_ascii=False),encoding='utf-8')
        return cached

class LogoMixin:
    def attach_logo(self,tree,item,info):
        from PIL import ImageTk
        from paths import DATA_DIR
        symbol=info.get('profile_symbol') or info.get('history_symbol') or info.get('symbol') or ''
        if not hasattr(self,'logo_images'):self.logo_images={};self.logo_waiters={};self.logo_retry={}
        if symbol not in self.logo_images:
            self.logo_images[symbol]=ImageTk.PhotoImage(placeholder(info.get('name') or symbol),master=self.root)
        if symbol not in self.logo_waiters and time.time()>=self.logo_retry.get(symbol,0):
            self.logo_waiters[symbol]=[]
            card=self.store.setting('card:'+info.get('isin',''),{})
            website=info.get('website') or (card.get('overview',{}).get('website') if card.get('symbol')==symbol else None)
            def done(image):
                self.logo_retry[symbol]=time.time()+(30*86400 if image is not None else 3600)
                if image is not None:self.logo_images[symbol]=ImageTk.PhotoImage(image,master=self.root)
                for target,iid in self.logo_waiters.pop(symbol,[]):
                    if target.winfo_exists() and target.exists(iid):target.item(iid,image=self.logo_images[symbol])
            if self.job(lambda:fetch(symbol,DATA_DIR/'logos',website,info.get('isin')),done,lambda _:done(None),lane='misc',key='logo:'+symbol) is False:
                self.logo_waiters.pop(symbol,None);self.logo_retry[symbol]=time.time()+30
        tree.item(item,image=self.logo_images[symbol])
        if symbol in self.logo_waiters:self.logo_waiters[symbol].append((tree,item))

    def show_watchlist(self):
        import tkinter as tk
        from tkinter import ttk
        win=tk.Toplevel(self.root);win.title(tr('Акции'));win.geometry('900x500')
        credit=ttk.Label(win,text='Логотипы: сайты компаний · Elbstream',cursor='hand2',style='Muted.TLabel')
        credit.pack(side='bottom',pady=4)
        credit.bind('<Button-1>',lambda _:__import__('webbrowser').open('https://elbstream.com'))
        ttk.Label(win,text=tr('Сверху: рабочий деморежим → доступный анализ → график → ожидающие и заблокированные.\nWAIT — ожидание сигнала; исполнение отдельно проверяет свежую цену и все ограничения.'),wraplength=880).pack(anchor='w',padx=8,pady=6)
        tree=ttk.Treeview(win,columns=('status',),show='tree headings',selectmode='browse')
        tree.heading('#0',text=tr('Акция'));tree.column('#0',width=280)
        tree.heading('status',text=tr('Состояние'));tree.column('status',width=580)
        scrollbar=ttk.Scrollbar(win,orient='vertical',command=tree.yview);scrollbar.pack(side='right',fill='y')
        tree.configure(yscrollcommand=scrollbar.set);tree.pack(fill='both',expand=True)
        def render():
            if not win.winfo_exists():return
            infos=self.stock_order.ordered() if hasattr(self,'stock_order') else self.watchlist
            for index,info in enumerate(infos):
                label=getattr(getattr(self,'stock_order',None),'rows',{}).get(info['isin'],{}).get('label','Ожидается оценка')
                if not tree.exists(info['isin']):
                    item=tree.insert('','end',iid=info['isin'],text=info['name']+' • '+info.get('symbol',''))
                    self.attach_logo(tree,item,info)
                tree.item(info['isin'],values=(label,));tree.move(info['isin'],'',index)
        render()
        if hasattr(self,'stock_order'):
            self.stock_order.refresh(render)
            ttk.Button(win,text=tr('Обновить порядок'),command=lambda:self.stock_order.refresh(render)).pack(side='right')
        def select(event=None):
            if tree.selection():
                self.symbol.set(next(i['name'] for i in self.watchlist if i['isin']==tree.selection()[0]));win.destroy();self.refresh_chart()
        tree.bind('<Double-1>',select);tree.bind('<Return>',select)
        ttk.Button(win,text=tr('Выбрать'),command=select).pack()
