"""Structured statement window; read-only auxiliary data, existing dispatcher."""
import tkinter as tk
from tkinter import ttk
from fundamentals_view import legacy_card, fetch_statements, cache_matches, freshness, merge_saved, overview_rows
from financial_format import format_value, saved_language
from i18n import tr
from localized_widgets import CanonicalCombobox


def show(app, info=None):
    info=dict(app.instrument_cfg() if info is None else info); isin=info['isin']
    windows=getattr(app,'_fundamental_windows',{})
    app._fundamental_windows=windows
    if isin in windows and windows[isin].winfo_exists():
        windows[isin].deiconify();windows[isin].lift();return
    win=tk.Toplevel(app.root);windows[isin]=win
    win.title(tr('Отчётность')+' • '+info['name']);win.geometry('1200x850')
    top=ttk.Frame(win,padding=10);top.pack(fill='x')
    frequency=tk.StringVar(value='quarterly')
    freq=CanonicalCombobox(top,textvariable=frequency,values=['quarterly','yearly','trailing','valuation'],state='readonly',width=20)
    freq.pack(side='left')
    status=tk.StringVar();ttk.Label(top,textvariable=status).pack(side='left',padx=12)
    cards=ttk.Frame(win,padding=10);cards.pack(fill='x')
    labels=[]
    for title in ('Выручка','Чистая прибыль','EPS','EBITDA'):
        card=ttk.LabelFrame(cards,text=tr(title),padding=10);card.pack(side='left',fill='both',expand=True,padx=4)
        label=ttk.Label(card,text='—');label.pack();labels.append(label)
    columns=('metric','period','value','currency','unit','method','updated','state')
    table=ttk.Frame(win);table.pack(fill='both',expand=True,padx=10)
    tree=ttk.Treeview(table,columns=columns,show='headings',height=10)
    for key,title in zip(columns,('Показатель','Период','Значение','Валюта','Единица','Расчёт','Обновлено','Статус')):
        tree.heading(key,text=tr(title));tree.column(key,width=170 if key in ('metric','method') else 110)
    tree.pack(side='left',fill='both',expand=True)
    scroll=ttk.Scrollbar(table,orient='vertical',command=tree.yview);tree.configure(yscrollcommand=scroll.set);scroll.pack(side='right',fill='y')
    horizontal=ttk.Scrollbar(win,orient='horizontal',command=tree.xview);tree.configure(xscrollcommand=horizontal.set);horizontal.pack(fill='x')
    calendar=tk.StringVar();ttk.Label(win,textvariable=calendar,wraplength=1150).pack(fill='x',padx=10)
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    fig=Figure(figsize=(9,2.6),dpi=100);ax=fig.add_subplot(111)
    canvas=FigureCanvasTkAgg(fig,master=win);canvas.get_tk_widget().pack(fill='both',expand=True)
    detail=tk.StringVar();ttk.Label(win,textvariable=detail,wraplength=1100).pack(fill='x',padx=10,pady=8)
    cached=app.store.setting('fundamentals:'+isin,{})
    if not cache_matches(cached,info):cached={}
    old_card=app.store.setting('card:'+isin,{})
    if old_card.get('symbol')!=(info.get('profile_symbol') or info.get('history_symbol')):old_card={}
    data={'value':cached or {'metrics':legacy_card(old_card),'status':'LEGACY_SAVED_QUARTERLY',
          'updated_at':old_card.get('fetched_at'),'calendar':old_card.get('events',{}),
          'valuation_metrics':overview_rows(old_card.get('overview',{}),old_card.get('fetched_at'),old_card.get('source','UNKNOWN'))}}
    def visible_rows():
        return data['value'].get('valuation_metrics',[]) if frequency.get()=='valuation' else [r for r in data['value'].get('metrics',[]) if r['frequency']==frequency.get()]
    def render(event=None):
        if not win.winfo_exists():return
        rows=visible_rows()
        locale=saved_language(app.store)
        status.set(tr(str(data['value'].get('status')))+' • '+tr(freshness(data['value']))+' • '+str(data['value'].get('updated_at') or ''))
        events=data['value'].get('calendar') or {}
        date_keys={'Earnings Date':'Дата отчёта','Dividend Date':'Дата дивиденда','Ex-Dividend Date':'Экс-дивидендная дата'}
        dates=[tr(label)+': '+(', '.join(map(str,events[key])) if isinstance(events[key],list) else str(events[key])) for key,label in date_keys.items() if events.get(key)]
        calendar.set(tr('Календарь: даты поставщика могут быть предварительными')+' • '+(' • '.join(dates) if dates else tr('Нет данных')))
        if data['value'].get('calendar_status'):calendar.set(calendar.get()+' • '+tr(data['value']['calendar_status'])+' • '+str(data['value'].get('calendar_updated_at')))
        for item in tree.get_children():tree.delete(item)
        for n,row in enumerate(rows):
            method=tr('Данные поставщика') if row['method'].startswith('PROVIDER_REPORTED') else tr('Прибыль / выручка')
            tree.insert('','end',iid=str(n),values=(tr(row['label']),row['period_end'] or tr('Период не указан'),format_value(row['value'],locale,kind='percent' if row['unit']=='ratio' else 'number',digits=4),row['currency'] or '—',tr(row['unit']),method,row.get('updated_at'),tr(row.get('cache_status') or row['status'])))
        for label,key in zip(labels,('TotalRevenue','NetIncome','DilutedEPS','EBITDA')):
            options=[r for r in rows if r['metric']==key]
            row=max(options,key=lambda r:r['period_end']) if options else None
            label.configure(text=format_value(row['value'],locale,kind='price' if row['unit']=='per_share' else 'amount',currency=row['currency'],compact=True)+'\n'+str(row['period_end'])+' • '+tr(row.get('cache_status') or row['status']) if row else tr('Нет данных'))
        ax.clear()
        values=sorted([r for r in rows if r['metric']=='TotalRevenue'],key=lambda r:r['period_end'])
        currencies={r['currency'] for r in values}
        if values and len(currencies)==1 and None not in currencies:
            from matplotlib.ticker import FuncFormatter
            ax.plot([r['period_end'] for r in values],[r['value'] for r in values],marker='o')
            ax.yaxis.set_major_formatter(FuncFormatter(lambda value,pos:format_value(value,locale,kind='number',compact=True)))
            ax.set_title(tr('Выручка')+' • '+tr(frequency.get())+' • '+values[0]['currency'])
        else:ax.set_axis_off();ax.set_title(tr('Нет сопоставимых данных'))
        fig.tight_layout();canvas.draw_idle()
        detail.set(tr('Выберите строку для источника и способа расчёта')+' • '+str(data['value'].get('source_url',''))+' • '+tr('Привязка по настроенному тикеру; ISIN независимо не подтверждён'))
    def inspect(event=None):
        selected=tree.selection()
        if not selected:return
        rows=visible_rows()
        row=rows[int(selected[0])]
        detail.set(f"{row['source']} • {row.get('source_url','')} • {row['updated_at']} • {tr(row['frequency'])} • {row['method']} • {row['provider_key']}")
    def refresh():
        symbol=info.get('profile_symbol') or info.get('history_symbol')
        if not symbol:status.set(tr('Тикер не указан'));return
        button.configure(state='disabled');status.set(tr('Загрузка…'))
        def done(value):
            current=next((i for i in app.watchlist if i['isin']==isin),None)
            if app.closing:return
            if not current or (current.get('profile_symbol') or current.get('history_symbol'))!=symbol:
                if win.winfo_exists():button.configure(state='normal');status.set(tr('Тикер изменён; откройте окно заново'))
                return
            if value.get('status')=='IDENTITY_MISMATCH':
                if win.winfo_exists():button.configure(state='normal');status.set(tr('Источник вернул другую компанию'))
                return
            available=bool(value.get('metrics') or value.get('valuation_metrics') or value.get('calendar'))
            value=merge_saved(data['value'],value);value['isin']=isin
            app.store.set_setting('fundamentals:'+isin,value);data['value']=value
            if win.winfo_exists():
                button.configure(state='normal');render()
                if not available:status.set(tr('Источник недоступен; сохранённые значения оставлены'))
        def fail(_):
            if win.winfo_exists():button.configure(state='normal');status.set(tr('Ошибка загрузки; сохранённые данные оставлены'))
        accepted=app.job(lambda:fetch_statements(symbol,app.store.directory/'provider_cache'),done,fail,key='fundamentals:'+isin,lane='misc',isin=isin)
        if not accepted:fail(None)
    button=ttk.Button(top,text=tr('Обновить отчётность'),command=refresh);button.pack(side='right')
    freq.bind('<<ComboboxSelected>>',render);tree.bind('<<TreeviewSelect>>',inspect);render()
