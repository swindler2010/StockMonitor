"""Read-only news tab and saved decision context for the V1 demo account."""
from datetime import datetime
import tkinter as tk
from tkinter import ttk
import webbrowser
from zoneinfo import ZoneInfo

from calendar_panel import dates, label, title as calendar_title
from financial_format import format_value
from i18n import language
from news_dashboard import details, translation_note
from news_engine import safe_url
from paper_v1_news import status


BERLIN = ZoneInfo('Europe/Berlin')


def local_time(value):
    return datetime.fromisoformat(value).astimezone(BERLIN).strftime('%d.%m %H:%M') if value else '—'


def feed_label(state):
    return {
        'AVAILABLE': label('Получено', 'Abgerufen', 'Fetched'),
        'PARTIAL': label('Часть источников недоступна', 'Quellen teilweise nicht verfügbar', 'Some sources unavailable'),
        'EMPTY': label('Свежих публикаций нет', 'Keine aktuellen Meldungen', 'No recent articles'),
        'STALE_CACHE': label('Устаревший кэш', 'Veralteter Cache', 'Stale cache'),
        'IDENTITY_CHANGED': label('Лента компании требует обновления', 'Unternehmensmeldungen müssen aktualisiert werden', 'Company feed needs refresh'),
        'NOT_CHECKED': label('Ожидается обновление', 'Aktualisierung ausstehend', 'Awaiting refresh'),
    }.get(state, label('Источник недоступен', 'Quelle nicht verfügbar', 'Source unavailable'))


def feed_line(name, feed):
    at = local_time(feed.get('fetched_at'))
    return f"{name} · {feed_label(feed['status'])} · {feed['recent']} · {at}"


def company_rows(packet):
    names={
        'EARNINGS_DATE':label('Ожидаемая отчётность','Erwarteter Bericht','Expected earnings'),
        'EX_DIVIDEND':label('Дата без дивиденда','Ex-Dividenden-Tag','Ex-dividend date'),
        'DIVIDEND_PAYMENT':label('Дата выплаты дивиденда','Dividendenzahlung','Dividend payment'),
        'FILING':label('Публикация эмитента','Unternehmensmeldung','Company filing'),
    }
    estimate=label('Дата поставщика, может измениться','Anbietertermin, Änderungen möglich','Provider date, subject to change')
    rows=[]
    for item in (*packet.get('dates',[]),*packet.get('filings',[])):
        name=names[item['kind']]
        text=(f"{name} {item['form']}\n{item['title']}" if item['kind']=='FILING' else name+'\n'+estimate)
        rows.append({**item,'label':name if item['kind']=='FILING' else name+' · '+estimate,'body':text})
    metrics={
        'TotalRevenue':label('Выручка','Umsatz','Revenue'),
        'NetIncome':label('Чистая прибыль','Nettogewinn','Net income'),
        'DilutedEPS':'EPS',
        'EBITDA':'EBITDA',
    }
    periods={}
    for row in packet.get('reports',[]):
        periods.setdefault(row['period_end'],[]).append(row)
    quarter=label('Квартальный отчёт','Quartalsbericht','Quarterly results')
    for period, values in sorted(periods.items(),reverse=True):
        lines=[quarter+' · '+label('конец периода','Periodenende','period end')+' '+period]
        for value in values:
            display=format_value(value['value'],language(),kind='price' if value['unit']=='per_share' else 'amount',
                                 currency=value.get('currency'),compact=True)
            if not value.get('currency'):
                display+=' · '+label('валюта не указана','Währung unbekannt','currency unknown')
            lines.append(metrics[value['metric']]+' · '+display)
        rows.append(dict(kind='QUARTERLY_REPORT',label=quarter+' · '+label('конец периода','Periodenende','period end'),
            body='\n'.join(lines),date=period,end_date=period,source=values[0]['source'],
            url=packet.get('report_url'),stale=any(r['stale'] for r in values)))
    return rows


