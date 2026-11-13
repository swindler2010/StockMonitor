"""Text-first Development Advisor, using the application's existing Dispatcher."""
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog

from development_advisor import collect, render, local_time
from features import text_area, set_text


def show(app):
    previous = getattr(app, '_development_advisor_window', None)
    if previous is not None and previous.winfo_exists():
        previous.lift()
        return
    win = tk.Toplevel(app.root)
    app._development_advisor_window = win
    win.title('Советы по развитию • Stock Monitor')
    win.geometry('1000x720')
    win.minsize(700, 480)
    bar = ttk.Frame(win, padding=10)
    bar.pack(fill='x')
    status = tk.StringVar(value='Ожидание чтения диагностики…')
    ttk.Label(win, textvariable=status, padding=10, wraplength=950).pack(fill='x')
    tabs = ttk.Notebook(win)
    tabs.pack(fill='both', expand=True)
    texts = {}
    for section, title in ((1, '1. Развитие программы'), (2, '2. Развитие торговли')):
        page = ttk.Frame(tabs)
        tabs.add(page, text=title)
        texts[section] = text_area(page)
    state = {'busy': False, 'text': None}

    def exists():
        return not getattr(app, 'closing', False) and bool(win.winfo_exists())

    def fail(_):
        state['busy'] = False
        if not exists():
            return
        state['text'] = None
        copy_button.configure(state='disabled')
        save_button.configure(state='disabled')
        status.set('Диагностика недоступна. Советы не сформированы; повторите обновление.')
        for text in texts.values():
            set_text(text, 'Нет нового подтверждённого снимка. Это не нулевое число проблем.')

    def done(report):
        state['busy'] = False
        if not exists():
            return
        state['text'] = render(report)
        for section, text in texts.items():
            set_text(text, render(report, section))
        copy_button.configure(state='normal')
        save_button.configure(state='normal')
        status.set(f'Снимок {local_time(report["collected_at"])} • советов: {len(report["advice"])} • '
                   f'чтение {report["read_seconds"]:.3f} с. Обновление раз в минуту, пока окно открыто.')

    def refresh():
        if not exists() or state['busy']:
            return
        state['busy'] = True
        status.set('Чтение сохранённой диагностики в фоновой очереди…')
        if app.job(lambda: collect(app.store.path), done, fail, key=('development-advice', id(win)), lane='misc') is False:
            fail(None)

    def periodic():
        if exists():
            refresh()
            win.after(60000, periodic)

    def copy():
        if state['text'] is not None:
            win.clipboard_clear()
            win.clipboard_append(state['text'])
            status.set('Текст скопирован для передачи Work. Ничего автоматически не отправлено.')

    def save():
        if state['text'] is None:
            return
        path = filedialog.asksaveasfilename(parent=win, title='Экспорт советов для Work',
                    defaultextension='.txt', initialfile='Советы по развитию СМ.txt',
                    initialdir=str(Path(app.store.directory).parent.parent), filetypes=[('Текст UTF-8', '*.txt')])
        if not path:
            return
        payload = state['text']

        def saved(_):
            if exists():
                status.set('Текст сохранён. Отправка Work выполняется пользователем.')

        def save_failed(_):
            if exists():
                status.set('Не удалось сохранить файл. Выберите доступное место или скопируйте текст.')

        if app.job(lambda: Path(path).write_text(payload, encoding='utf-8'), saved, save_failed,
                   key=('development-advice-export', id(win)), lane='misc') is False:
            save_failed(None)

    ttk.Button(bar, text='Обновить советы', command=refresh).pack(side='left', padx=(0, 8))
    copy_button = ttk.Button(bar, text='Копировать для Work', command=copy, state='disabled')
    copy_button.pack(side='left', padx=4)
    save_button = ttk.Button(bar, text='Сохранить текст…', command=save, state='disabled')
    save_button.pack(side='left', padx=4)
    ttk.Button(bar, text='Закрыть', command=win.destroy).pack(side='right')
    for text in texts.values():
        set_text(text, 'Локальные советы по диагностике. Внешняя ИИ-модель не вызывается.')
    refresh()
    win.after(60000, periodic)
