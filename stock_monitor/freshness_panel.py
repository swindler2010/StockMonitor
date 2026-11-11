"""Bounded diagnostic table, read through the existing misc Dispatcher lane."""
import tkinter as tk
from tkinter import ttk
from i18n import language

TEXT={
 'title':('Свежесть данных','Datenaktualität','Data freshness'),
 'refresh':('Обновить таблицу','Tabelle aktualisieren','Refresh table'),
 'loading':('Чтение диагностики…','Diagnose wird gelesen…','Reading diagnostics…'),
 'error':('Диагностика недоступна','Diagnose nicht verfügbar','Diagnostics unavailable'),
 'note':('Диагностика, не торговый допуск. Время поставщика не подтверждено.','Diagnose, keine Handelsfreigabe. Anbieterzeit unbestätigt.','Diagnostics, not trading permission. Provider time is unconfirmed.'),
 'name':('Компания / ISIN','Unternehmen / ISIN','Company / ISIN'),
 'source':('Источник / площадка','Quelle / Handelsplatz','Source / venue'),
 'status':('Статус','Status','Status'),
 'attempt':('Последняя загрузка','Letzter Abruf','Latest attempt'),
 'ERROR':('Ошибка','Fehler','Error'),
 'EMPTY':('Пустой ответ','Leere Antwort','Empty response'),
 'RECEIVED':('Ответ получен','Antwort erhalten','Response received'),
 'INVALID':('Некорректный ответ','Ungültige Antwort','Invalid response'),
 'IDENTITY_MISMATCH':('Другой инструмент','Anderes Instrument','Different instrument'),
 'LEGACY_UNCLASSIFIED':('Без классификации','Nicht klassifiziert','Unclassified'),
 'NOT_OBSERVED':('Нет наблюдения','Nicht beobachtet','Not observed'),
 'reported':('Время поставщика (Берлин)','Anbieterzeit (Berlin)','Provider time (Berlin)'),
 'age':('Возраст, мин','Alter, Min.','Age, min'),
 'received':('Ответ получен (Берлин)','Antwort erhalten (Berlin)','Response received (Berlin)'),
 'UNKNOWN':('НЕИЗВЕСТНО','UNBEKANNT','UNKNOWN'),
 'MARKET_CLOSED':('РЫНОК ЗАКРЫТ','MARKT GESCHLOSSEN','MARKET CLOSED'),
 'counts':('Акций: {stocks} • рядов: {sources} • проверено: {at}','Aktien: {stocks} • Reihen: {sources} • geprüft: {at}','Stocks: {stocks} • series: {sources} • checked: {at}')}
def label(key):return TEXT[key][{'ru':0,'de':1,'en':2}.get(language(),0)]

def show(app):
    existing=getattr(app,'_freshness_window',None)
    if existing is not None and existing.winfo_exists():existing.lift();return
    from freshness_runtime import runtime
    controller=runtime(app)
    win=tk.Toplevel(app.root);app._freshness_window=win;win.title(label('title'));win.geometry('1250x650')
    bar=ttk.Frame(win,padding=8);bar.pack(fill='x')
    status=tk.StringVar(value=label('loading'))
    ttk.Label(win,text=label('note'),padding=8).pack(fill='x')
    ttk.Label(win,textvariable=status,padding=8).pack(fill='x')
    area=ttk.Frame(win);area.pack(fill='both',expand=True)
    columns=('name','source','status','attempt','reported','age','received')
    table=ttk.Treeview(area,columns=columns,show='headings')
    for key,width in zip(columns,(320,200,140,160,220,90,220)):
        table.heading(key,text=label(key));table.column(key,width=width,minwidth=60,stretch=False)
    horizontal=ttk.Scrollbar(area,orient='horizontal',command=table.xview)
    table.configure(xscrollcommand=horizontal.set);horizontal.pack(side='bottom',fill='x')
    scroll=ttk.Scrollbar(area,orient='vertical',command=table.yview);table.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right',fill='y');table.pack(side='left',fill='both',expand=True)
    def done(value):
        if not win.winfo_exists():return
        names={i['isin']:i.get('name') or i['isin'] for i in value['stocks']}
        table.delete(*table.get_children())
        for row in value['sources']:
            report=row['reported_market_time'];age=report['age_seconds']
            table.insert('', 'end',values=(names[row['isin']]+' / '+row['isin'],row['source']+' / '+row['venue'],
                label(row['status']),label(row.get('latest_attempt',{}).get('outcome','NOT_OBSERVED')),
                report['berlin'] or '—','—' if age is None else f'{age/60:.2f}',row['response_received']['berlin'] or '—'))
        status.set(label('counts').format(stocks=value['stock_count'],sources=value['source_count'],at=value['checked_at_berlin']))
    def fail(_):
        if win.winfo_exists():status.set(label('error'))
    def refresh():
        if getattr(app,'closing',False):return
        status.set(label('loading'));controller.request('PANEL_REFRESH')
    def periodic():
        if not win.winfo_exists():return
        controller.request('VISIBLE_AGE_TICK');win.after(60000,periodic)
    def destroyed(event):
        if event.widget is win and getattr(app,'_freshness_listener',None) is done:
            app._freshness_listener=None;app._freshness_error=None
    app._freshness_listener=done;app._freshness_error=fail
    win.bind('<Destroy>',destroyed,add='+')
    ttk.Button(bar,text=label('refresh'),command=refresh).pack(side='left')
    refresh()
    win.after(60000,periodic)
