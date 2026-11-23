"""Main-screen presentation only; existing chart, workers and PAPER callbacks own behaviour.

The Amazon reference defines layout, never quotes, readiness or trading rules.
Only bounded cached presentation reads run on the existing Dispatcher; no
archive decoding, source download, analytical calculation or trading decision.
"""
import math
import hashlib
import re
from contextlib import closing
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from i18n import tr
from localized_widgets import CanonicalCombobox
from timeframes import LABELS

COLLECTIONS = {
    'technology': ('Технологии', re.compile(r'APPLE|MICROSOFT|ALPHABET|META|ORACLE|SALESFORCE|ADOBE|PALANTIR|NVIDIA|ADVANCED MICRO|BROADCOM|MICRON|INTEL|INFINEON|ASML|STMICRO|SUSS', re.I)),
    'semis': ('Полупроводники', re.compile(r'NVIDIA|ADVANCED MICRO|\bAMD\b|BROADCOM|MICRON|INTEL|INFINEON|ASML|STMICRO|\bSTM\b|SUSS', re.I)),
    'cloud': ('ИИ / Облако', re.compile(r'MICROSOFT|AMAZON|ALPHABET|META|ORACLE|SALESFORCE|PALANTIR|NVIDIA', re.I)),
    'auto': ('Авто / EV', re.compile(r'TESLA|BMW|BAYERISCHE MOT|MERCEDES|VOLKSWAGEN|GENERAL MOT', re.I)),
    'pharma': ('Фарма', re.compile(r'ABBVIE|BAYER\s|JOHNSON|MERCK|NOVO|NOVARTIS|ROCHE|SANOFI|PFIZER|ASTRAZENECA', re.I)),
}


def finite_float(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else float('nan')
    except (TypeError, ValueError, OverflowError):return float('nan')


def group_match(info, group):
    isin = info['isin']
    benchmark = info.get('kind') == 'benchmark'
    if group == 'index':return benchmark
    if benchmark:return False
    if group == 'all':return True
    if group == 'us':return isin.startswith('US')
    if group == 'de':return isin.startswith('DE')
    if group == 'eu':return isin[:2] in ('DE','FR','NL','CH','DK','GB','IE','FI','IT','BE','ES','SE','AT','NO','LU','PT')
    return bool(COLLECTIONS[group][1].search(info['name'] + ' ' + info.get('symbol', ''))) if group in COLLECTIONS else False


def theme(app, palette):
    from PIL import Image, ImageDraw, ImageTk
    style = ttk.Style(app.root)
    dark = palette['bg'] == '#0b1220'
    panel = '#0d1827' if dark else palette['panel']
    accent = '#00b9f2' if dark else palette['accent']
    for name in ('Desk.TFrame', 'Desk.TLabel'):
        style.configure(name, background=panel, foreground=palette['text'])
    style.configure('DeskCard.TFrame', background=panel, borderwidth=1,
                    relief='solid', bordercolor=palette['grid'])
    style.configure('DeskMuted.TLabel', background=panel, foreground=palette['muted'])
    style.configure('DeskTitle.TLabel', background=panel, foreground=palette['text'], font=('Segoe UI', 13, 'bold'))
    style.configure('DeskPrice.TLabel', background=panel, foreground=palette['text'], font=('Segoe UI', 20, 'bold'))
    style.configure('DeskSignal.TLabel', background=panel, foreground=palette['text'], font=('Segoe UI', 16, 'bold'))
    style.configure('DeskHeading.TLabel', foreground=accent, font=('Segoe UI', 10, 'bold'))
    style.configure('Desk.Treeview', rowheight=29, font=('Segoe UI', 9), borderwidth=0, relief='flat', background=panel, fieldbackground=panel)
    style.configure('DeskNews.Treeview', rowheight=23, font=('Segoe UI', 9), borderwidth=0, relief='flat', background=panel, fieldbackground=panel)
    style.configure('DeskGroup.TButton', anchor='w', padding=(5, 1), font=('Segoe UI', 9))
    style.configure('Desk.TEntry',fieldbackground=panel,foreground=palette['text'],insertcolor=palette['text'],bordercolor=palette['grid'],lightcolor=panel,darkcolor=panel,padding=4)
    style.configure('Desk.Vertical.TScrollbar',background=palette['grid'],troughcolor=panel,bordercolor=panel,lightcolor=panel,darkcolor=panel,arrowcolor=palette['muted'],width=9,arrowsize=9)
    style.map('Desk.Vertical.TScrollbar',background=[('active',accent)])
    style.layout('DeskMenu.TMenubutton', [('Menubutton.padding', {'sticky': 'nsew', 'children': [('Menubutton.label', {'sticky': 'nsew'})]})])
    style.configure('DeskMenu.TMenubutton', background=palette['bg'], foreground=palette['text'], padding=(8, 3), font=('Segoe UI', 9))
    style.map('DeskMenu.TMenubutton', background=[('active', panel)], foreground=[('active', accent)])
    for state, color in [('Good', '#25e772' if dark else '#15803d'), ('Bad', '#ff5866' if dark else '#b91c1c'), ('Pending', palette['muted'])]:
        style.configure(f'DeskSync{state}.TLabel', background=palette['bg'], foreground=color, font=('Segoe UI', 9))
        style.configure(f'DeskQuality{state}.Horizontal.TProgressbar', background=color, troughcolor=panel, borderwidth=0,bordercolor=panel,lightcolor=panel,darkcolor=panel)
    for name in ('Desk.Treeview.Heading', 'DeskNews.Treeview.Heading'):
        style.configure(name, background=panel, foreground=palette['muted'], relief='flat', borderwidth=0, bordercolor=palette['grid'], lightcolor=panel, darkcolor=panel, font=('Segoe UI', 9))
    for name in ('Desk.Treeview', 'DeskNews.Treeview'):
        style.configure(name,bordercolor=panel,lightcolor=panel,darkcolor=panel)
        style.map(name, background=[('selected', '#11355b' if dark else palette['accent'])], foreground=[('selected', '#ffffff')])
    style.configure('DeskSmall.TLabel', background=panel, foreground=palette['text'], font=('Segoe UI', 9))
    style.configure('DeskPositive.TLabel', background=panel, foreground='#25e772' if dark else '#15803d')
    style.configure('DeskNegative.TLabel', background=panel, foreground='#ff5866' if dark else '#b91c1c')
    style.configure('Desk.Horizontal.TProgressbar', background='#25e772' if dark else '#15803d', troughcolor=palette['bg'])
    style.configure('DeskActive.TButton', foreground=accent)
    # Nine-slice artwork preserves rounded corners at any panel/window size.
    generation = getattr(app.root, '_desk_theme_generation', 0) + 1
    app.root._desk_theme_generation = generation
    images = []
    for name, fill, outline in [('Card', panel, '#345069' if dark else palette['grid']),
                                ('Selected', '#00568f' if dark else palette['grid'], accent)]:
        image = Image.new('RGBA', (96, 96))
        ImageDraw.Draw(image).rounded_rectangle((2, 2, 93, 93), radius=28, fill=fill, outline=outline, width=4)
        photo = ImageTk.PhotoImage(image.resize((32, 32), Image.Resampling.LANCZOS), master=app.root)
        images.append(photo)
        element = f'SMDesk{name}{generation}'
        style.element_create(element, 'image', photo, border=10, sticky='nsew')
        if name == 'Card':
            style.layout('DeskCard.TFrame', [(element, {'sticky': 'nsew'})])
        else:
            style.layout('DeskActive.TButton', [(element, {'sticky': 'nsew', 'children': [
                ('Button.padding', {'sticky': 'nsew', 'children': [('Button.label', {'sticky': 'nsew'})]})]})])
            style.layout('DeskSelected.TFrame',[(element,{'sticky':'nsew'})])
    style.configure('DeskSelected.TLabel',background='#00568f' if dark else palette['grid'],foreground=palette['text'],font=('Segoe UI',9))
    app.root._desk_theme_images = images
    style.map('DeskActive.TButton', foreground=[('disabled', palette['muted']), ('!disabled', palette['text'])])
    progress = getattr(getattr(app, 'main_screen', None), 'progress', None)
    if isinstance(progress, SignalMeter):progress.paint()
    screen = getattr(app, 'main_screen', None)
    if screen is not None and hasattr(screen, 'tree'):screen.schedule_deltas()
    from ui_icons import photo
    for widget, name, size in getattr(app.root, '_desk_icon_widgets', []):
        if widget.winfo_exists():widget.configure(image=photo(app.root,name,size,color=palette['text']))
    app.root.after_idle(lambda: native_titlebar(app.root, palette))


def native_titlebar(root, palette):
    """Window-local dark caption; preserve native move/resize/minimize/close."""
    import sys
    if sys.platform != 'win32':return
    try:
        import ctypes
        from ctypes import wintypes
        user = ctypes.windll.user32
        user.GetParent.argtypes = [wintypes.HWND]
        user.GetParent.restype = wintypes.HWND
        window = user.GetParent(root.winfo_id()) or root.winfo_id()
        dark = ctypes.c_int(palette['bg'] != '#f8fafc')
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(window), 20, ctypes.byref(dark), ctypes.sizeof(dark))
        for attribute, key in ((35, 'bg'), (36, 'text')):
            value = palette[key].lstrip('#')
            color = ctypes.c_uint(int(value[0:2],16) | int(value[2:4],16)<<8 | int(value[4:6],16)<<16)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(window), attribute, ctypes.byref(color), ctypes.sizeof(color))
    except (AttributeError, OSError, tk.TclError):
        pass  # Older Windows keeps its native caption; no functional fallback.


