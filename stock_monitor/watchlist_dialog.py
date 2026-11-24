"""Stock list presentation using the existing read-only stock_order runtime."""
import tkinter as tk
from tkinter import ttk
from datetime import datetime

from i18n import tr
from localized_widgets import CanonicalCombobox
from timeframes import LABELS
from ui_icons import photo
from indicator_dialog import styles
from themes import THEMES


def matches(info,row,query='',state='Все',tf='Все',venue='Все'):
    if query.casefold() not in ' '.join(str(info.get(k,'')) for k in ('name','symbol','isin')).casefold():return False
    tier=row.get('tier',6)
    if state=='Готовы' and tier not in (1,2):return False
    if state=='Ожидают' and tier not in (3,4,6):return False
    if state=='WAIT' and 'WAIT' not in row.get('label',''):return False
    if state=='Блокировки' and tier!=5:return False
    if tf!='Все' and tf not in row.get('timeframes',[]):return False
    return venue=='Все' or venue in row.get('venues',[])


def counts(infos,rows):
    values=[rows.get(i['isin'],{}) for i in infos if i.get('kind')!='benchmark']
    return (len(values),sum(r.get('tier',6) in (1,2) for r in values),
            sum(r.get('tier',6) in (3,4,6) for r in values),
            sum('WAIT' in r.get('label','') for r in values),sum(r.get('tier')==5 for r in values))


def timestamp(value):
    if not value:return '—'
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).astimezone().strftime('%d.%m.%Y %H:%M')
    except (ValueError,TypeError):return str(value)[:24]


