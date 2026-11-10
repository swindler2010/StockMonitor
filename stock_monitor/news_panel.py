from i18n import tr
"""News window for the existing desktop; no new application or trading calls."""
import tkinter as tk
from tkinter import ttk
import webbrowser
from datetime import datetime, timezone
from news_engine import normalize


def show(app):
    from tkinter.scrolledtext import ScrolledText
    def text_area(parent):
        widget=ScrolledText(parent,wrap='word',font=('Segoe UI',10),padx=12,pady=12)
        widget.pack(fill='both',expand=True);return widget
    def set_text(widget,text):
        widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',text);widget.configure(state='disabled')
    from news_runtime import NewsRuntime
    existing = getattr(app, '_news_window', None)
    if existing is not None and existing.winfo_exists():
        existing.deiconify(); existing.lift(); return
    if not hasattr(app, 'news_runtime'):
        app.news_runtime = NewsRuntime(app)
        app.news_runtime.start()
    win = tk.Toplevel(app.root); app._news_window = win
    win.title(tr('Новости — оригинальные материалы')); win.geometry('1100x720')
    bar = ttk.Frame(win, padding=10); bar.pack(fill='x')
    targets = [(tr('Рынок и экономика'), 'market', None)] + [
        (i['name']+' • '+i['isin'], i['isin'], i.get('profile_symbol') or i.get('history_symbol'))
        for i in app.watchlist if i.get('kind') != 'benchmark']
    choice = ttk.Combobox(bar, state='readonly', values=[x[0] for x in targets], width=50)
    choice.pack(side='left'); choice.current(0)
    status = tk.StringVar(); ttk.Label(win, textvariable=status).pack(fill='x')
    box = text_area(win); links = []
    def render(value):
        if not win.winfo_exists():return
        links.clear(); lines=[]
        labels={'AVAILABLE':tr('Загружено'),'PARTIAL':tr('Часть источников недоступна'),'EMPTY':tr('Материалов нет'),'UNAVAILABLE':tr('Источники недоступны'),'STALE_CACHE':tr('Сохранённые данные — обновление не подтверждено'),'NOT_LOADED':tr('Ещё не загружено'),'ERROR':tr('Ошибка загрузки')}
        state=value.get('status','NOT_LOADED')
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(value['attempted_at'])).total_seconds()
            if age>3600 and value.get('articles'):state='STALE_CACHE'
        except (ValueError,TypeError,KeyError):pass
        status.set(labels.get(state,state)+tr(' • Получено: ')+str(value.get('fetched_at') or tr('нет'))+' • '+str(value.get('errors') or ''))
        events={'earnings':tr('отчётность'),'guidance':tr('прогноз'),'dividend':tr('дивиденды'),'deal':tr('сделка'),'management':tr('руководство')}
        freshness={'CURRENT':tr('за последние 7 дней'),'STALE':tr('старше 7 дней'),'UNKNOWN':tr('дата неизвестна'),'FUTURE':tr('дата в будущем — проверить источник')}
        for article in normalize(value.get('articles', [])):
            links.append(article['url'])
            tags=', '.join(events[e] for e in article.get('events',[]) if e in events)
            source_note=tr('Издатель указан источником') if article.get('publisher_status')=='NAMED_BY_FEED' else tr('Показан домен ссылки; первичный издатель может отличаться')
            link_note=tr('Ссылка агрегатора ведёт к публикации') if article.get('link_kind')=='AGGREGATOR_LINK_TO_ORIGINAL' else tr('Ссылка на публикацию')
            text_note=tr('\nИсточник содержит повреждённые символы') if article.get('text_quality')=='UPSTREAM_REPLACEMENT_CHARACTERS' else ''
            relevance=tr('В тексте упомянута компания; это не проверка биржевой записи.') if article.get('scope')=='company' else tr('Рынок и экономика')
            if article.get('relevance')=='OFFICIAL_ISSUER_FEED':relevance=tr('Официальная лента компании')
            lines.append(f"{len(links)}. {article['title']}\n{article.get('published') or tr('Дата неизвестна')} • {article.get('provider')} • {freshness.get(article.get('date_status'),'')}\n{source_note}\n{article.get('summary') or tr('Краткое содержание не предоставлено источником')}\n"+(tr('Возможное событие по заголовку: ')+tags+'\n' if tags else '')+f"{relevance}{text_note}\n{link_note}: {article['url']}")
        attempts='\n'.join(a['provider']+': '+a['status']+(' / '+a['error'] if a.get('error') else '') for a in value.get('source_attempts',[]))
        set_text(box, tr('Тексты приведены на языке оригинала.\nСобытия выделены по словам в заголовке; требуется проверка первоисточника.\n\n')+('\n\n'.join(lines) or tr('Материалы отсутствуют. Это не означает отсутствия событий.'))+tr('\n\nИсточники:\n')+attempts)
    def selected(event=None):
        render(app.store.setting('news:'+targets[choice.current()][1], {}))
    def refresh():
        _, key, symbol = targets[choice.current()]
        def done(value):
            if win.winfo_exists() and targets[choice.current()][1] == key:render(value)
        if app.news_runtime.refresh(key, symbol, done):status.set(tr('Загрузка…'))
        else:status.set(tr('Предыдущее обновление ещё выполняется'))
    def original():
        from tkinter.simpledialog import askinteger
        if not links:return
        n=askinteger(tr('Оригинал'),tr('Номер новости'),minvalue=1,maxvalue=len(links),parent=win)
        if n:webbrowser.open(links[n-1])
    choice.bind('<<ComboboxSelected>>', selected)
    ttk.Button(bar,text=tr('Обновить'),command=refresh).pack(side='right')
    ttk.Button(bar,text=tr('Оригинал'),command=original).pack(side='right')
    def listener(key,value):
        if win.winfo_exists() and targets[choice.current()][1]==key:render(value)
    app._news_listener=listener
    def close():
        if getattr(app,'_news_listener',None) is listener:app._news_listener=None
        win.destroy()
    win.protocol('WM_DELETE_WINDOW',close)
    def update_freshness():
        if win.winfo_exists():selected();win.after(60000,update_freshness)
    win.after(60000,update_freshness)
    selected()