def flow_widgets(widgets,width):
    """Reflow only changed cells; repeated Configure must not relayout Tk."""
    row=column=used=0
    for widget in widgets:
        requested=widget.winfo_reqwidth()+2
        if used and used+requested>width:
            row+=1;column=used=0
        current=widget.grid_info()
        if current.get('row')!=row or current.get('column')!=column:
            widget.grid(row=row,column=column,sticky='w',padx=(0,2),pady=1)
        column+=1;used+=requested


def decision_row(parent, decision, reason):
    """Reserve the longest status height; loading must not move other cards."""
    from tkinter import font
    status_font=ttk.Style(parent).lookup('DeskSignal.TLabel','font') or 'TkDefaultFont'
    row=ttk.Frame(parent,style='Desk.TFrame',height=3*font.Font(parent,font=status_font).metrics('linespace'))
    row.pack(fill='x',pady=5)
    row.pack_propagate(False)
    ttk.Label(row,textvariable=decision,style='DeskSignal.TLabel',wraplength=135).pack(side='left',anchor='w')
    ttk.Label(row,textvariable=reason,style='DeskMuted.TLabel',font=('Segoe UI',8),wraplength=140).pack(side='right',fill='x',expand=True,padx=(7,0))
    return row


class SignalMeter(tk.Canvas):
    """Segmented display of a prepared score; never computes a trading signal."""
    def __init__(self, parent, **kwargs):
        self.value = 0
        super().__init__(parent, height=19, highlightthickness=0, **kwargs)
        self.bind('<Configure>', lambda _: self.paint())

    def __setitem__(self, key, value):
        if key == 'value':
            self.value = float(value)
            self.paint()
        else:super().__setitem__(key, value)

    def __getitem__(self, key):
        return self.value if key == 'value' else super().__getitem__(key)

    def paint(self):
        style = ttk.Style(self)
        bg = style.lookup('Desk.TFrame', 'background') or '#0d1827'
        self.configure(background=bg)
        self.delete('meter')
        width = max(20, self.winfo_width()) / 16
        for i in range(16):
            fill = '#25e772' if i < int(self.value * 16 / 100) else '#263446'
            self.create_rectangle(i * width + 1, 2, (i + 1) * width - 2, 17, fill=fill, outline='', tags='meter')


def clone_menu(source, parent):
    """Keep the original registered callbacks in a properly owned Tk menu."""
    target = tk.Menu(parent, tearoff=False, background=source.cget('background'),
                     foreground=source.cget('foreground'), activebackground=source.cget('activebackground'),
                     activeforeground=source.cget('activeforeground'), font=source.cget('font'))
    end = source.index('end')
    for index in range(end + 1 if end is not None else 0):
        kind = source.type(index)
        if kind == 'tearoff':continue
        options = {key: value[-1] for key, value in source.entryconfigure(index).items()}
        if kind == 'cascade':
            options['menu'] = clone_menu(source.nametowidget(source.entrycget(index, 'menu')), target)
        target.add(kind, **options)
    return target


def right_axes(axes):
    for axis in axes:
        axis.yaxis.tick_right()
        axis.yaxis.set_label_position('right')