class Dialog:
    def __init__(self,app):
        from main_screen import native_titlebar
        self.app=app;self.closed=False
        self.win=win=tk.Toplevel(app.root,background=ttk.Style(app.root).lookup('Desk.TFrame','background') or '#0b1220');win.title(tr('Акции • Stock Monitor'))
        from design_layout import fit_window
        fit_window(win,1540,840,(1000,600))
        self.palette=palette=THEMES.get(app.store.setting('theme','Тёмная'),THEMES['Тёмная'])
        self.p=p=styles(app.root,palette)
        style=ttk.Style(app.root)
        style.configure('Watchlist.Treeview',font=('Segoe UI',10),rowheight=37,background=palette['bg'],fieldbackground=palette['bg'],foreground=palette['text'],borderwidth=0,bordercolor=palette['bg'],lightcolor=palette['bg'],darkcolor=palette['bg'])
        style.configure('Watchlist.Treeview.Heading',font=('Segoe UI',10,'bold'),background=palette['panel'],foreground=palette['text'],padding=(5,8),bordercolor=palette['grid'],lightcolor=palette['panel'],darkcolor=palette['panel'],borderwidth=0,relief='flat')
        style.map('Watchlist.Treeview',background=[('selected','#11355b')],foreground=[('selected','#ffffff')])
        style.layout('Watchlist.TButton',style.layout(p+'.Secondary.TButton'))
        style.configure('Watchlist.TButton',font=('Segoe UI',10),padding=(10,6))
        style.configure('Watchlist.Horizontal.TScrollbar',background=palette['grid'],troughcolor=palette['bg'],bordercolor=palette['bg'],lightcolor=palette['bg'],darkcolor=palette['bg'],arrowcolor=palette['muted'],arrowsize=10)
        win.iconphoto(False,photo(app.root,'chart',32))
        self.query=tk.StringVar(win);self.state=tk.StringVar(win,'Все');self.tf=tk.StringVar(win,'Все')
        self.venue=tk.StringVar(win,'Все');self.sort=tk.StringVar(win,'По готовности')
        self.values={};self.stats=[]
        base=ttk.Frame(win,padding=10,style=p+'.TFrame');base.pack(fill='both',expand=True)
        nav=ttk.Frame(base,style=p+'.TFrame');nav.pack(fill='x',pady=(0,10))
        for label,command in [('График',self.select),('Акции',lambda:None),('Индикаторы',app.show_indicators),
                              ('CandlePilot',lambda:__import__('candlepilot_health').show(app)),('Новости',app.show_news),
                              ('Портфель',app.show_portfolio),('Настройки',app.show_settings)]:
            ttk.Button(nav,text=tr(label),image=photo(app.root,{'График':'chart','Акции':'template','Индикаторы':'indicators','CandlePilot':'globe','Новости':'news','Портфель':'template','Настройки':'gear'}[label],18),compound='left',command=command,style='DeskActive.TButton' if label=='Акции' else 'Watchlist.TButton').pack(side='left',padx=3)
        header=ttk.Frame(base,style=p+'.TFrame');header.pack(fill='x',pady=(0,12))
        ttk.Label(header,image=photo(app.root,'chart',40),style=p+'.TLabel').pack(side='left',padx=8)
        title=ttk.Frame(header,style=p+'.TFrame');title.pack(side='left')
        ttk.Label(title,text=tr('Акции'),style=p+'.TLabel',font=('Segoe UI',20,'bold')).pack(anchor='w')
        ttk.Label(title,text=tr('Инструменты и состояние анализа'),style=p+'.TLabel',font=('Segoe UI',10)).pack(anchor='w')
        summary=ttk.Frame(header,style=p+'.TFrame');summary.pack(side='right')
        summary_cards=[]
        for label,color in [('Всего акций',palette['text']),('Готовы','#2be870'),('Ожидают','#d9b13b'),('WAIT','#f3a622'),('Блокировки','#ff5966')]:
            card=ttk.Frame(summary,padding=(9,5),width=140,height=68,style='DeskCard.TFrame');card.pack(side='left',padx=4);card.pack_propagate(False)
            summary_cards.append(card)
            artwork={'Всего акций':'template','Готовы':'status_ready','Ожидают':'status_wait','WAIT':'status_wait','Блокировки':'status_error'}[label]
            ttk.Label(card,image=photo(app.root,artwork,25,color=color),style='Desk.TLabel').pack(side='left',padx=(0,7))
            words=ttk.Frame(card,style='Desk.TFrame');words.pack(side='left',fill='both',expand=True)
            ttk.Label(words,text=tr(label),style='DeskSmall.TLabel').pack(anchor='w')
            v=tk.StringVar(win,'—');self.stats.append(v)
            ttk.Label(words,textvariable=v,foreground=color,style='Desk.TLabel',font=('Segoe UI',17,'bold')).pack(anchor='w')
        header.bind('<Configure>',lambda event:[card.configure(width=max(76,min(140,(event.width-300)//5-8))) for card in summary_cards],add='+')
        filters=ttk.Frame(base,style=p+'.TFrame');filters.pack(fill='x',pady=(0,10))
        search=ttk.Entry(filters,textvariable=self.query,width=28,style=p+'.TEntry',font=('Segoe UI',10))
        search.pack(side='left',fill='x',expand=True,padx=(0,6))
        placeholder=ttk.Label(search,text=tr('Поиск по названию, тикеру или ISIN…'),style='DeskMuted.TLabel',font=('Segoe UI',9),cursor='xterm')
        def hint(*_):
            if self.query.get() or win.focus_get()==search:placeholder.place_forget()
            else:placeholder.place(x=7,rely=.5,anchor='w')
        placeholder.bind('<Button-1>',lambda _:(search.focus_set(),placeholder.place_forget()))
        search.bind('<FocusIn>',hint,add='+');search.bind('<FocusOut>',hint,add='+');self.query.trace_add('write',hint);hint()
        app.tooltip(search,'Поиск по названию, тикеру или ISIN')
        for variable,options,width in [(self.state,['Все','Готовы','Ожидают','WAIT','Блокировки'],12),
                                       (self.tf,['Все',*LABELS],5),
                                       (self.venue,['Все'],12),
                                       (self.sort,['По готовности','По названию','По тикеру'],15)]:
            label='Фильтр' if variable is self.state else 'ТФ' if variable is self.tf else 'Площадка' if variable is self.venue else 'Порядок'
            ttk.Label(filters,text=tr(label)+':',style='DeskMuted.TLabel',font=('Segoe UI',9)).pack(side='left',padx=(5,0))
            box=CanonicalCombobox(filters,textvariable=variable,values=options,state='readonly',style=p+'.TCombobox',width=width)
            box.pack(side='left',padx=3)
            if variable is self.venue:self.venue_box=box
            variable.trace_add('write',lambda *_:self.render())
        ttk.Button(filters,text=tr('↻ Обновить'),command=self.refresh).pack(side='right',padx=3)
        self.query.trace_add('write',lambda *_:self.render())
        footer=ttk.Frame(base,style=p+'.TFrame');footer.pack(side='bottom',fill='x',pady=(10,0))
        ttk.Button(footer,text=tr('✓ Выбрать'),command=self.select,style='DeskActive.TButton').pack(side='left',padx=3)
        ttk.Button(footer,text=tr('Обновить порядок'),command=self.refresh).pack(side='left',padx=3)
        ttk.Button(footer,text=tr('＋ Добавить'),command=app.add_stock_dialog).pack(side='left',padx=3)
        ttk.Button(footer,text=tr('Перезагрузить график'),command=app.refresh_chart).pack(side='left',padx=3)
        credit=ttk.Label(footer,text=tr('Логотипы: сайты компаний · Elbstream'),style='DeskMuted.TLabel',cursor='hand2')
        credit.pack(side='right');credit.bind('<Button-1>',lambda _:__import__('webbrowser').open('https://elbstream.com'))
        self.status=tk.StringVar(win,tr('Показаны сохранённые результаты; это не новый торговый допуск.'))
        ttk.Label(base,textvariable=self.status,style='Muted.TLabel',wraplength=1300).pack(side='bottom',fill='x',pady=(6,0))
        workspace=ttk.Frame(base,style=p+'.TFrame');workspace.pack(fill='both',expand=True)
        detail=ttk.Frame(workspace,width=300,padding=14,style='DeskCard.TFrame');detail.pack(side='right',fill='y',padx=(10,0));detail.pack_propagate(False)
        self.logo=ttk.Label(detail,style='Desk.TLabel');self.logo.pack(anchor='w',pady=(2,8))
        self.name=tk.StringVar(win,'—');self.detail_label=tk.StringVar(win,'—')
        ttk.Label(detail,textvariable=self.name,style='DeskTitle.TLabel',wraplength=245).pack(anchor='w',pady=5)
        ttk.Label(detail,textvariable=self.detail_label,style='DeskMuted.TLabel',wraplength=245,justify='left').pack(anchor='w',pady=10)
        self.selected_status=ttk.Label(detail,style='Desk.TLabel',wraplength=245,font=('Segoe UI',11,'bold'))
        self.selected_status.pack(fill='x',pady=10)
        for label,command in [('Открыть график',self.select),('Обновить состояние',self.refresh)]:
            ttk.Button(detail,text=tr(label),command=command).pack(fill='x',side='bottom',pady=5)
        listing=ttk.Frame(workspace);listing.pack(fill='both',expand=True)
        self.tree=tree=ttk.Treeview(listing,columns=('favorite','symbol','source','tf','state','at'),show='tree headings',selectmode='browse',style='Watchlist.Treeview')
        for key,label,width in [('#0','#  Акция / Компания',270),('favorite','☆',25),('symbol','Тикер',85),('source','Площадка',90),('tf','Таймфреймы',100),('state','Состояние анализа',260),('at','Обновлено',140)]:
            tree.heading(key,text=tr(label),command=lambda k=key:self.sort_heading(k));tree.column(key,width=width,minwidth=22 if key=='favorite' else 65 if key!='#0' else 160,stretch=key in ('#0','state'))
        scroll=ttk.Scrollbar(listing,command=tree.yview,style=p+'.Vertical.TScrollbar');scroll.pack(side='right',fill='y')
        xscroll=ttk.Scrollbar(listing,orient='horizontal',command=tree.xview,style='Watchlist.Horizontal.TScrollbar');xscroll.pack(side='bottom',fill='x')
        tree.configure(yscrollcommand=scroll.set,xscrollcommand=xscroll.set);tree.pack(fill='both',expand=True)
        tree.tag_configure('ready',foreground='#2be870');tree.tag_configure('wait',foreground='#f2b54b');tree.tag_configure('blocked',foreground='#ff6976')
        tree.bind('<<TreeviewSelect>>',self.details);tree.bind('<Double-1>',self.select);tree.bind('<Return>',self.select)
        tree.bind('<Button-1>',self.favorite_click,add='+')
        tree.bind('<Button-3>',self.context_menu)
        win.bind('<Escape>',lambda _:self.close());win.protocol('WM_DELETE_WINDOW',self.close)
        win.after_idle(lambda:native_titlebar(win,palette))
        self.render();self.refresh()

    def rows(self):return getattr(getattr(self.app,'stock_order',None),'rows',{})

    def render(self):
        if self.closed or not hasattr(self,'tree'):return
        app=self.app;rows=self.rows()
        infos=list(app.stock_order.ordered() if hasattr(app,'stock_order') else app.watchlist)
        self.values={i['isin']:i for i in infos}
        for v,count in zip(self.stats,counts(infos,rows)):v.set(str(count))
        venues=sorted({s for r in rows.values() for s in r.get('venues',[])})
        if tuple(self.venue_box['values'])!=('Все',*venues):self.venue_box.configure(values=['Все',*venues])
        if self.sort.get()=='По названию':infos.sort(key=lambda i:i['name'].casefold())
        elif self.sort.get()=='По тикеру':infos.sort(key=lambda i:i.get('symbol','').casefold())
        known=set(self.values)
        for iid in getattr(self,'_known',set())|set(self.tree.get_children()):
            if iid not in known:self.tree.delete(iid)
        self._known=known
        for index,info in enumerate(infos):
            iid=info['isin'];row=rows.get(iid,{})
            cached=getattr(getattr(app,'main_screen',None),'logo_images',{}).get(iid)
            if not self.tree.exists(iid):
                self.tree.insert('','end',iid=iid,text=info['name'])
                if not cached:app.attach_logo(self.tree,iid,info)
            if cached:self.tree.item(iid,image=cached)
            tier=row.get('tier',6)
            favorites=getattr(getattr(app,'main_screen',None),'favorites',set())
            self.tree.item(iid,text=f'{index+1}  '+info['name'],values=('★' if iid in favorites else '☆',info.get('symbol','—'),', '.join(row.get('venues',[])) or '—',
                ', '.join(sorted(row.get('timeframes',[]),key=lambda tf:list(LABELS).index(tf) if tf in LABELS else 99)) or '—',__import__('design_layout').status_text(row),timestamp(row.get('assessment_checked_at'))),
                tags=('ready' if tier in (1,2) else 'blocked' if tier==5 else 'wait' if tier in (0,3,4) else '',))
            if matches(info,row,self.query.get(),self.state.get(),self.tf.get(),self.venue.get()):self.tree.move(iid,'','end')
            else:self.tree.detach(iid)
        if not self.tree.selection():
            selected=next((i['isin'] for i in infos if i['name']==app.symbol.get()),None)
            if selected and selected in self.tree.get_children():self.tree.selection_set(selected);self.tree.see(selected)
        self.details()

    def refresh(self):
        if self.closed:return
        if hasattr(self.app,'stock_order'):self.app.stock_order.refresh(self.render)
        else:self.render()

    def details(self,event=None):
        selected=self.tree.selection()
        if not selected or selected[0] not in self.values:
            self.name.set('—');self.detail_label.set('');self.selected_status.configure(text='');self.logo.configure(image='');return
        iid=selected[0];info=self.values[iid];row=self.rows().get(iid,{})
        self.name.set(info['name'])
        self.detail_label.set('\n\n'.join([str(info.get('symbol') or '—'),iid,
            tr('Валюта')+': '+str(info.get('currency') or '—'),tr('Площадка')+': '+(', '.join(row.get('venues',[])) or '—'),
            tr('Таймфреймы')+': '+(', '.join(row.get('timeframes',[])) or '—'),tr('Обновлено')+': '+timestamp(row.get('assessment_checked_at')),
            tr('Действительность оценки')+': '+str(row.get('current_validity') or '—')]))
        tier=row.get('tier',6);self.selected_status.configure(text=row.get('label',tr('Ожидается оценка')),
            foreground='#2be870' if tier in (1,2) else '#ff6976' if tier==5 else '#f2b54b')
        image=getattr(getattr(self.app,'main_screen',None),'header_images',{}).get(iid)
        self.logo.configure(image=image or self.tree.item(iid,'image'))

    def select(self,event=None):
        selected=self.tree.selection()
        if selected and selected[0] in self.values:
            self.app.symbol.set(self.values[selected[0]]['name']);self.close();self.app.refresh_chart()

    def sort_heading(self,key):
        if key in ('#0','symbol'):self.sort.set('По названию' if key=='#0' else 'По тикеру')
        elif key=='state':self.sort.set('По готовности')

    def context_menu(self,event):
        iid=self.tree.identify_row(event.y)
        if not iid or iid not in self.values:return
        self.tree.selection_set(iid);self.details()
        menu=tk.Menu(self.win,tearoff=False,background=self.palette['panel'],foreground=self.palette['text'])
        for label,command in [('Открыть график',self.select),('Индикаторы',self.app.show_indicators),('Обновить состояние',self.refresh),('Избранное',lambda:self.toggle_selected_favorite(iid))]:
            menu.add_command(label=tr(label),command=command)
        try:menu.tk_popup(event.x_root,event.y_root)
        finally:menu.grab_release()

    def toggle_selected_favorite(self,iid):
        screen=getattr(self.app,'main_screen',None)
        if screen is not None and iid in self.values:screen.toggle_favorite(iid);self.render()

    def favorite_click(self,event):
        if self.tree.identify_column(event.x)!='#1':return
        iid=self.tree.identify_row(event.y)
        screen=getattr(self.app,'main_screen',None)
        if iid and screen is not None:
            screen.toggle_favorite(iid);self.render()
            return 'break'

    def close(self):
        self.closed=True;self.win.destroy()


def show(app):
    old=getattr(app,'_watchlist_dialog',None)
    if old is not None and not old.closed and old.win.winfo_exists():old.win.lift();return old.win
    app._watchlist_dialog=Dialog(app)
    return app._watchlist_dialog.win