class Panel:
    def __init__(self, app, notebook, parent):
        self.app, self.notebook, self.parent = app, notebook, parent
        self.generation, self.busy = 0, False
        self.rows = {}
        self.details_key = None
        self.packet = None
        from paper_account import instrument_allowed
        stocks = [info for info in app.watchlist if instrument_allowed(info)]
        self.targets = [None]+[info['isin'] for info in stocks]
        names = [label('Рынок', 'Markt', 'Market')]+[
            f"{info['name']} ({info['isin']})" for info in stocks]
        bar=ttk.Frame(parent)
        bar.pack(fill='x', padx=6, pady=(5,2))
        self.choice=ttk.Combobox(bar,state='readonly',values=names,width=45)
        self.choice.pack(side='left')
        current=getattr(app,'symbol',None)
        current=current.get() if current is not None else None
        self.choice.current(next((i for i,name in enumerate(names) if name.startswith((current or '')+' (')),0))
        self.choice.bind('<<ComboboxSelected>>',lambda _:self.changed())
        self.filter=ttk.Combobox(bar,state='readonly',width=26,values=(
            label('Всё','Alle','All'),
            label('Отчёты и события','Berichte und Ereignisse','Reports and events'),
            label('Новости','Nachrichten','News')))
        self.filter.current(0)
        self.filter.pack(side='left',padx=8)
        self.filter.bind('<<ComboboxSelected>>',lambda _:self.render(self.packet) if self.packet else None)
        self.open_button=ttk.Button(bar,text=label('Оригинал ↗','Original ↗','Original ↗'),
                                    command=self.open_original,state='disabled')
        self.open_button.pack(side='right')
        self.note=tk.StringVar(parent)
        ttk.Label(parent,textvariable=self.note,wraplength=1040).pack(fill='x',padx=6,pady=3)
        detail_box=ttk.Frame(parent)
        detail_box.pack(side='bottom',fill='x',padx=6,pady=4)
        self.detail=tk.Text(detail_box,height=3,wrap='word',state='disabled')
        detail_scroll=ttk.Scrollbar(detail_box,command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        detail_scroll.pack(side='right',fill='y')
        self.detail.pack(side='left',fill='x',expand=True)
        body=ttk.Frame(parent)
        body.pack(fill='both',expand=True,padx=6)
        self.tree=ttk.Treeview(body,columns=('scope','time','source','title'),show='headings',height=4)
        for column,heading,width in (
            ('scope',label('Раздел','Bereich','Scope'),105),
            ('time',label('Берлин / дата','Berlin / Datum','Berlin / date'),145),
            ('source',label('Источник','Quelle','Source'),145),
            ('title',label('Заголовок','Titel','Headline'),550)):
            self.tree.heading(column,text=heading)
            self.tree.column(column,width=width,stretch=column=='title')
        scroll=ttk.Scrollbar(body,command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left',fill='both',expand=True)
        scroll.pack(side='right',fill='y')
        self.tree.bind('<<TreeviewSelect>>',lambda _:self.show_details())
        self.tree.bind('<Double-1>',lambda _:self.open_original())
        notebook.bind('<<NotebookTabChanged>>',lambda _:self.refresh(),add='+')
        self.timer=parent.after(20000,self.tick)
        parent.bind('<Destroy>',self.destroy,add='+')

    def destroy(self,event):
        if event.widget is self.parent and self.timer:
            self.parent.after_cancel(self.timer)
            self.timer=None

    def active(self):
        return not self.app.closing and self.parent.winfo_exists() and self.notebook.select()==str(self.parent)

    def tick(self):
        self.refresh()
        self.timer=self.parent.after(20000,self.tick)

    def changed(self):
        self.generation+=1
        self.packet=None
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.show_details()
        self.note.set(label('Читаю новостной фон…','Nachrichten werden gelesen…','Reading news…'))
        self.refresh()

    def refresh(self):
        if self.busy or not self.active():
            return
        index=self.choice.current()
        isin=self.targets[index] if index>=0 else None
        generation=self.generation
        target=language()
        self.busy=True
        if not self.rows:
            self.note.set(label('Читаю новостной фон…','Nachrichten werden gelesen…','Reading news…'))

        def done(packet):
            self.busy=False
            if not self.active():
                return
            if generation!=self.generation:
                self.refresh()
                return
            self.render(packet)
            self.translate_visible(packet,target)

        def failed(_):
            self.busy=False
            if self.active() and generation==self.generation:
                self.note.set(label('Кэш новостей временно недоступен','Nachrichtencache vorübergehend nicht verfügbar','News cache temporarily unavailable'))

        if self.app.job(lambda:status(self.app.store.path,isin,language=target),done,failed,
                        lane='read',key=('v1-news-view',id(self))) is False:
            failed(None)

    def translate_visible(self,packet,target):
        for key in ('company','market'):
            feed=packet.get(key)
            if not feed:
                continue
            pending=[{**row,'title':row['original_title'],'summary':row['original_summary']}
                      for row in feed['articles'] if row.get('translation',{}).get('status')=='PENDING']
            if pending:
                self.app.news_runtime.translate_news(feed['key'],pending[:3],target)
                break

    def render(self,packet):
        self.packet=packet
        company=label('Компания','Unternehmen','Company')
        market=label('Рынок','Markt','Market')
        issuer=label('Отчёты и события','Berichte und Ereignisse','Reports and events')
        filter_index=self.filter.current()
        rows=[]
        states=[]
        if 'company_events' in packet:
            sources=packet['company_events']
            states.append(label('Календарь компании','Unternehmenskalender','Company calendar')+' · '+feed_label(sources['calendar_status']))
            states.append(label('Отчёты','Berichte','Reports')+' · '+feed_label(sources['report_status']))
            if filter_index!=2:
                for event in company_rows(sources):
                    title=('⚠ ' if event['stale'] else '')+event['label']
                    rows.append((event['kind']+' '+event['date']+' '+(event.get('url') or ''),event,
                                 (issuer,dates(event,compact=True),event['source'],title)))
        for key,name in (('company',company),('market',market)):
            if key not in packet:
                continue
            feed=packet[key]
            states.append(feed_line(name,feed))
            for item in feed['articles'] if filter_index!=1 else ():
                text=item['title']
                if item.get('translation',{}).get('status')=='TRANSLATED':
                    text+=' · '+translation_note(item)
                sections=[label_item for key,label_item in (
                    ('earnings',label('Отчётность','Ergebnis','Earnings')),
                    ('guidance',label('Прогноз','Ausblick','Guidance')),
                    ('regulation',label('Регулирование','Regulierung','Regulation')),
                    ('buyback',label('Выкуп','Rückkauf','Buyback')),
                    ('split',label('Дробление','Split','Split')),
                ) if key in item.get('events',[])]
                if sections:
                    text=' / '.join(sections)+' · '+text
                rows.append((key+' '+item['url'],item,(name,local_time(item['published']),item.get('provider') or '—',text)))
        calendar=packet['calendar']
        states.append(' / '.join(key+' · '+feed_label(value) for key,value in calendar['sources'].items()))
        for event in calendar['events'] if filter_index!=2 else ():
            name=label('Календарь','Kalender','Calendar')
            text=('⚠ ' if event['stale'] else '')+calendar_title(event)
            rows.append((event['source']+' '+event['date']+' '+event['kind'],event,
                         (name,dates(event,compact=True),event['source'],text)))
        self.note.set('   |   '.join(states))
        previous=self.tree.selection()
        self.rows={key:item for key,item,_ in rows}
        self.tree.delete(*self.tree.get_children())
        for key,_,values in rows:
            if not self.tree.exists(key):
                self.tree.insert('', 'end', iid=key, values=values)
        if previous and self.tree.exists(previous[0]):
            self.tree.selection_set(previous[0])
        elif rows:
            self.tree.selection_set(rows[0][0])
        self.show_details()

    def selected(self):
        selection=self.tree.selection()
        return self.rows.get(selection[0],{}) if selection else {}

    def show_details(self):
        row=self.selected()
        key=row.get('url'), row.get('title'), row.get('date'), row.get('kind')
        offset=self.detail.yview()[0] if key==self.details_key else 0
        self.details_key=key
        if 'body' in row:
            text=row['body']
        elif 'end_date' in row:
            text=calendar_title(row)+' · '+dates(row)+'\n'+row['original']
        else:
            text=details(row) if row else ''
        self.detail.configure(state='normal')
        self.detail.delete('1.0','end')
        self.detail.insert('1.0',text)
        self.detail.yview_moveto(offset)
        self.detail.configure(state='disabled')
        self.open_button.configure(state='normal' if safe_url(row.get('url')) else 'disabled')

    def open_original(self):
        url=safe_url(self.selected().get('url'))
        if url:
            webbrowser.open(url)


def show_saved(parent, background):
    win=tk.Toplevel(parent)
    win.title(label('Новости на момент решения V1','Nachrichten bei der V1-Entscheidung','News at the V1 decision'))
    win.geometry('810x420')
    text=tk.Text(win,wrap='word')
    text.pack(fill='both',expand=True,padx=8,pady=8)
    sections=[]
    for key,name in (('company',label('Компания','Unternehmen','Company')),
                     ('market',label('Рынок','Markt','Market'))):
        feed=background.get(key)
        if not feed:
            continue
        lines=[feed_line(name,feed),label('Сохранённые оригиналы','Gespeicherte Originale','Saved originals')]
        for row in feed['articles']:
            lines.extend((f"{local_time(row['published'])} · {row.get('provider') or '—'}",
                          row['title'],row['url']))
        sections.append('\n'.join(lines))
    for event in background.get('calendar',{}).get('events',[]):
        sections.append(' · '.join((dates(event),calendar_title(event),event['url'])))
    for item in company_rows(background.get('company_events',{})):
        sections.append('\n'.join((dates(item),item['body'],item.get('url') or '')))
    text.insert('1.0','\n\n'.join(sections) or label('Кэш был недоступен','Cache war nicht verfügbar','Cache was unavailable'))
    text.configure(state='disabled')

