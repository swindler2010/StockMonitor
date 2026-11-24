"""Search/add presentation only. Existing identity resolution owns admission."""
import tkinter as tk
from tkinter import ttk

from i18n import tr
from indicator_dialog import styles
from themes import THEMES
import hashlib


def clean_favorite(value):
    if not isinstance(value,dict) or not isinstance(value.get('symbol'),str) or not value['symbol'].strip():return None
    return {k:str(value[k])[:250] for k in ('symbol','longname','shortname','isin','exchDisp','exchange','currency','quoteType') if value.get(k) is not None}


def favorite_id(item):return hashlib.sha256(item['symbol'].strip().casefold().encode()).hexdigest()


class Dialog:
    def __init__(self,app):
        from main_screen import native_titlebar
        self.app=app;self.items={};self.original_photos={};self.decorated={};self.sort_column=None;self.descending=False;self.favorites_first=False
        self.win=win=tk.Toplevel(app.root);win.title(tr('Поиск и добавление акции'))
        from design_layout import fit_window
        fit_window(win,1100,730,(900,600))
        palette=THEMES.get(app.store.setting('theme','Тёмная'),THEMES['Тёмная'])
        p=self.prefix=styles(app.root,palette)
        style=ttk.Style(app.root)
        style.configure('Instrument.TCombobox',fieldbackground=palette['bg'],background=palette['panel'],foreground=palette['text'],arrowcolor=palette['text'],bordercolor=palette['grid'],lightcolor=palette['panel'],darkcolor=palette['panel'],padding=4)
        style.configure('Instrument.Treeview',rowheight=34,font=('Segoe UI',10),background=palette['bg'],fieldbackground=palette['bg'],foreground=palette['text'],bordercolor=palette['bg'],lightcolor=palette['bg'],darkcolor=palette['bg'])
        style.configure('Instrument.Treeview.Heading',font=('Segoe UI',10,'bold'),background=palette['panel'],foreground=palette['text'],bordercolor=palette['panel'],lightcolor=palette['panel'],darkcolor=palette['panel'],relief='flat')
        style.map('Instrument.Treeview.Heading',background=[('pressed','#123955'),('active','#12304a')],foreground=[('pressed','#ffffff'),('active','#ffffff')])
        style.map('Instrument.Treeview',background=[('selected','#11355b')],foreground=[('selected','#ffffff')])
        self.body=body=ttk.Frame(win,padding=16,style=p+'.TFrame');body.pack(fill='both',expand=True)
        heading=ttk.Frame(body,style=p+'.TFrame');heading.pack(fill='x',pady=(0,12))
        ttk.Label(heading,image=__import__('ui_icons').photo(app.root,'add',30),style=p+'.TLabel').pack(side='left',padx=(0,9))
        ttk.Label(heading,text=tr('Поиск и добавление акции'),style=p+'.TLabel',font=('Segoe UI',17,'bold')).pack(side='left')
        from ui_icons import photo
        self.favorite_note=tk.StringVar(win)
        ttk.Label(heading,textvariable=self.favorite_note,style=p+'.TLabel').pack(side='right',padx=8)
        self.favorite_top=ttk.Button(heading,image=photo(app.root,'star_filled'),command=self.favorite_order,width=3)
        self.favorite_top.pack(side='right')
        if hasattr(app,'tooltip'):app.tooltip(self.favorite_top,'Избранное сверху')
        ttk.Label(body,text=tr('Поиск по названию, тикеру или ISIN. Для GETTEX нужен ISIN; EUR proxy указывается отдельно.'),
                  style=p+'.TLabel',font=('Segoe UI',10),wraplength=980).pack(anchor='w',pady=(0,12))
        search=ttk.Frame(body,style=p+'.TFrame');search.pack(fill='x',pady=(0,12))
        self.query=tk.StringVar(win)
        self.search_entry=ttk.Entry(search,textvariable=self.query,style=p+'.TEntry',font=('Segoe UI',12))
        self.search_entry.pack(side='left',fill='x',expand=True,padx=(0,5))
        self.clear_search=ttk.Button(search,text='✕',width=3);self.clear_search.pack(side='left',padx=3)
        self.find_button=ttk.Button(search,text=tr('Найти'),style=p+'.Secondary.TButton');self.find_button.pack(side='left')
        footer=ttk.Frame(body,style=p+'.TFrame');footer.pack(side='bottom',fill='x',pady=(10,0))
        self.clear_button=ttk.Button(footer,text=tr('Очистить'),style=p+'.Secondary.TButton');self.clear_button.pack(side='left')
        self.add_button=ttk.Button(footer,text=tr('＋ Добавить'),state='disabled',style=p+'.Primary.TButton');self.add_button.pack(side='right')
        ttk.Button(footer,text=tr('Отмена'),command=win.destroy,style=p+'.Secondary.TButton').pack(side='right',padx=8)
        self.state=tk.StringVar(win)
        ttk.Label(body,textvariable=self.state,style=p+'.TLabel',font=('Segoe UI',10),wraplength=980).pack(side='bottom',fill='x',pady=(8,0))
        form=ttk.Frame(body,style=p+'.TFrame');form.pack(side='bottom',fill='x',pady=(12,0))
        form.columnconfigure(1,weight=3);form.columnconfigure(3,weight=2)
        self.fields={}
        specs=[('name','Название',0,0),('isin','ISIN',1,0),('symbol','Тикер / обозначение',2,0),('venue','Биржа',3,0),
               ('currency','Валюта',0,2),('history_symbol','Тикер истории',1,2),('profile_symbol','Тикер карточки',2,2)]
        for key,label,row,column in specs:
            ttk.Label(form,text=tr(label)+':',style=p+'.TLabel',font=('Segoe UI',10)).grid(row=row,column=column,sticky='w',padx=(0,12),pady=3)
            variable=self.fields[key]=tk.StringVar(win)
            if key in ('venue','currency'):
                values=('GETTEX','XETRA','FRA','NASDAQ','NYSE') if key=='venue' else ('EUR','USD','GBP','CHF')
                widget=ttk.Combobox(form,textvariable=variable,values=values,state='normal',width=18,font=('Segoe UI',11),style=p+'.TCombobox')
            else:widget=ttk.Entry(form,textvariable=variable,style=p+'.TEntry',font=('Segoe UI',11),width=26)
            widget.grid(row=row,column=column+1,sticky='ew',pady=3,padx=(0,22) if column==0 else 0)
        table=ttk.Frame(body,style=p+'.TFrame');table.pack(fill='both',expand=True)
        self.results=tree=ttk.Treeview(table,columns=('symbol','isin','venue','currency','kind'),show='tree headings',selectmode='browse',height=7,style='Instrument.Treeview')
        for key,label,width in [('#0','Название / Компания',310),('symbol','Тикер',110),('isin','ISIN',170),('venue','Биржа',95),('currency','Валюта',75),('kind','Тип',90)]:
            tree.heading(key,text=tr(label),command=lambda column=key:self.sort(column));tree.column(key,width=width,minwidth=60,stretch=key=='#0')
        scroll=ttk.Scrollbar(table,command=tree.yview,style=p+'.Vertical.TScrollbar');scroll.pack(side='right',fill='y')
        tree.configure(yscrollcommand=scroll.set);tree.pack(fill='both',expand=True)
        tree._decorate_logo=self.decorate_logo;tree._logo_bindings={}
        tree.bind('<Button-1>',self.click_star,add='+')
        from ui_collections import shared
        self.favorites=shared(app,'ui_search_collection_v1',clean_favorite)
        self.unsubscribe=self.favorites.subscribe(self.paint_favorites);self.favorites.load()
        win.bind('<Destroy>',lambda event:self.unsubscribe() if event.widget is win else None,add='+')
        win.bind('<Escape>',lambda _:win.destroy())
        win.after_idle(lambda:native_titlebar(win,palette))
        win.bind('<Map>',lambda event:win.after_idle(lambda:native_titlebar(win,palette)) if event.widget is win else None,add='+')

    def insert(self,index,item):
        if index==0:
            self.items.clear();self.original_photos.clear();self.decorated.clear();self.results._logo_bindings.clear()
        self.items[str(index)]=dict(item)
        from ui_icons import photo
        return self.results.insert('','end',iid=str(index),text=item.get('longname') or item.get('shortname') or item['symbol'],image=photo(self.app.root,'star_filled' if self.favorites.has(favorite_id(item)) else 'star'),
            values=(item['symbol'],item.get('isin') or '—',item.get('exchDisp') or item.get('exchange') or '—',
                    item.get('currency') or '—',item.get('typeDisp') or item.get('quoteType') or '—'))

    def confirmed(self,iid,details):
        if self.results.exists(iid):
            values=list(self.results.item(iid,'values'))
            values[:4]=[details['symbol'],details['isin'],details['venue'],details['currency']]
            self.results.item(iid,values=values)
            if self.sort_column:self.sort(self.sort_column,False)

    def decorate_logo(self,iid,image):
        from PIL import Image,ImageTk
        from ui_icons import photo
        if iid not in self.items:return image
        self.original_photos[iid]=image
        starred=self.favorites.has(favorite_id(self.items[iid]));key=(iid,str(image),starred)
        if key not in self.decorated:
            canvas=Image.new('RGBA',(54,24))
            canvas.paste(ImageTk.getimage(photo(self.app.root,'star_filled' if starred else 'star',18)),(0,3))
            canvas.paste(ImageTk.getimage(image),(27,0))
            self.decorated[key]=ImageTk.PhotoImage(canvas,master=self.app.root)
        return self.decorated[key]

    def paint_favorites(self):
        from ui_icons import photo
        if not self.win.winfo_exists():return
        self.favorite_note.set(tr('Сохранение…') if self.favorites.busy else tr('Не удалось сохранить отметку') if self.favorites.error else '')
        self.decorated.clear()
        for iid in self.results.get_children():
            if iid not in self.items:continue
            image=self.original_photos.get(iid)
            image=self.decorate_logo(iid,image) if image is not None else photo(self.app.root,'star_filled' if self.favorites.has(favorite_id(self.items[iid])) else 'star')
            self.results.item(iid,image=image)
        if self.favorites_first:self.sort(self.sort_column or '#0',False)

    def click_star(self,event):
        iid=self.results.identify_row(event.y)
        if iid and iid in self.items and self.results.identify_column(event.x)=='#0':
            bbox=self.results.bbox(iid,'#0')
            if bbox and event.x-bbox[0]<41:
                self.favorites.toggle(favorite_id(self.items[iid]),self.items[iid]);return 'break'

    def favorite_order(self):
        self.favorites_first=not self.favorites_first;self.sort(self.sort_column or '#0',False)

    def sort(self,column,toggle=True):
        self.descending=not self.descending if toggle and column==self.sort_column else self.descending if not toggle else False
        self.sort_column=column
        def value(iid):return str(self.results.item(iid,'text') if column=='#0' else self.results.set(iid,column)).casefold()
        rows=list(self.results.get_children());known=[iid for iid in rows if value(iid) not in ('','—')];missing=[iid for iid in rows if iid not in known]
        rows=sorted(known,key=value,reverse=self.descending)+missing
        if self.favorites_first:rows.sort(key=lambda iid:not self.favorites.has(favorite_id(self.items[iid])))
        for n,iid in enumerate(rows):self.results.move(iid,'',n)
        labels={'#0':'Название / Компания','symbol':'Тикер','isin':'ISIN','venue':'Биржа','currency':'Валюта','kind':'Тип'}
        for key,label in labels.items():self.results.heading(key,text=tr(label)+('  ▾' if self.descending else '  ▴') if key==column else tr(label))

    def wire(self,find,add,clear):
        self.find_button.configure(command=find);self.add_button.configure(command=add)
        self.clear_button.configure(command=clear);self.clear_search.configure(command=clear)
        self.search_entry.bind('<Return>',lambda _:find())
        return self.win
