"""Search/add presentation only. Existing identity resolution owns admission."""
import tkinter as tk
from tkinter import ttk

from i18n import tr
from indicator_dialog import styles
from themes import THEMES


class Dialog:
    def __init__(self,app):
        from main_screen import native_titlebar
        self.win=win=tk.Toplevel(app.root);win.title(tr('Поиск и добавление акции'))
        win.geometry(f'{min(1100,win.winfo_screenwidth()-80)}x{min(730,win.winfo_screenheight()-100)}')
        win.minsize(900,600)
        palette=THEMES.get(app.store.setting('theme','Тёмная'),THEMES['Тёмная'])
        p=self.prefix=styles(app.root,palette)
        style=ttk.Style(app.root)
        style.configure('Instrument.TCombobox',fieldbackground=palette['bg'],background=palette['panel'],foreground=palette['text'],arrowcolor=palette['text'],bordercolor=palette['grid'],lightcolor=palette['panel'],darkcolor=palette['panel'],padding=4)
        style.configure('Instrument.Treeview',rowheight=34,font=('Segoe UI',10),background=palette['bg'],fieldbackground=palette['bg'],foreground=palette['text'],bordercolor=palette['bg'],lightcolor=palette['bg'],darkcolor=palette['bg'])
        style.configure('Instrument.Treeview.Heading',font=('Segoe UI',10,'bold'),background=palette['panel'],foreground=palette['text'],bordercolor=palette['panel'],lightcolor=palette['panel'],darkcolor=palette['panel'],relief='flat')
        style.map('Instrument.Treeview',background=[('selected','#11355b')],foreground=[('selected','#ffffff')])
        self.body=body=ttk.Frame(win,padding=16,style=p+'.TFrame');body.pack(fill='both',expand=True)
        heading=ttk.Frame(body,style=p+'.TFrame');heading.pack(fill='x',pady=(0,12))
        ttk.Label(heading,text='⊕',foreground='#00c5ff',style=p+'.TLabel',font=('Segoe UI',21)).pack(side='left',padx=(0,9))
        ttk.Label(heading,text=tr('Поиск и добавление акции'),style=p+'.TLabel',font=('Segoe UI',17,'bold')).pack(side='left')
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
                widget=ttk.Combobox(form,textvariable=variable,values=values,state='normal',width=18,font=('Segoe UI',11),style='Instrument.TCombobox')
            else:widget=ttk.Entry(form,textvariable=variable,style=p+'.TEntry',font=('Segoe UI',11),width=26)
            widget.grid(row=row,column=column+1,sticky='ew',pady=3,padx=(0,22) if column==0 else 0)
        table=ttk.Frame(body,style=p+'.TFrame');table.pack(fill='both',expand=True)
        self.results=tree=ttk.Treeview(table,columns=('symbol','isin','venue','currency','kind'),show='tree headings',selectmode='browse',height=7,style='Instrument.Treeview')
        for key,label,width in [('#0','Название / Компания',310),('symbol','Тикер',110),('isin','ISIN',170),('venue','Биржа',95),('currency','Валюта',75),('kind','Тип',90)]:
            tree.heading(key,text=tr(label));tree.column(key,width=width,minwidth=60,stretch=key=='#0')
        scroll=ttk.Scrollbar(table,command=tree.yview,style=p+'.Vertical.TScrollbar');scroll.pack(side='right',fill='y')
        tree.configure(yscrollcommand=scroll.set);tree.pack(fill='both',expand=True)
        win.bind('<Escape>',lambda _:win.destroy())
        win.after_idle(lambda:native_titlebar(win,palette))
        win.bind('<Map>',lambda event:win.after_idle(lambda:native_titlebar(win,palette)) if event.widget is win else None,add='+')

    def insert(self,index,item):
        return self.results.insert('','end',iid=str(index),text=item.get('longname') or item.get('shortname') or item['symbol'],
            values=(item['symbol'],item.get('isin') or '—',item.get('exchDisp') or item.get('exchange') or '—',
                    item.get('currency') or '—',item.get('typeDisp') or item.get('quoteType') or '—'))

    def confirmed(self,iid,details):
        if self.results.exists(iid):
            values=list(self.results.item(iid,'values'))
            values[:4]=[details['symbol'],details['isin'],details['venue'],details['currency']]
            self.results.item(iid,values=values)

    def wire(self,find,add,clear):
        self.find_button.configure(command=find);self.add_button.configure(command=add)
        self.clear_button.configure(command=clear);self.clear_search.configure(command=clear)
        self.search_entry.bind('<Return>',lambda _:find())
        return self.win
