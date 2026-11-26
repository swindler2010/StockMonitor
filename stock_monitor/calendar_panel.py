"""Small translated official-calendar view over the existing news worker."""
from datetime import date
from zoneinfo import ZoneInfo
from datetime import datetime
import tkinter as tk
from tkinter import ttk
import webbrowser
from i18n import language
from official_calendar import SETTING, SOURCES, SOURCE_PAGES, upcoming


def label(ru, de, en): return {'ru': ru, 'de': de, 'en': en}.get(language(), en)


def title(row):
    return {
        'FOMC': label('ФРС: заседание FOMC', 'Fed: FOMC-Sitzung', 'Fed: FOMC meeting'),
        'GENERAL': label('ЕЦБ: Генеральный совет', 'EZB: Erweiterter Rat', 'ECB: General Council'),
        'NON_MONETARY': label('ЕЦБ: заседание без денежной политики', 'EZB: keine geldpolitische Sitzung', 'ECB: non-monetary policy meeting'),
        'MONETARY_DAY1': label('ЕЦБ: денежная политика, день 1', 'EZB: Geldpolitik, Tag 1', 'ECB: monetary policy, day 1'),
        'MONETARY_DAY2': label('ЕЦБ: день 2 и пресс-конференция', 'EZB: Tag 2 und Pressekonferenz', 'ECB: day 2 and press conference'),
        'US_CPI': label('США: потребительская инфляция (CPI)', 'USA: Verbraucherpreise (CPI)', 'US consumer price index (CPI)'),
        'US_PPI': label('США: цены производителей (PPI)', 'USA: Erzeugerpreise (PPI)', 'US producer price index (PPI)'),
        'US_JOBS': label('США: занятость и безработица', 'USA: Arbeitsmarktbericht', 'US employment situation'),
        'US_JOLTS': label('США: вакансии и текучесть (JOLTS)', 'USA: Offene Stellen (JOLTS)', 'US job openings and turnover (JOLTS)'),
        'US_GDP': label('США: ВВП', 'USA: Bruttoinlandsprodukt', 'US gross domestic product'),
        'US_PCE': label('США: доходы, расходы и инфляция PCE', 'USA: Einkommen, Ausgaben und PCE-Inflation', 'US income, spending and PCE inflation'),
    }.get(row['kind'], label('ЕЦБ: другое заседание (см. оригинал)', 'EZB: weitere Sitzung (siehe Original)', 'ECB: other meeting (see original)'))


def dates(row, compact=False):
    start, end = date.fromisoformat(row['date']), date.fromisoformat(row['end_date'])
    fmt = '%Y-%m-%d' if language() == 'en' else '%d.%m.%Y'
    if row.get('starts_at'):
        return datetime.fromisoformat(row['starts_at']).astimezone(ZoneInfo('Europe/Berlin')).strftime(fmt+' %H:%M')
    if compact and start != end and start.year == end.year and start.month == end.month:
        return (start.strftime('%Y-%m-')+str(start.day)+'–'+str(end.day)) if language()=='en' else (str(start.day)+'–'+end.strftime('%d.%m.%Y'))
    return start.strftime(fmt) + (' — '+end.strftime(fmt) if end != start else '')


