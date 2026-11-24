"""History audit and current calculation are separate display-only states.

Consumes accepted callbacks, never reads a feed, recalculates DQ or grants orders.
"""
import re
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from i18n import tr,language
from financial_format import format_date
from diagnostic_display import diagnostic_text


def describe(report,tf):
    report=report if isinstance(report,dict) else {}
    state=str(report.get('state') or 'UNKNOWN')[:40]
    raw=report.get('issues');issues=raw if isinstance(raw,(list,tuple)) else [raw] if raw else []
    raw=report.get('warnings');warnings=raw if isinstance(raw,(list,tuple)) else [raw] if raw else []
    reasons=[]
    patterns=[(r'Неполные агрегированные свечи: (\d+)','Неполных агрегатов: {count}'),
              (r'Неподтверждённые пустые интервалы: (\d+)','Неподтверждённых интервалов: {count}'),
              (r'Пропущенные свечи: (\d+)','Пропущенных свечей: {count}')]
    for raw in issues[:50]:
        text=str(raw)[:1200]
        for pattern,label in patterns:
            match=re.fullmatch(pattern,text)
            if match:reasons.append(tr(label,count=match[1]));break
        else:
            if text.startswith('SOURCE_COVERAGE_CONFLICT:'):text=tr('Полнота публикации не подтверждена')
            elif text.startswith('Невозможные OHLC'):text=tr('Некорректные OHLCV')
            else:text=diagnostic_text(text)
            reasons.append(text[:180]+('…' if len(text)>180 else ''))
    if not reasons:
        reasons=[tr('Замечаний нет') if state=='READY' and not warnings else
                 tr('Предупреждений: {count}',count=len(warnings)) if state=='READY' else tr('Причина не указана')]
    summary='\n'.join(reasons[:2])
    if len(issues)>2:summary+='\n'+tr('Ещё причин: {count}',count=len(issues)-2)
    identity=report.get('identity');identity=identity if isinstance(identity,dict) else {}
    lines=[tr('Таймфрейм')+': '+tf,tr('Статус')+': '+state,
           tr('ТФ проверки DQ')+': '+str(report.get('base_tf') or '—'),
           tr('Источник')+': '+' / '.join(str(identity.get(k) or '—') for k in ('source','symbol','venue','currency')),
           tr('Проверено')+': '+format_date(report.get('checked_at'),language(),clock=True),
           tr('Это проверка истории, не разрешение на сделку.')]
    classification=report.get('interval_classification')
    if isinstance(classification,dict):
        actual=classification.get('with_candles');expected=classification.get('expected_open_intervals')
        if type(actual) is int and type(expected) is int:
            lines.append(tr('Временных отметок: {actual}/{expected}',actual=actual,expected=expected))
    for label,items,limit in [('Причины',issues,50),('Предупреждения',warnings,20),('Диапазоны',report.get('ranges') or [],30)]:
        if not isinstance(items,(list,tuple)):continue
        lines.extend(['',tr(label)+':'])
        for item in items[:limit]:
            if label=='Диапазоны' and isinstance(item,(list,tuple)) and len(item)==2:
                text=' — '.join(str(v)[:500] for v in item)
            else:text=diagnostic_text(str(item)[:1200])
            lines.append(str(text)[:1200])
        if len(items)>limit:lines.append(tr('Показаны первые {count} из {total}',count=limit,total=len(items)))
    return dict(state=state,summary=summary,details='\n'.join(lines),
                style='DeskPositive.TLabel' if state=='READY' else 'DeskNegative.TLabel' if state=='INVALID' else 'DeskMuted.TLabel')