class Screen:
    def __init__(self, app):
        self.app = app
        self.query = tk.StringVar(master=app.root)
        self.group = tk.StringVar(master=app.root, value='all')
        self.context = tk.StringVar(master=app.root, value='—')
        self.strength = tk.StringVar(master=app.root, value=tr('Нет актуального результата'))
        self.tf_buttons = {}
        self._signature = None
        self._selection_pending = None
        self._news_key = None
        self._news_translation_timer = None
        self._logo_signature = None
        self._shown_isin = None
        self.logo_images = {}
        self.header_images = {}
        self.prices = {}
        self.sync_text = tk.StringVar(app.root, tr('Синхронизация: ожидание'))
        self.clock_text = tk.StringVar(app.root)
        self.portfolio_text = tk.StringVar(app.root, tr('Портфель: — EUR'))
        self.daily_text = tk.StringVar(app.root, tr('Изм. дня: —'))
        self._footer_busy = False
        self._delta_timer = None
        self.delta_labels = {}
        self.favorite_labels = {}
        self.favorites = set()
        self._favorites_loaded = self._favorite_busy = False
        self._favorite_timer = None
        self._favorite_seq = 0
        self._clock_timer = self._account_timer = None
        self._closed = False
        app.root.bind('<Destroy>', lambda event:self.close() if event.widget is app.root else None, add='+')

    def sync_watchlist(self):
        app = self.app
        infos = app.stock_order.ordered() if hasattr(app, 'stock_order') else app.watchlist
        query = self.query.get().casefold().strip()
        group = self.group.get()
        signature = tuple((x['isin'], x['name'], x.get('symbol', ''), x.get('kind')) for x in infos), query, group
        if signature != self._signature:
            self._signature = signature
            keep = {x['isin'] for x in infos}
            for iid in self.tree.get_children(''):
                if iid not in keep:
                    self.tree.delete(iid)
            visible = []
            for info in infos:
                iid = info['isin']
                if not self.tree.exists(iid):
                    self.tree.insert('', 'end', iid=iid, text=info.get('symbol') or info['name'], values=self.row_values(iid))
                self.tree.item(iid, text=info.get('symbol') or info['name'], values=self.row_values(iid))
                matching_group = group_match(info, group)
                if not matching_group or query and query not in ' '.join(str(v) for v in (iid, info['name'], info.get('symbol', ''))).casefold():
                    self.tree.detach(iid)
                else:
                    self.tree.move(iid, '', len(visible))
                    visible.append(iid)
            total=sum(group_match(x,'all') for x in infos)
            self.count.configure(text=str(total))
            if 'all' in getattr(self,'group_buttons',{}):self.group_buttons['all'].configure(text=f'{tr("Мои акции")}      {total}')
            for key, variable in getattr(self, 'group_counts', {}).items():
                variable.set(str(sum(group_match(x,key) for x in infos)))
        selected = next((x for x in infos if x['name'] == app.symbol.get()), None)
        if selected:
            iid = selected['isin']
            if self._shown_isin != iid:
                self._shown_isin = iid
                app.instrument.set(selected['name'])
                for variable in app.metrics.values():variable.set('—')
                if hasattr(self,'delta'):
                    self.delta.set('—');self.listing.set('—');self.source_summary.set('—');self.quality.set(tr('Загрузка'))
            if iid in self.tree.get_children('') and self.tree.selection() != (iid,):
                self.tree.selection_set(iid)
                self.tree.see(iid)
            self.context.set(' · '.join(str(v) for v in (selected.get('symbol'), selected.get('isin'), selected.get('currency')) if v))
            if iid != self._news_key:
                if self._news_translation_timer is not None:
                    self.app.root.after_cancel(self._news_translation_timer);self._news_translation_timer=None
                self._news_key = iid
                self.news_tree.delete(*self.news_tree.get_children())
                self.news_text.set(tr('Новости: чтение сохранённой ленты…'))
                from news_translation import cached_snapshot
                from i18n import language
                target_language=language()
                app.job(lambda: cached_snapshot(app.store, iid, target_language),
                        lambda value, key=iid: self.show_news(key, value),
                        lambda _, key=iid: self.show_news(key, {}), lane='misc', key='main-screen-news')
            self.asset_logo.configure(image=self.header_images.get(iid, ''), text='' if iid in self.header_images else '◈')
        logo_signature = tuple((x['isin'], x.get('profile_symbol') or x.get('history_symbol') or x.get('symbol') or '') for x in infos)
        if logo_signature != self._logo_signature:
            self._logo_signature = logo_signature
            app.job(lambda: cached_logos(logo_signature), self.show_logos, lambda _: None,
                    lane='misc', key='main-screen-artwork')
            if hasattr(app.store, 'path'):
                path = app.store.path
                app.job(lambda: cached_quotes(path, tuple(x[0] for x in logo_signature)), self.show_quotes,
                        lambda _: None, lane='misc', key='main-screen-quotes')
        for tf, button in self.tf_buttons.items():
            button.configure(style='DeskActive.TButton' if tf == app.timeframe.get() else 'TButton')
        for key, button in getattr(self, 'group_buttons', {}).items():
            button.configure(style='DeskActive.TButton' if key == self.group.get() else 'DeskGroup.TButton')
        self.schedule_deltas()
        self.favorite_header()

    def show_logos(self, values):
        if getattr(self.app, 'closing', False):
            return
        from PIL import ImageTk, Image
        current = {x['isin'] for x in self.app.watchlist}
        for isin, image in values.items():
            if isin in current and self.tree.exists(isin):
                self.logo_images[isin] = ImageTk.PhotoImage(image, master=self.app.root)
                self.header_images[isin] = ImageTk.PhotoImage(image.resize((40,40), Image.Resampling.LANCZOS), master=self.app.root)
                self.tree.item(isin, image=self.logo_images[isin])
        self.selection_changed()

    def show_quotes(self, values):
        if getattr(self.app, 'closing', False):return
        current = {x['isin'] for x in self.app.watchlist}
        for isin, quote in values.items():
            if isin in current and self.tree.exists(isin):
                self.prices[isin] = quote
                self.tree.item(isin, values=self.row_values(isin))
        self.schedule_deltas()

    def row_values(self, isin):
        return (*self.prices.get(isin, ('—','—')), '★' if isin in self.favorites else '☆')

    def load_favorites(self):
        if self._closed or getattr(self.app, 'closing', False):return
        def loaded(value):
            if self._closed:return
            self.favorites = {v for v in value if isinstance(v,str)} if isinstance(value,list) else set()
            self._favorites_loaded = True
            for info in self.app.watchlist:
                if self.tree.exists(info['isin']):self.tree.item(info['isin'],values=self.row_values(info['isin']))
            self.favorite_header();self.schedule_deltas()
        if self.app.job(lambda:self.app.store.setting('ui_favorites',[]), loaded, lambda _:None,
                        lane='misc',key='main-screen-favorite-read') is False:
            self._favorite_timer=self.app.root.after(1000,self.load_favorites)

    def favorite_header(self):
        button = getattr(self,'favorite_button',None)
        if button is None:return
        from ui_icons import photo
        info=next((x for x in self.app.watchlist if x['name']==self.app.symbol.get()),None)
        button.configure(image=photo(self.app.root,'star_filled' if info and info['isin'] in self.favorites else 'star'),
                         state='normal' if self._favorites_loaded else 'disabled')

    def toggle_favorite(self, isin=None):
        if not self._favorites_loaded or self._closed:return
        if isin is None:
            info=next((x for x in self.app.watchlist if x['name']==self.app.symbol.get()),None)
            if not info:return
            isin=info['isin']
        if isin in self.favorites:self.favorites.remove(isin)
        else:self.favorites.add(isin)
        self._favorite_seq+=1
        if self.tree.exists(isin):self.tree.item(isin,values=self.row_values(isin))
        self.favorite_header();self.schedule_deltas();self.save_favorites()

    def save_favorites(self):
        if self._closed or self._favorite_busy or getattr(self.app,'closing',False):return
        captured=sorted(self.favorites);self._favorite_busy=True
        def done(_):
            self._favorite_busy=False
            if not self._closed and captured!=sorted(self.favorites):self.save_favorites()
        def failed(_):
            self._favorite_busy=False
            if not self._closed:self.app.status.set(tr('Избранное не сохранено'))
        if self.app.job(lambda:self.app.store.set_setting('ui_favorites',captured),done,failed,lane='misc',key='main-favorite:'+str(self._favorite_seq)) is False:
            self._favorite_busy=False
            self._favorite_timer=self.app.root.after(1000,self.save_favorites)

    def schedule_deltas(self, *_):
        if self._delta_timer is None and not getattr(self.app, 'closing', False):
            self._delta_timer = self.app.root.after_idle(self.paint_deltas)

    def paint_deltas(self):
        self._delta_timer = None
        if not self.tree.winfo_exists():return
        style = ttk.Style(self.tree)
        normal = style.lookup('Desk.Treeview', 'fieldbackground') or '#0d1827'
        selected = style.lookup('Desk.Treeview', 'background', ('selected',)) or '#11355b'
        visible = set()
        for isin in self.tree.get_children():
            bounds = self.tree.bbox(isin, 'change')
            if not bounds:continue
            visible.add(isin)
            values = self.tree.item(isin, 'values')
            value = str(values[1]) if len(values)>1 else '—'
            label = self.delta_labels.get(isin)
            if label is None:
                label = tk.Label(self.tree, borderwidth=0, anchor='e', padx=3, font=('Segoe UI', 9))
                self.delta_labels[isin] = label
                def choose(_, iid=isin):
                    self.tree.focus_set();self.tree.selection_set(iid);self.tree.focus(iid)
                label.bind('<Button-1>', choose)
                label.bind('<MouseWheel>', lambda event:self.tree.yview_scroll(-int(event.delta/120), 'units'))
            light = normal.lower() == '#e2e8f0'
            color = ('#b91c1c' if light else '#ff5866') if value.startswith('-') else ('#15803d' if light else '#24d876') if value.startswith('+') else '#94a3b8'
            label.configure(text=value+('%' if value not in ('—','') and not value.endswith('%') else ''),
                            foreground=color, background=selected if isin in self.tree.selection() else normal)
            x,y,w,h = bounds
            label.place(x=x,y=y,width=max(1,w-1),height=h-1)
            favorite_bounds=self.tree.bbox(isin,'favorite')
            if favorite_bounds:
                star=self.favorite_labels.get(isin)
                if star is None:
                    star=tk.Label(self.tree,borderwidth=0,anchor='center',font=('Segoe UI',11))
                    star.bind('<Button-1>',lambda _,iid=isin:self.toggle_favorite(iid))
                    star.bind('<MouseWheel>',lambda event:self.tree.yview_scroll(-int(event.delta/120),'units'))
                    self.favorite_labels[isin]=star
                star.configure(text='★' if isin in self.favorites else '☆',foreground='#ffd020',background=selected if isin in self.tree.selection() else normal)
                x,y,w,h=favorite_bounds;star.place(x=x,y=y,width=w-1,height=h-1)
        for isin,label in self.delta_labels.items():
            if isin not in visible:label.place_forget()
        for isin,label in self.favorite_labels.items():
            if isin not in visible:label.place_forget()

    def show_news(self, key, value):
        if key != self._news_key or getattr(self.app, 'closing', False):
            return
        from news_translation import project
        from i18n import language
        articles = project(value.get('articles') or [],value.get('_translation_cache',{}),language())
        if self._news_translation_timer is None and any(r['translation']['status']=='PENDING' for r in articles[:3]):
            def translate_visible():
                self._news_translation_timer=None
                if self._closed or key!=self._news_key:return
                runtime=getattr(self.app,'news_runtime',None)
                if runtime and not runtime.translate_news(key,(value.get('articles') or [])[:3],language(),lambda packet:self.show_news(key,packet)):
                    self._news_translation_timer=self.app.root.after(5000,translate_visible)
            self._news_translation_timer=self.app.root.after(1500,translate_visible)
        lines = [tr('Сохранённые новости') + ' · ' + str(value.get('attempted_at') or tr('время не подтверждено'))]
        for article in articles[:2]:
            lines.append(str(article.get('provider') or '—') + '  ·  ' + str(article.get('title') or '—')[:210])
        self.news_text.set('\n'.join(lines) if articles else tr('Сохранённых новостей нет. Откройте «Новости» для загрузки.'))
        if hasattr(self, 'news_tree'):
            self.news_text.set(lines[0]+f' · {len(articles)}' if articles else tr('Сохранённых новостей нет. Откройте «Новости» для загрузки.'))
            self.news_tree.delete(*self.news_tree.get_children())
            for article in articles[:5]:
                self.news_tree.insert('', 'end', values=(str(article.get('published') or '—')[:16], article.get('provider') or '—', ('⌛ ' if article.get('translation',{}).get('status') not in ('TRANSLATED','ALREADY_TARGET') else '')+(article.get('title') or '—')))

    def selection_changed(self, *_):
        # Coalesce symbol/TF writes. No source or account work on the UI thread.
        if self._selection_pending is None:
            self._selection_pending = self.app.root.after_idle(self._apply_selection)

    def _apply_selection(self):
        self._selection_pending = None
        self.sync_watchlist()
        if hasattr(self,'pages'):self.pages.context_changed()

    def choose_stock(self, _=None):
        selected = self.tree.selection()
        if not selected:
            return
        info = next((x for x in self.app.watchlist if x['isin'] == selected[0]), None)
        if info and info['name'] != self.app.symbol.get():
            self.app.symbol.set(info['name'])
            self.app.refresh_chart()

    def choose_tf(self, tf):
        if tf != self.app.timeframe.get():
            self.app.timeframe.set(tf)
            self.app.refresh_chart()

    def bind_engine(self, engine):
        if hasattr(self,'pages'):self.pages.engine_changed()
        value = engine.get('confidence') if isinstance(engine, dict) else None
        valid = (isinstance(engine, dict) and engine.get('status') in ('READY', 'DATA BLOCKED')
                 and engine.get('signal') in ('BUY', 'SELL', 'WAIT', 'HOLD'))
        if valid and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100:
            self.strength.set(f'{value:g}%')
            self.progress['value'] = value
        else:
            self.strength.set('—')
            self.progress['value'] = 0
            self.sync_text.set(tr('Синхронизация: обновление'))
            if hasattr(self, 'sync_label'):self.sync_label.configure(style='DeskSyncPending.TLabel')
            if hasattr(self, 'quality'):
                self.quality.set(tr('Ожидается актуальный расчёт'))
                self.quality_bar['value'] = 0
        contexts = (engine.get('timeframe_context') or {}) if valid else {}
        contexts = contexts if isinstance(contexts, dict) else {}
        for tf, variable in self.context_vars.items():
            context = contexts.get(tf) or {}
            context = context if isinstance(context, dict) else {}
            facts = context.get('facts') or {}
            facts = facts if isinstance(facts, dict) else {}
            regime=facts.get('regime','—')
            variable.set(f'{tf}\n'+{'BULLISH':'Рост','BEARISH':'Снижение','TRANSITION':'Переход','RANGE':'Боковик'}.get(regime,regime))
            if tf in getattr(self,'context_frames',{}):
                cell,label=self.context_frames[tf]
                selected=tf==self.app.timeframe.get()
                cell.configure(style='DeskSelected.TFrame' if selected else 'DeskCard.TFrame')
                label.configure(style='DeskSelected.TLabel' if selected else 'DeskSmall.TLabel')
        if hasattr(self, 'condition_vars'):
            selected = contexts.get(self.app.timeframe.get()) or {}
            facts = selected.get('facts') or {} if isinstance(selected, dict) else {}
            facts = facts if isinstance(facts,dict) else {}
            def number(key):
                value = facts.get(key)
                return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None
            fast, slow = number('ema10'), number('ema30')
            histogram, rsi, rv = number('histogram'), number('rsi'), number('relative_volume')
            values = {'trend': '—' if fast is None or slow is None else '✓' if fast > slow else '✕',
                      'volume': '—' if rv is None else f'{rv:.2f}×',
                      'macd': '—' if histogram is None else '✓' if histogram > 0 else '✕',
                      'rsi': '—' if rsi is None else f'{rsi:.1f}'}
            for key, variable in self.condition_vars.items():
                variable.set(values.get(key, '—'))
            self.signal_caption.set(tr('Сигнал') + f' ({self.app.timeframe.get()}) · '+tr('закрытая'))
            if hasattr(self,'signal_reason'):
                reason=str(engine.get('reason') or '') if valid else tr('Ожидается расчёт')
                self.signal_reason.set(reason[:45]+('…' if len(reason)>45 else ''))

    def bind_chart(self, prepared):
        """Project the already accepted frame's bounded tail and provenance."""
        frame = prepared.get('frame')
        if frame is None or frame.empty:
            self.delta.set('—')
            self.quality.set(tr('Нет подтверждённого ряда'))
            return
        meta = frame.attrs
        self.sync_text.set(tr('Синхронизация: ОК'))
        self.sync_label.configure(style='DeskSyncGood.TLabel')
        self.listing.set(' · '.join(str(meta.get(k) or '—') for k in ('venue', 'currency', 'source')))
        dq = meta.get('data_quality') or {}
        self.quality.set(str(dq.get('state', '—')) if isinstance(dq, dict) else str(dq))
        state = self.quality.get()
        self.quality_bar.configure(style='DeskQualityGood.Horizontal.TProgressbar' if state == 'READY'
                                   else 'DeskQualityBad.Horizontal.TProgressbar' if state == 'INVALID'
                                   else 'DeskQualityPending.Horizontal.TProgressbar')
        self.quality_bar['value'] = 100 if state != '—' else 0
        self.source_summary.set(str(meta.get('source') or '—') + ' · ' + str(meta.get('venue') or '—'))
        if hasattr(self, 'header_volume'):
            volume = finite_float(frame.volume.iloc[-1])
            from chart_modes import compact_number
            self.header_volume.set(tr('Объём') + f' ({self.app.timeframe.get()}): ' + compact_number(volume) if math.isfinite(volume) else tr('Объём: —'))
        if hasattr(self, 'source_rows'):
            timestamp = frame.index[-1].strftime('%d.%m %H:%M')
            used = {'GETTEX': meta.get('venue') == 'GETTEX', 'Xetra': meta.get('venue') in ('GER','XETRA','XETR'),
                    'LSEG / Refinitiv': meta.get('source') == 'gettex_chart', 'financial.com': meta.get('source') == 'gettex_chart'}
            for name, (variable, label) in self.source_rows.items():
                variable.set(('✓ ' + timestamp) if used.get(name) else '—')
                label.configure(style='DeskPositive.TLabel' if used.get(name) else 'DeskMuted.TLabel')
        close = finite_float(frame.close.iloc[-1])
        delta = '—'
        if len(frame) > 1:
            previous = finite_float(frame.close.iloc[-2])
            if math.isfinite(close) and math.isfinite(previous) and previous > 0:
                change = close - previous
                delta = f'{change:+.2f} ({change / previous * 100:+.2f}%) · {self.app.timeframe.get()}'
                self.delta_label.configure(style='DeskPositive.TLabel' if change >= 0 else 'DeskNegative.TLabel')
        self.delta.set(delta)
        selected = next((x['isin'] for x in self.app.watchlist if x['name'] == self.app.symbol.get()), None)
        if selected and math.isfinite(close):
            self.prices[selected] = (f'{close:.2f}', delta.split('(')[-1].split(')')[0] if '(' in delta else '—')
            if self.tree.exists(selected):
                self.tree.item(selected, values=self.row_values(selected))
                self.schedule_deltas()

    def fullscreen(self, active):
        # Chart itself, its artists and viewport remain the same objects.
        if active:
            if hasattr(self,'pages'):self.pages.select('chart')
            self.sidebar.pack_forget()
            self.asset.pack_forget()
            self.bottom_shared.pack_forget()
            self.shell.pack(fill='both',expand=True)
        else:
            self.sidebar.pack(side='left', fill='y', before=self.right_workspace, padx=(0, 7))
            self.asset.pack(fill='x', before=self.app.canvas.get_tk_widget(), pady=(0, 4))
            self.bottom_shared.pack(side='bottom',fill='x',before=self.app.chart_body,pady=(4,0))
            from themes import THEMES
            palette=THEMES.get(self.app.store.setting('theme','Тёмная'),THEMES['Тёмная'])
            self.app.root.after_idle(lambda:native_titlebar(self.app.root,palette))

    def clock_tick(self):
        if self._closed or getattr(self.app, 'closing', False):return
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.clock_text.set(datetime.now(ZoneInfo('Europe/Berlin')).strftime('%d.%m.%Y %H:%M:%S'))
        self._clock_timer = self.app.root.after(1000, self.clock_tick)

    def refresh_account(self):
        if self._closed or getattr(self.app, 'closing', False) or not hasattr(self.app.store, 'path'):return
        if not self._footer_busy:
            from paper_account import status
            self._footer_busy = True
            def done(value):
                self._footer_busy = False
                if not self._closed and not getattr(self.app, 'closing', False):self.bind_account(value)
            def failed(_):
                self._footer_busy = False
                self.portfolio_text.set(tr('Портфель: — EUR'))
            if self.app.job(lambda:status(self.app.store.path), done, failed, lane='read', key='main-screen-account') is False:
                self._footer_busy = False
        self._account_timer = self.app.root.after(60000, self.refresh_account)

    def close(self):
        self._closed = True
        if hasattr(self,'pages'):self.pages.close()
        for timer in (self._clock_timer, self._account_timer, self._delta_timer, self._selection_pending, self._favorite_timer, self._news_translation_timer):
            if timer is not None:
                try:self.app.root.after_cancel(timer)
                except tk.TclError:pass

    def bind_account(self, state):
        equity = state.get('equity')
        self.portfolio_text.set(tr('Портфель: ') + (str(equity) if equity is not None else '—') + ' EUR')
        # A lifetime realized return is never relabelled as a daily return.
        self.daily_text.set(tr('Изм. дня: ') + str(state.get('daily_pnl') if state.get('daily_pnl') is not None else '—'))

    def export_chart(self):
        from tkinter import filedialog
        from PIL import Image
        path = filedialog.asksaveasfilename(parent=self.app.root, title=tr('Сохранить график'), defaultextension='.png', filetypes=[('PNG','*.png')])
        if not path:return
        size = self.app.canvas.get_width_height()
        image = Image.frombytes('RGBA', size, bytes(self.app.canvas.buffer_rgba()))
        self.app.job(lambda:image.save(path, format='PNG'), lambda _:self.app.status.set(tr('График сохранён')),
                     lambda _:self.app.status.set(tr('Не удалось сохранить график')), lane='misc', key='chart-export')

    def toggle_levels(self):
        from indicator_controls import snapshot, persist
        import sqlite3
        desired=bool(self.levels_visible.get())
        try:current=snapshot(self.app)
        except (sqlite3.Error,ValueError):
            self.levels_visible.set(not desired);self.app.status.set(tr('Настройка уровней временно недоступна'));return
        prior=bool(current['settings']['Уровни анализа'])
        if current['unsaved_local_changes'] or current['write_in_progress']:
            self.levels_visible.set(prior)
            self.app.status.set(tr('Сначала завершите сохранение настроек индикаторов'))
            return
        self.app._indicator_write_busy=True
        self.levels_button.configure(state='disabled')
        def done(result):
            self.app._indicator_write_busy=False
            if self._closed:return
            self.levels_button.configure(state='normal')
            if result.get('saved'):self.app.refresh_chart()
            else:
                self.levels_visible.set(prior);self.app.status.set(tr('Настройка уровней не сохранена'))
        def failed(_):done({'saved':False})
        def work():
            return persist(self.app.store.path,current['revision'],{'Уровни анализа':desired},lambda:not getattr(self.app,'closing',False))
        if self.app.job(work,done,failed,lane='read',key='indicator-settings-write') is False:failed(None)