class Panel:
    def __init__(self, app, host):
        self.app, self.host, self.snapshot, self.window = app, host, {}, None
        self.closed = False
        top = ttk.Frame(host, style='Desk.TFrame'); top.pack(fill='x')
        ttk.Label(top, text=label('▦ Макроэкономический календарь', '▦ Wirtschaftskalender', '▦ Economic calendar'), style='Desk.TLabel', font=('Segoe UI', 10, 'bold')).pack(side='left')
        ttk.Button(top, text='↗', width=3, command=self.show).pack(side='right')
        self.note = tk.StringVar(host, label('Чтение расписаний…', 'Termine werden gelesen…', 'Reading schedules…'))
        ttk.Label(host, textvariable=self.note, style='DeskMuted.TLabel', font=('Segoe UI', 8)).pack(anchor='w')
        self.tree = ttk.Treeview(host, columns=('date', 'title'), show='', height=3, style='DeskNews.Treeview')
        self.tree.column('date', width=105, stretch=False); self.tree.column('title', width=280)
        self.tree.pack(fill='both', expand=True); self.tree.bind('<Double-1>', lambda _: self.show())
        host.bind('<Destroy>', lambda event: self.cleanup() if event.widget is host else None, add='+')
        app._calendar_listener = self.receive
        app.job(lambda: app.store.setting(SETTING, {}), self.receive, lambda _: self.receive({}), lane='read', key='official-calendar-view')

    def receive(self, snapshot):
        if self.closed or getattr(self.app, 'closing', False): return
        self.snapshot = snapshot
        self.rows = upcoming(snapshot, today=datetime.now(ZoneInfo('Europe/Berlin')).date())
        self.tree.delete(*self.tree.get_children())
        for row in self.rows[:3]: self.tree.insert('', 'end', values=(dates(row, compact=True), ('⚠ ' if row['stale'] else '')+title(row)))
        self.note.set(label('Время по Берлину, когда указано источником', 'Berliner Zeit, sofern angegeben', 'Berlin time where supplied') if self.rows else
                      label('Нет подтверждённых расписаний — откройте ↗', 'Keine bestätigten Termine — öffnen Sie ↗', 'No confirmed schedules — open ↗'))
        if self.window is not None and self.window.winfo_exists(): self.fill()

    def show(self):
        if self.window is not None and self.window.winfo_exists(): self.window.lift(); return
        from main_screen import native_titlebar
        from themes import THEMES
        self.window = win = tk.Toplevel(self.app.root)
        win.title(label('Официальный макроэкономический календарь', 'Offizieller Wirtschaftskalender', 'Official economic calendar'))
        win.geometry('1000x620'); win.minsize(800, 480)
        box = ttk.Frame(win, padding=15); box.pack(fill='both', expand=True)
        ttk.Label(box, text=label('Даты заседаний и публикаций могут меняться. Время по Берлину.',
                                 'Termine für Sitzungen und Veröffentlichungen können sich ändern. Berliner Zeit.',
                                 'Meeting and release schedules are subject to change. Berlin time.'), wraplength=940).pack(anchor='w', pady=8)
        self.status = tk.StringVar(win)
        ttk.Label(box, textvariable=self.status, wraplength=940).pack(anchor='w', pady=6)
        self.table = ttk.Treeview(box, columns=('date', 'event', 'status'), show='headings', height=12)
        for key, heading, width in [('date', label('Дата / диапазон', 'Datum / Zeitraum', 'Date / range'), 205),
                                    ('event', label('Событие', 'Ereignis', 'Event'), 500),
                                    ('status', label('Проверка', 'Bestätigung', 'Verification'), 160)]:
            self.table.heading(key, text=heading); self.table.column(key, width=width, stretch=key == 'event')
        self.table.pack(fill='both', expand=True)
        self.table.bind('<Double-1>', lambda _: self.original())
        bar = ttk.Frame(box); bar.pack(fill='x')
        ttk.Button(bar, text=label('Обновить', 'Aktualisieren', 'Refresh'), command=self.refresh).pack(side='left')
        ttk.Button(bar, text=label('Оригинал выбранного события ↗', 'Original des gewählten Termins ↗', 'Selected event source ↗'), command=self.original).pack(side='left', padx=10)
        ttk.Button(bar, text=label('Закрыть', 'Schließen', 'Close'), command=win.destroy).pack(side='right')
        palette = THEMES.get(self.app.store.setting('theme', 'Тёмная'), THEMES['Тёмная'])
        win.bind('<Map>', lambda event: win.after_idle(lambda: native_titlebar(win, palette)) if event.widget is win else None, add='+')
        win.bind('<Escape>', lambda _: win.destroy()); self.fill()

    def fill(self):
        self.table.delete(*self.table.get_children())
        for n, row in enumerate(self.rows):
            self.table.insert('', 'end', iid=str(n), values=(dates(row), title(row), label('Старое расписание', 'Alter Stand', 'Stale schedule') if row['stale'] else label('Источник получен', 'Quelle abgerufen', 'Source retrieved')))
        parts = []
        for source in SOURCES:
            value = self.snapshot.get('sources', {}).get(source, {})
            state={'AVAILABLE':label('Получено','Abgerufen','Retrieved'),'STALE_CACHE':label('Старое расписание','Alter Stand','Stale schedule'),'UNAVAILABLE':label('Недоступно','Nicht verfügbar','Unavailable')}.get(value.get('status'),label('Не загружено','Nicht geladen','Not loaded'))
            parts.append(source+': '+str(value.get('fetched_at') or '—')+' · '+state+(' · '+str(value['error']) if value.get('error') else ''))
        self.status.set('\n'.join(parts))

    def original(self):
        selected = self.table.selection()
        if selected and int(selected[0]) < len(self.rows): webbrowser.open(SOURCE_PAGES[self.rows[int(selected[0])]['source']])

    def refresh(self):
        runtime = getattr(self.app, 'news_runtime', None)
        if runtime and runtime.refresh_calendar(): self.status.set(label('Загрузка расписаний…', 'Termine werden geladen…', 'Loading schedules…'))
        else: self.status.set(label('Обновление уже выполняется; повторите позже', 'Aktualisierung läuft; später erneut versuchen', 'Refresh is busy; try again later'))

    def cleanup(self):
        self.closed = True
        if getattr(self.app, '_calendar_listener', None) == self.receive: self.app._calendar_listener = None
        if self.window is not None and self.window.winfo_exists(): self.window.destroy()
