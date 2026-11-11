from i18n import tr
"""Small company site icons; network and decoding stay off the UI thread."""
import hashlib,io,time
from pathlib import Path
from urllib.parse import urlparse
from PIL import Image,ImageDraw

def placeholder(name):
    image=Image.new('RGBA',(24,24),'#334155')
    ImageDraw.Draw(image).text((8,5),(name or '?')[0].upper(),fill='white')
    return image

def fetch(symbol,directory):
    import requests,yfinance as yf
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    yf.set_tz_cache_location(str(directory.parent/'provider_cache'))
    key=hashlib.sha256(symbol.encode()).hexdigest();path=directory/(key+'.png');failed=directory/(key+'.missing')
    if path.exists() and time.time()-path.stat().st_mtime<30*86400:
        try:
            with Image.open(path) as image:
                if image.width*image.height>4_000_000:raise ValueError('Cached icon dimensions too large')
                return image.convert('RGBA').copy()
        except (OSError,ValueError):
            # Corrupt cache is not a UI failure; keep file and try the provider.
            pass
    if failed.exists() and time.time()-failed.stat().st_mtime<86400:return None
    try:
        website=yf.Ticker(symbol).get_info().get('website','')
        host=urlparse(website).hostname
        if not host or '.' not in host or ':' in host or host.replace('.','').isdigit():raise ValueError('No public company website')
        response=requests.get('https://'+host+'/favicon.ico',timeout=(4,6),stream=True,allow_redirects=False)
        response.raise_for_status();chunks=[];size=0
        for chunk in response.iter_content(16384):
            size+=len(chunk)
            if size>1024*1024:raise ValueError('Icon too large')
            chunks.append(chunk)
        with Image.open(io.BytesIO(b''.join(chunks))) as image:
            if image.width*image.height>4_000_000:raise ValueError('Icon dimensions too large')
            result=image.convert('RGBA');result.thumbnail((24,24))
            canvas=Image.new('RGBA',(24,24));canvas.paste(result,((24-result.width)//2,(24-result.height)//2),result)
            canvas.save(path)
        return canvas
    except Exception:
        failed.touch();return None

class LogoMixin:
    def attach_logo(self,tree,item,info):
        from PIL import ImageTk
        from paths import DATA_DIR
        symbol=info.get('profile_symbol') or info.get('history_symbol') or info.get('symbol') or ''
        if not hasattr(self,'logo_images'):self.logo_images={};self.logo_waiters={}
        if symbol not in self.logo_images:
            self.logo_images[symbol]=ImageTk.PhotoImage(placeholder(info.get('name') or symbol),master=self.root)
            self.logo_waiters[symbol]=[]
            def done(image):
                if image is not None:self.logo_images[symbol]=ImageTk.PhotoImage(image,master=self.root)
                for target,iid in self.logo_waiters.pop(symbol,[]):
                    if target.winfo_exists() and target.exists(iid):target.item(iid,image=self.logo_images[symbol])
            self.job(lambda:fetch(symbol,DATA_DIR/'logos'),done,lambda _:done(None))
        tree.item(item,image=self.logo_images[symbol])
        if symbol in self.logo_waiters:self.logo_waiters[symbol].append((tree,item))

    def show_watchlist(self):
        import tkinter as tk
        from tkinter import ttk
        win=tk.Toplevel(self.root);win.title(tr('Акции'));win.geometry('900x500')
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