def cached_logos(identities):
    """Read existing small artwork in a Dispatcher job; never fetch a provider."""
    from PIL import Image
    from paths import DATA_DIR
    result = {}
    for isin, symbol in identities:
        path = DATA_DIR / 'logos' / (hashlib.sha256(symbol.encode()).hexdigest() + '.png')
        try:
            if path.stat().st_size > 1024 * 1024:
                continue
            with Image.open(path) as image:
                if image.width * image.height > 4_000_000:
                    continue
                image = image.convert('RGBA')
                image.thumbnail((24, 24))
                result[isin] = image.copy()
        except (OSError, ValueError):
            continue
    return result


def cached_quotes(path, isins):
    """Indexed read-only last bars for display, never execution quotes.

    Exact GETTEX identity comes from the existing source receipt. No history
    decode/download, source merge, DQ inference or readiness grant.
    """
    import json
    import sqlite3
    from pathlib import Path
    result = {}
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        for isin in isins:
            row = db.execute('SELECT value FROM settings WHERE key=?', ('gettex_chart:' + isin + ':15m',)).fetchone()
            try:saved = json.loads(row[0]) if row else {}
            except (ValueError, TypeError):continue
            if not isinstance(saved, dict):continue
            identity = saved.get('identity') or {}
            if not isinstance(identity, dict):continue
            if any(identity.get(k) != v for k, v in [('source', 'gettex_chart'), ('venue', 'GETTEX'), ('currency', 'EUR')]):continue
            if not isinstance(identity.get('symbol'), str):continue
            rows = db.execute('SELECT close FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? ORDER BY ts DESC LIMIT 2',
                (isin, identity['source'], identity['venue'], identity['currency'], identity['symbol'], '15m')).fetchall()
            if not rows or not isinstance(rows[0][0], (int, float)) or not math.isfinite(rows[0][0]) or rows[0][0] <= 0:continue
            last = rows[0][0]
            previous = rows[1][0] if len(rows) > 1 else None
            change = f'{(last / previous - 1) * 100:+.2f}' if isinstance(previous, (int, float)) and math.isfinite(previous) and previous > 0 else '—'
            result[isin] = (f'{last:.2f}', change)
    return result