class Panel:
    def __init__(self,app,parent):
        self.app=app;self.context=None;self.details=''
        self.frame=host=ttk.Frame(parent,padding=(9,6),style='DeskCard.TFrame')
        self.heading=tk.StringVar(host);self.audit=tk.StringVar(host);self.reason=tk.StringVar(host);self.analysis=tk.StringVar(host)
        title=ttk.Frame(host,style='Desk.TFrame');title.pack(fill='x')
        ttk.Label(title,textvariable=self.heading,style='Desk.TLabel',font=('Segoe UI',10,'bold')).pack(side='left')
        self.button=ttk.Button(title,text='ⓘ',width=3,command=self.show_details,state='disabled');self.button.pack(side='right')
        self.audit_label=ttk.Label(host,textvariable=self.audit,style='DeskMuted.TLabel',font=('Segoe UI',9,'bold'));self.audit_label.pack(anchor='w')
        ttk.Label(host,textvariable=self.reason,style='DeskMuted.TLabel',wraplength=260,font=('Segoe UI',9)).pack(anchor='w',pady=(2,5))
        self.analysis_label=ttk.Label(host,textvariable=self.analysis,style='DeskMuted.TLabel',wraplength=260,font=('Segoe UI',9));self.analysis_label.pack(anchor='w')
        ttk.Label(host,text=tr('Цена и риск — перед сделкой'),style='DeskMuted.TLabel',font=('Segoe UI',8)).pack(anchor='w',pady=(3,0))
        self.ensure_context()

    def key(self):
        app=self.app;item=next((x for x in app.watchlist if x.get('name')==app.symbol.get()),{})
        return item.get('isin'),app.timeframe.get(),app.period.get()

    def ensure_context(self):
        key=self.key()
        if key!=self.context:
            self.context=key;self.details='';self.heading.set(tr('История CP')+' · '+key[1])
            self.audit.set(tr('Ожидание DQ'));self.reason.set('');self.analysis.set(tr('Анализ: ожидается расчёт'))
            self.audit_label.configure(style='DeskMuted.TLabel');self.analysis_label.configure(style='DeskMuted.TLabel');self.button.configure(state='disabled')
        return key

    def bind_chart(self,prepared):
        key=self.ensure_context();frame=prepared.get('frame')
        if frame is None or frame.empty:
            self.details='';self.audit.set(tr('Нет подтверждённого ряда'));self.reason.set('');self.analysis.set(tr('Анализ: ожидается расчёт'));self.audit_label.configure(style='DeskMuted.TLabel');self.analysis_label.configure(style='DeskMuted.TLabel');self.button.configure(state='disabled');return
        meta=frame.attrs
        if meta.get('timeframe') not in (None,key[1]) or meta.get('isin') not in (None,key[0]):return
        model=describe(meta.get('data_quality'),key[1]);self.details=model['details']
        self.audit.set('DQ: '+model['state']);self.reason.set(model['summary']);self.audit_label.configure(style=model['style']);self.button.configure(state='normal')

    def bind_engine(self,engine):
        key=self.ensure_context()
        valid=isinstance(engine,dict) and engine.get('signal') in ('WAIT','BUY','SELL','HOLD')
        contexts=engine.get('timeframe_context',{}) if valid else {}
        mismatch=isinstance(contexts,dict) and bool(contexts) and key[1] not in contexts
        if not valid or mismatch or engine.get('status') not in ('READY','DATA BLOCKED'):
            text=tr('Анализ: ожидается расчёт');style='DeskMuted.TLabel'
        elif engine['status']=='DATA BLOCKED' or engine.get('blockers'):
            text=tr('Анализ: данные блокируют расчёт');style='DeskNegative.TLabel'
        else:text=tr('Анализ доступен')+' · '+engine['signal'];style='DeskPositive.TLabel'
        self.analysis.set(text);self.analysis_label.configure(style=style)

    def show_details(self):
        if not self.details:return
        win=tk.Toplevel(self.app.root);win.title(tr('Проверка истории')+' · '+self.app.symbol.get()+' · '+self.context[1]);win.geometry('740x570')
        box=ScrolledText(win,wrap='word',font=('Segoe UI',10),padx=12,pady=12);box.pack(fill='both',expand=True)
        style=ttk.Style(win);box.configure(background=style.lookup('Desk.TFrame','background'),foreground=style.lookup('Desk.TLabel','foreground'))
        box.insert('1.0',self.details);box.configure(state='disabled');win.bind('<Escape>',lambda _:win.destroy())