def build(app, periods, bg, text):
    from matplotlib.figure import Figure
    from smooth_canvas import FigureCanvasTkAgg
    from chart_modes import MODES
    from features import set_text
    from themes import THEMES
    from ui_icons import photo, bind as bind_icon

    screen = Screen(app)
    app.main_screen = screen
    app.root.iconphoto(False, photo(app.root, 'globe', 32))
    theme(app, THEMES.get(app.store.setting('theme', 'Тёмная'), THEMES['Тёмная']))
    app.root.minsize(1000, 680)
    outer = ttk.Frame(app.root, padding=(5, 3))
    outer.pack(fill='both', expand=True)
    app.chart_outer = outer

    # The reference's global header remains thin. The asset header and chart
    # controls belong to the centre column, not above the entire application.
    global_bar = ttk.Frame(outer)
    global_bar.pack(fill='x', pady=(0, 3))
    hamburger = ttk.Button(global_bar, command=app.show_watchlist, width=3)
    bind_icon(hamburger, app.root, 'menu')
    hamburger.pack(side='left', padx=(0, 4))
    menu = app._main_menu
    menu_buttons = []
    for index in range((menu.index('end') or 0) + 1):
        if menu.type(index) != 'cascade':continue
        button = ttk.Menubutton(global_bar, text=menu.entrycget(index, 'label'), style='DeskMenu.TMenubutton', takefocus=True)
        submenu = clone_menu(app.root.nametowidget(menu.entrycget(index, 'menu')), button)
        button.configure(menu=submenu)
        button.pack(side='left')
        menu_buttons.append(button)
    screen.menu_buttons = menu_buttons
    if menu_buttons:app.root.bind('<F10>', lambda _: menu_buttons[0].focus_set(), add='+')
    ttk.Label(global_bar, textvariable=screen.clock_text, style='Muted.TLabel', font=('Segoe UI', 9)).pack(side='right', padx=(10, 5))
    screen.sync_label = ttk.Label(global_bar, textvariable=screen.sync_text, style='DeskSyncPending.TLabel')
    screen.sync_label.pack(side='right', padx=10)
    app.tooltip(screen.sync_label, 'Согласованность загруженного графика. Свежесть цены исполнения проверяется отдельно.')
    app.cp_label = ttk.Label(global_bar, text=tr('● CandlePilot — UNKNOWN'), foreground='#ef4444', font=('Segoe UI', 9))
    app.cp_label.pack(side='right', padx=5)
    app.cp_label.bind('<Button-1>', lambda _: __import__('candlepilot_health').show(app))
    app.tooltip(app.cp_label, 'CandlePilot Health — нажмите для диагностики')
    app.root.after(500, app._update_candlepilot)
    screen.clock_tick()

    footer = ttk.Frame(outer)
    footer.pack(side='bottom', fill='x', pady=(3, 0))
    ttk.Label(footer, textvariable=screen.daily_text, style='Muted.TLabel', font=('Segoe UI', 9)).pack(side='right', padx=8)
    ttk.Label(footer, textvariable=screen.portfolio_text, style='Muted.TLabel', font=('Segoe UI', 9)).pack(side='right', padx=10)
    ttk.Button(footer, text=tr('Связь'), command=app.show_ai).pack(side='left', padx=(0, 8))
    app.status_label = ttk.Label(footer, textvariable=app.status, style='Muted.TLabel', wraplength=850, font=('Segoe UI', 9))
    app.status_label.pack(side='left', fill='x', expand=True)
    screen.shell = ttk.Frame(outer)
    screen.shell.pack(fill='both', expand=True)
    screen.right_workspace = ttk.Frame(screen.shell)
    screen.right_workspace.pack(side='left',fill='both',expand=True)
    body = ttk.Panedwindow(screen.right_workspace, orient='horizontal')
    body.pack(fill='both', expand=True)
    chart = ttk.Frame(body)
    side = ttk.Frame(body, width=290)
    body.add(chart, weight=5)
    body.add(side, weight=1)
    app.chart_body, app.chart_side = body, side

    screen.sidebar = ttk.Frame(screen.shell, width=220, padding=(7, 4), style='Desk.TFrame')
    screen.sidebar.pack(side='left', fill='y', before=screen.right_workspace,padx=(0, 7))
    screen.sidebar.pack_propagate(False)
    screen.count = ttk.Label(screen.sidebar, style='DeskTitle.TLabel')
    screen.group_buttons = {}
    screen.group_counts = {}
    for key, label, artwork in [('all', 'Мои акции', 'star'), ('us', 'США', 'us'), ('de', 'Германия', 'de'), ('eu', 'Европа', 'eu'), ('index', 'Индексы', 'chart')]:
        def group_selected(value=key):
            screen.group.set(value)
            screen.sync_watchlist()
        button = ttk.Button(screen.sidebar, text=tr(label), image=photo(app.root,artwork,18), compound='left', command=group_selected, style='DeskGroup.TButton')
        bind_icon(button,app.root,artwork)
        button.pack(fill='x', pady=1)
        screen.group_buttons[key] = button
    ttk.Label(screen.sidebar, text=tr('▾  Секторы'), style='Desk.TLabel', font=('Segoe UI', 10, 'bold')).pack(anchor='w', pady=(2, 5))
    for key, (label, _) in COLLECTIONS.items():
        row = ttk.Frame(screen.sidebar, style='Desk.TFrame')
        row.pack(fill='x', pady=1)
        count = tk.StringVar(app.root, '—')
        screen.group_counts[key] = count
        ttk.Label(row, textvariable=count, style='DeskMuted.TLabel', width=3, anchor='center').pack(side='right')
        def collection_selected(value=key):
            screen.group.set(value);screen.sync_watchlist()
        button = ttk.Button(row, text='▹  '+tr(label), command=collection_selected, style='DeskGroup.TButton')
        button.pack(side='left', fill='x', expand=True)
        screen.group_buttons[key] = button
        app.tooltip(button, 'Быстрая группа известных компаний. Только фильтр списка; торговля и очередь CandlePilot не меняются.')
    ttk.Separator(screen.sidebar).pack(fill='x', pady=4)
    search_row=ttk.Frame(screen.sidebar,style='Desk.TFrame')
    search_row.pack(fill='x',pady=(4,6))
    ttk.Label(search_row,text='⌕',style='DeskMuted.TLabel').pack(side='left',padx=(0,3))
    search = ttk.Entry(search_row, textvariable=screen.query,style='Desk.TEntry')
    search.pack(fill='x')
    placeholder=ttk.Label(search,text=tr('Поиск акции…'),style='DeskMuted.TLabel',cursor='xterm',font=('Segoe UI',9))
    def placeholder_state(*_):
        if screen.query.get() or app.root.focus_get()==search:placeholder.place_forget()
        else:placeholder.place(x=4,rely=.5,anchor='w')
    placeholder.bind('<Button-1>',lambda _:(search.focus_set(),placeholder.place_forget()))
    search.bind('<FocusIn>',placeholder_state,add='+');search.bind('<FocusOut>',placeholder_state,add='+')
    screen.query.trace_add('write',placeholder_state)
    placeholder_state()
    app.tooltip(search, 'Поиск по названию, тикеру или ISIN')
    listing = ttk.Frame(screen.sidebar)
    listing.pack(fill='both', expand=True)
    screen.tree = ttk.Treeview(listing, columns=('price', 'change','favorite'), show='tree headings', selectmode='browse', style='Desk.Treeview')
    for key, label, width in [('#0', 'Акция', 82), ('price', 'Цена', 56), ('change', 'Изм%', 50), ('favorite','',20)]:
        screen.tree.heading(key, text=tr(label))
        screen.tree.column(key, width=width, minwidth=18 if key=='favorite' else 35, stretch=key == '#0', anchor='w' if key == '#0' else 'e')
    scroll = ttk.Scrollbar(listing, command=screen.tree.yview,style='Desk.Vertical.TScrollbar')
    def list_scrolled(first, last):
        scroll.set(first, last)
        screen.schedule_deltas()
    screen.tree.configure(yscrollcommand=list_scrolled)
    scroll.pack(side='right', fill='y')
    screen.tree.pack(fill='both', expand=True)
    screen.tree.bind('<<TreeviewSelect>>', screen.choose_stock)
    screen.tree.bind('<<TreeviewSelect>>', screen.schedule_deltas, add='+')
    screen.tree.bind('<Configure>', screen.schedule_deltas, add='+')
    app.tooltip(screen.tree, 'Цены из сохранённого GETTEX-архива 15m или последнего просмотренного ТФ. Не являются котировкой исполнения.')
    ttk.Button(screen.sidebar, text=tr('＋ Добавить / все акции'), command=app.show_watchlist).pack(fill='x', pady=(2, 0))
    screen.query.trace_add('write', lambda *_: screen.sync_watchlist())

    screen.workspace = ttk.Frame(chart)
    screen.workspace.pack(side='left', fill='both', expand=True)
    screen.asset = ttk.Frame(screen.workspace, padding=(8, 5), style='Desk.TFrame')
    screen.asset.pack(fill='x', pady=(0, 3))
    screen.asset_logo = ttk.Label(screen.asset, text='◈', style='DeskTitle.TLabel')
    screen.asset_logo.grid(row=0, column=0, rowspan=2, padx=(0, 8), sticky='nw')
    app.instrument = tk.StringVar(master=app.root, value=app.symbol.get())
    app.metrics = {k: tk.StringVar(master=app.root, value='—') for k in ['price', 'ema10', 'ema30', 'volume', 'support', 'resistance', 'stop', 'add', 'updated', 'rsi', 'atr', 'position']}
    screen.asset.columnconfigure(1, weight=1)
    ttk.Label(screen.asset, textvariable=app.instrument, style='DeskTitle.TLabel', wraplength=330).grid(row=0, column=1, sticky='w')
    ttk.Label(screen.asset, textvariable=app.metrics['price'], style='DeskPrice.TLabel').grid(row=0, column=2, sticky='e', padx=6)
    ttk.Label(screen.asset, textvariable=screen.context, style='DeskMuted.TLabel', font=('Segoe UI', 9)).grid(row=1, column=1, sticky='w')
    screen.delta = tk.StringVar(app.root, '—')
    screen.delta_label = ttk.Label(screen.asset, textvariable=screen.delta, style='DeskPositive.TLabel', font=('Segoe UI', 9))
    screen.delta_label.grid(row=1, column=2, sticky='e', padx=6)
    header_context = ttk.Frame(screen.asset, style='Desk.TFrame')
    header_context.grid(row=0,column=3,rowspan=2,sticky='e',padx=(8,0))
    ttk.Label(header_context, text=tr('Премаркет: —'), style='DeskMuted.TLabel', font=('Segoe UI', 8)).pack(anchor='e')
    screen.header_volume = tk.StringVar(app.root, tr('Объём: —'))
    ttk.Label(header_context, textvariable=screen.header_volume, style='DeskMuted.TLabel', font=('Segoe UI', 8)).pack(anchor='e',pady=3)
    header_visible=[True]
    def header_width(event):
        visible=event.width>=720
        if visible==header_visible[0]:return
        header_visible[0]=visible
        if visible:header_context.grid()
        else:header_context.grid_remove()
    screen.asset.bind('<Configure>', header_width, add='+')

    tabs = ttk.Frame(screen.asset, style='Desk.TFrame')
    tabs.grid(row=2, column=0, columnspan=4, sticky='ew', pady=(7, 3))
    tabs.columnconfigure(0,weight=1)
    tab_strip = ttk.Frame(tabs,style='Desk.TFrame')
    tab_strip.grid(row=0,column=0,sticky='ew')
    tab_buttons=[]
    from workspace_pages import SECTIONS
    screen.section_buttons={}
    for section,label in SECTIONS.items():
        button=ttk.Button(tab_strip,text=tr(label),command=lambda key=section:screen.pages.select(key),style='DeskActive.TButton' if section=='chart' else 'TButton')
        screen.section_buttons[section]=button
        tab_buttons.append(button)
    def wrap_tabs(event):
        flow_widgets(tab_buttons,event.width)
    tab_strip.bind('<Configure>',wrap_tabs,add='+')
    toolbar = ttk.Frame(screen.asset, style='Desk.TFrame')
    toolbar.grid(row=3, column=0, columnspan=4, sticky='ew', pady=(3, 0))
    for tf, label in LABELS.items():
        button = ttk.Button(toolbar, text=label, width=3, command=lambda value=tf: screen.choose_tf(value))
        button.pack(side='left', padx=(0, 1))
        screen.tf_buttons[tf] = button
    tools = toolbar
    app.symbol_box = ttk.Combobox(tools, textvariable=app.symbol, values=[i['name'] for i in app.watchlist], state='readonly', width=14)
    app.symbol_box.bind('<<ComboboxSelected>>', lambda _: app.refresh_chart())
    app.timeframe_box = ttk.Combobox(tools, textvariable=app.timeframe, values=list(LABELS), state='readonly', width=4)
    app.timeframe_box.bind('<<ComboboxSelected>>', lambda _: app.refresh_chart())
    app.period_box = CanonicalCombobox(tabs, textvariable=app.period, values=list(periods), state='readonly', width=11)
    app.period_box.grid(row=0,column=1,sticky='ne',padx=2)
    app.period_box.bind('<<ComboboxSelected>>', lambda _: app.change_period())
    app.chart_mode = tk.StringVar(master=app.root, value=app.store.setting('chart_mode', 'Candles'))
    if app.chart_mode.get() not in MODES:
        app.chart_mode.set('Candles')
    mode_box = ttk.Menubutton(tools, image=photo(app.root,'candles'), style='DeskMenu.TMenubutton', width=2)
    bind_icon(mode_box,app.root,'candles')
    mode_menu = tk.Menu(mode_box, tearoff=False, background=bg, foreground=text, activebackground='#17355a', activeforeground='white')
    mode_box.configure(menu=mode_menu)
    for mode in MODES:
        mode_menu.add_radiobutton(label=tr(mode), variable=app.chart_mode, value=mode, command=app.change_chart_mode)
    mode_box.pack(side='left', padx=2)
    def line_mode():
        app.chart_mode.set('Line');app.change_chart_mode()
    line_button = ttk.Button(tools, command=line_mode)
    bind_icon(line_button, app.root, 'line')
    line_button.pack(side='left', padx=1)
    app.tooltip(line_button, 'Линейный график')
    collapsing_tools=[]
    for label, artwork, command in [('Индикаторы', 'indicators', app.show_indicators), ('Шаблоны', 'template', app.show_drawing),
                                   ('', 'draw', app.show_drawing), ('', 'ruler', app.start_measurement),
                                   ('', 'reset', app.reset_view), ('', 'camera', screen.export_chart),
                                   ('', 'gear', app.show_settings), ('', 'fullscreen', app.toggle_chart_fullscreen)]:
        button = ttk.Button(tools, text=tr(label), image=photo(app.root,artwork), compound='left', command=command)
        bind_icon(button,app.root,artwork)
        button.pack(side='left', padx=1)
        if label:
            collapsing_tools.append((button,tr(label)));app.tooltip(button,label)
        if not label:app.tooltip(button, {'draw':'Рисование','ruler':'Линейка','reset':'Сбросить масштаб','camera':'Сохранить график в PNG','gear':'Настройки','fullscreen':'Полный экран'}[artwork])
    app.root.bind('<Escape>', app.escape_chart, add='+')
    visible_tools = toolbar.pack_slaves()
    for widget in visible_tools:widget.pack_forget()
    last_layout = [None]
    def wrap_tools(event=None):
        width = event.width if event else toolbar.winfo_width()
        if width < 20 or width == last_layout[0]:return
        last_layout[0] = width
        for widget,label in collapsing_tools:
            text=label if width>=950 else ''
            if widget.cget('text')!=text:widget.configure(text=text)
        flow_widgets(visible_tools,width-4)
    toolbar.bind('<Configure>', wrap_tools, add='+')
    app.root.after_idle(wrap_tools)


    # Lower strip mirrors the reference. Every item is actual cached news;
    # no invented calendar, premium feed availability or fake account summary.
    screen.bottom_shared = ttk.Frame(screen.right_workspace,height=145)
    screen.bottom_shared.pack(side='bottom',fill='x',before=body,pady=(4,0))
    screen.bottom_shared.grid_propagate(False)
    screen.bottom_shared.columnconfigure(0,weight=55,uniform='bottom')
    screen.bottom_shared.columnconfigure(1,weight=45,uniform='bottom')
    screen.bottom_shared.rowconfigure(0,weight=1)
    screen.news = ttk.Frame(screen.bottom_shared, padding=5, style='DeskCard.TFrame')
    screen.news.grid(row=0,column=0,sticky='nsew',padx=(0,6))
    news_bar = ttk.Frame(screen.news, style='Desk.TFrame')
    news_bar.pack(fill='x')
    screen.bottom_buttons={}
    for label, command in [('Новости',lambda:screen.pages.select('news')),('События',lambda:screen.pages.select('events')),('Отчёты',lambda:screen.pages.select('reports')),('Календарь', lambda:screen.calendar_panel.show())]:
        button=ttk.Button(news_bar,text=tr(label),command=command);button.pack(side='left',padx=(0,2));screen.bottom_buttons[label]=button
    screen.news_text = tk.StringVar(app.root)
    screen.news_label = ttk.Label(screen.news, textvariable=screen.news_text, style='DeskMuted.TLabel', wraplength=650, font=('Segoe UI', 8))
    screen.news_label.pack(fill='x', pady=2)
    screen.news_tree = ttk.Treeview(screen.news, columns=('time', 'provider', 'title'), show='', height=4, style='DeskNews.Treeview')
    for key, label, width in [('time', 'Время источника', 108), ('provider', 'Источник', 100), ('title', 'Публикация', 400)]:
        screen.news_tree.heading(key, text=tr(label))
        screen.news_tree.column(key, width=width, minwidth=55, stretch=key == 'title')
    screen.news_tree.pack(fill='x')
    screen.news_tree.bind('<Double-1>',lambda _:screen.pages.select('news'))
    screen.news.bind('<Configure>', lambda e: screen.news_label.configure(wraplength=max(100, e.width - 15)))
    app.fig = Figure(figsize=(10, 7), dpi=100, facecolor=bg)
    app.ax_price = app.fig.add_axes([.09, .3, .88, .65])
    app.ax_vol = app.fig.add_axes([.09, .1, .88, .15], sharex=app.ax_price)
    for axes in (app.ax_price, app.ax_vol):
        axes.set_facecolor(bg)
    app.canvas = FigureCanvasTkAgg(app.fig, master=screen.workspace)
    app.canvas.get_tk_widget().pack(fill='both', expand=True)
    from workspace_pages import Pages
    screen.pages=Pages(screen);screen.pages.buttons=screen.section_buttons
    for event, callback in [('motion_notify_event', app.on_motion), ('figure_leave_event', app.on_leave), ('button_press_event', app.on_press), ('button_release_event', app.on_release), ('scroll_event', app.on_scroll)]:
        app.canvas.mpl_connect(event, callback)

    # Right column: compact separate cards, not the former long metrics list.
    calendar = ttk.Frame(screen.bottom_shared, padding=9, height=139, style='DeskCard.TFrame')
    calendar.grid(row=0,column=1,sticky='nsew')
    calendar.pack_propagate(False)
    from calendar_panel import Panel as CalendarPanel
    screen.calendar_panel = CalendarPanel(app, calendar)
    scroller = tk.Canvas(side, highlightthickness=0, background=bg, width=286)
    scrollbar = ttk.Scrollbar(side, command=scroller.yview,style='Desk.Vertical.TScrollbar')
    scroller.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side='right', fill='y')
    scroller.pack(fill='both', expand=True)
    content = ttk.Frame(scroller, padding=(4, 2))
    window = scroller.create_window(0, 0, window=content, anchor='nw')
    content.bind('<Configure>', lambda _: scroller.configure(scrollregion=scroller.bbox('all')))
    scroller.bind('<Configure>', lambda event: scroller.itemconfigure(window, width=event.width))
    analysis_heading=ttk.Frame(content)
    analysis_heading.pack(fill='x',pady=(2,9))
    screen.favorite_button=ttk.Button(analysis_heading,command=screen.toggle_favorite,width=3,state='disabled')
    screen.favorite_button.pack(side='right')
    ttk.Label(analysis_heading, text=tr('CandlePilot — Анализ'), font=('Segoe UI', 12, 'bold')).pack(side='left')

    def card(title):
        frame = ttk.Frame(content, padding=(9, 7), style='DeskCard.TFrame')
        frame.pack(fill='x', pady=(0, 7))
        frame.heading = ttk.Label(frame, text=tr(title), style='Desk.TLabel', font=('Segoe UI', 10, 'bold'))
        frame.heading.pack(anchor='w', pady=(0, 5))
        return frame

    signal = card('Сигнал')
    screen.signal_caption = tk.StringVar(app.root, '1H')
    app.decision = tk.StringVar(app.root)
    signal.heading.configure(textvariable=screen.signal_caption)
    screen.signal_reason=tk.StringVar(app.root)
    decision_row(signal,app.decision,screen.signal_reason)
    contexts = ttk.Frame(signal, style='Desk.TFrame')
    contexts.pack(fill='x')
    screen.context_vars = {tf: tk.StringVar(app.root, f'{tf}\n—') for tf in ('15m', '1H', '4H', '1D')}
    screen.context_frames={}
    for tf,variable in screen.context_vars.items():
        cell = ttk.Frame(contexts, style='DeskCard.TFrame', padding=3)
        cell.pack(side='left', fill='x', expand=True, padx=1)
        label=ttk.Label(cell, textvariable=variable, style='DeskSmall.TLabel', width=7, wraplength=58, anchor='center')
        label.pack(fill='x')
        screen.context_frames[tf]=cell,label
        cell.bind('<Button-1>',lambda _,value=tf:screen.choose_tf(value))
        label.bind('<Button-1>',lambda _,value=tf:screen.choose_tf(value))
    strength = card('Сила сигнала')
    strength_row = ttk.Frame(strength, style='Desk.TFrame')
    strength_row.pack(fill='x')
    ttk.Label(strength_row, textvariable=screen.strength, style='Desk.TLabel', font=('Segoe UI', 12, 'bold'), width=5, anchor='e').pack(side='right')
    screen.progress = SignalMeter(strength_row)
    screen.progress.pack(side='left', fill='x', expand=True, pady=5)
    app.tooltip(screen.progress, 'Сила подтверждений, не вероятность прибыли')
    conditions = card('Условия стратегии')
    screen.condition_vars = {key: tk.StringVar(app.root, '—') for key in ('trend', 'volume', 'macd', 'rsi')}
    for key, label in [('trend', 'Тренд (EMA 10 > EMA 30)'), ('volume', 'Объём / средний 20'), ('macd', 'MACD: гистограмма > 0'), ('rsi', 'RSI (14)')]:
        row = ttk.Frame(conditions, style='Desk.TFrame')
        row.pack(fill='x', pady=1)
        ttk.Label(row, text=tr(label), style='DeskSmall.TLabel').pack(side='left')
        ttk.Label(row, textvariable=screen.condition_vars[key], style='DeskSmall.TLabel').pack(side='right')
    levels = ttk.Frame(conditions, style='Desk.TFrame')
    levels.pack(fill='x', pady=1)
    ttk.Label(levels, text=tr('Уровни (поддержка/сопротивление)'), style='DeskSmall.TLabel').pack(side='left')
    screen.levels_visible=tk.BooleanVar(app.root,value=app.store.setting('global_indicators',{}).get('Уровни анализа',True))
    screen.levels_button=ttk.Checkbutton(levels,variable=screen.levels_visible,command=screen.toggle_levels)
    screen.levels_button.pack(side='right')
    sources = card('Источники данных')
    screen.source_summary = tk.StringVar(app.root, '—')
    screen.listing = tk.StringVar(app.root, '—')
    screen.source_rows = {}
    for name in ('GETTEX','Xetra','LSEG / Refinitiv','financial.com'):
        row = ttk.Frame(sources, style='Desk.TFrame')
        row.pack(fill='x', pady=1)
        ttk.Label(row, text=name, style='DeskSmall.TLabel').pack(side='left')
        variable = tk.StringVar(app.root, '—')
        label = ttk.Label(row, textvariable=variable, style='DeskMuted.TLabel', font=('Segoe UI', 9))
        label.pack(side='right')
        screen.source_rows[name] = variable, label
    app.tooltip(sources, 'Показан выбранный ряд и время его свечи. LSEG и financial.com описывают одного поставщика, а не независимые подтверждения свежести.')
    quality = ttk.Frame(side,padding=(9,5),style='DeskCard.TFrame')
    quality.pack(side='bottom',fill='x',before=scroller,padx=4,pady=(4,0))
    ttk.Label(quality,text=tr('Качество данных'),style='Desk.TLabel',font=('Segoe UI',10,'bold')).pack(anchor='w')
    screen.quality = tk.StringVar(app.root, '—')
    screen.quality_bar = ttk.Progressbar(quality, style='DeskQualityPending.Horizontal.TProgressbar', maximum=100)
    screen.quality_bar.pack(fill='x', pady=(3, 5))
    app.tooltip(screen.quality_bar, 'Цвет показывает фактический DQ-статус, а не выдуманный процент качества или допуск к сделке.')
    ttk.Label(quality, textvariable=screen.quality, style='Desk.TLabel').pack(anchor='w')
    details = card('Позиция и подробности анализа')
    for key, label in [('ema10', 'EMA 10'), ('ema30', 'EMA 30'), ('support','Поддержка'),('resistance','Сопротивление'),('stop', 'Стоп'), ('add', 'Добавление'), ('position', 'Позиция / результат')]:
        ttk.Label(details, text=tr(label), style='DeskMuted.TLabel').pack(anchor='w')
        ttk.Label(details, textvariable=app.metrics[key], style='DeskSmall.TLabel', wraplength=245).pack(anchor='w', pady=(0, 3))
    app.explain = tk.StringVar(app.root)
    explanation = ScrolledText(details, height=8, width=27, wrap='word', font=('Segoe UI', 9), background=bg, foreground=text, relief='flat')
    explanation.pack(fill='x', pady=5)
    explanation.configure(state='disabled')
    app.explain.trace_add('write', lambda *_: set_text(explanation, app.explain.get()))
    app.source_choice = tk.StringVar(app.root, tr('Источник: ожидается подтверждение'))
    app.source_box = ttk.Label(details, textvariable=app.source_choice, style='DeskMuted.TLabel', wraplength=245)
    app.source_box.pack(anchor='w')
    ttk.Label(details, textvariable=screen.source_summary, style='DeskMuted.TLabel', wraplength=245).pack(anchor='w')
    ttk.Label(details, textvariable=screen.listing, style='DeskMuted.TLabel', wraplength=245).pack(anchor='w')
    ttk.Label(details, textvariable=app.source_status, style='DeskMuted.TLabel', wraplength=245).pack(anchor='w', pady=4)
    ttk.Label(details, textvariable=app.metrics['updated'], style='DeskMuted.TLabel', wraplength=245).pack(anchor='w')
    app.main_benchmark_host = card('Рыночный контекст')

    # Reference proportions scale with the window; a user-adjusted divider
    # remains their divider. No figure/artist/viewport is replaced on resize.
    screen._body_width = None
    screen._split_ratio = None
    def layout_columns(event):
        width = screen.shell.winfo_width()
        body_width = body.winfo_width()
        marker = (width,body_width)
        if width < 600 or body_width < 400 or marker == screen._body_width or len(body.panes()) != 2:return
        screen._body_width = marker
        sidebar_width=max(200,min(270,round(width*.16)))
        if int(screen.sidebar.cget('width'))!=sidebar_width:screen.sidebar.configure(width=sidebar_width)
        right = max(250, min(380, round(width * .22)))
        position = body_width-right if screen._split_ratio is None else round(body_width * screen._split_ratio)
        position = min(body_width - 250, position)
        if position > 0 and body.sashpos(0)!=position:body.sashpos(0, position)

    def divider_changed(_):
        if len(body.panes()) == 2:screen._split_ratio = body.sashpos(0) / max(1, body.winfo_width())
    body.bind('<Configure>', layout_columns, add='+')
    screen.shell.bind('<Configure>',layout_columns,add='+')
    body.bind('<ButtonRelease-1>', divider_changed, add='+')

    app.symbol.trace_add('write', screen.selection_changed)
    app.timeframe.trace_add('write', screen.selection_changed)
    screen.sync_watchlist()
    screen._account_timer = app.root.after(5000, screen.refresh_account)
    screen._favorite_timer = app.root.after(500,screen.load_favorites)

