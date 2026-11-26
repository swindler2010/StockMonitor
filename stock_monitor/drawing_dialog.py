"""Drawing toolbox presentation over existing guarded draft operations."""
import copy
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from i18n import tr, rendered_message
from indicator_dialog import styles
from ui_icons import photo
from themes import THEMES
from drawing_actions import commit, modify, travel, chart_identity

from drawing_tools import REGISTRY,GROUPS
from localized_widgets import CanonicalCombobox

TOOLS=tuple((value['name'],kind,'') for kind,value in REGISTRY.items())
NAMES={kind:value['name'] for kind,value in REGISTRY.items()}


class Dialog:
    def __init__(self,app,info):
        from main_screen import native_titlebar
        self.app=app;self.info=copy.deepcopy(info);self.isin=info['isin'];self.identity=chart_identity(app)
        self.listed=copy.deepcopy(app.user_state(info).get('drawings',[]))
        self.win=win=tk.Toplevel(app.root,background=ttk.Style(app.root).lookup('Desk.TFrame','background') or '#0b1220');app._drawing_windows[self.isin]=win
        win.title(tr('Рисование • {name}',name=info['name']))
        width=min(1080,max(900,app.root.winfo_width()-80));height=min(790,max(780,app.root.winfo_height()-90))
        from design_layout import fit_window
        fit_window(win,1100,850,(850,590))
        palette=THEMES.get(app.store.setting('theme','Тёмная'),THEMES['Тёмная']);self.palette=palette;self.p=p=styles(app.root,palette)
        style=ttk.Style(win)
        style.layout(p+'.Tool.TButton',style.layout(p+'.Secondary.TButton'))
        style.configure(p+'.Tool.TButton',font=('Segoe UI',10,'bold'),foreground=palette['text'],padding=(6,4))
        style.configure(p+'.Drawing.TLabel',background='#0a2136' if palette['bg']!='#f8fafc' else '#ffffff',foreground=palette['text'])
        base=ttk.Frame(win,padding=8,style=p+'.TFrame');base.pack(fill='both',expand=True)
        ttk.Label(base,text=tr('Рисование')+' • '+info['name'],style=p+'.TLabel',font=('Segoe UI',16,'bold')).pack(anchor='w',pady=(0,6))
        tabs=ttk.Frame(base,style=p+'.TFrame');tabs.pack(fill='x',pady=(0,6))
        self.tabs={}
        for text,command in [('Инструменты',lambda:self.reveal(self.tools_box)),('Избранное',self.favorites),('Объекты',lambda:self.reveal(self.objects)),('Шаблоны',self.templates),('Калькулятор позиции',self.calculator),('Настройки',self.settings)]:
            b=ttk.Button(tabs,text=tr(text),image=photo(app.root,{'Инструменты':'draw','Избранное':'star_filled','Объекты':'template','Шаблоны':'template','Калькулятор позиции':'ruler','Настройки':'gear'}[text],18),compound='left',style=p+'.Secondary.TButton',command=command);b.grid(row=0,column=len(self.tabs),sticky='ew',padx=(0,4));self.tabs[text]=b
        from main_screen import flow_widgets
        tabs.bind('<Configure>',lambda event:flow_widgets(list(self.tabs.values()),event.width),add='+')
        self.tabs['Инструменты'].configure(style=p+'.Primary.TButton')
        body_host=ttk.Frame(base,style=p+'.TFrame');body_host.pack(fill='both',expand=True)
        self.body_canvas=tk.Canvas(body_host,highlightthickness=0,background=palette['bg'])
        body_scroll=ttk.Scrollbar(body_host,command=self.body_canvas.yview,style=p+'.Vertical.TScrollbar');body_scroll.pack(side='right',fill='y')
        self.body_canvas.configure(yscrollcommand=body_scroll.set);self.body_canvas.pack(fill='both',expand=True)
        body=ttk.Frame(self.body_canvas,style=p+'.TFrame');self.body_item=self.body_canvas.create_window(0,0,window=body,anchor='nw')
        body.bind('<Configure>',lambda _:self.body_canvas.configure(scrollregion=self.body_canvas.bbox('all')))
        self.body_canvas.bind('<Configure>',lambda e:self.body_canvas.itemconfigure(self.body_item,width=e.width))
        self.body_canvas.bind('<MouseWheel>',lambda e:self.body_canvas.yview_scroll(-1 if e.delta>0 else 1,'units'))
        self.tools_box=tools=ttk.Frame(body,padding=6,style=p+'.Info.TFrame');tools.pack(fill='x',pady=(0,6))
        group_bar=ttk.Frame(tools,style=p+'.TFrame');group_bar.pack(fill='x',pady=(0,8))
        self.group_caption=tk.StringVar(win,tr('Инструменты рисования'))
        ttk.Label(group_bar,textvariable=self.group_caption,style=p+'.Drawing.TLabel',font=('Segoe UI',12,'bold')).pack(side='left')
        self.tool_group=tk.StringVar(win,'Все группы')
        self.group_box=CanonicalCombobox(group_bar,textvariable=self.tool_group,values=['Все группы',*GROUPS],state='readonly',style=p+'.TCombobox',width=30)
        self.group_box.pack(side='right')
        from drawing_gallery import Gallery
        self.gallery=Gallery(self,tools)
        self.tool_group.trace_add('write',lambda *_:self.show_tools());self.show_tools()
        detail=ttk.Frame(body,padding=6,style=p+'.Info.TFrame');detail.pack(fill='x',pady=(0,6))
        detail_head=ttk.Frame(detail,style=p+'.TFrame');detail_head.pack(fill='x')
        ttk.Label(detail_head,text=tr('Выбранный объект'),style=p+'.Drawing.TLabel',font=('Segoe UI',12,'bold')).pack(side='left')
        self.style_button=ttk.Button(detail_head,text=tr('Стиль…'),command=self.settings);self.style_button.pack(side='right')
        self.detail=tk.StringVar(win,tr('Выберите объект в списке ниже'))
        ttk.Label(detail,textvariable=self.detail,style=p+'.Drawing.TLabel',font=('Segoe UI',10),wraplength=920,justify='left').pack(anchor='w',pady=6)
        panel=ttk.Frame(body,padding=6,style=p+'.Info.TFrame');panel.pack(fill='both',expand=True,pady=(0,6))
        self.count=tk.StringVar(win);ttk.Label(panel,textvariable=self.count,style=p+'.Drawing.TLabel',font=('Segoe UI',12,'bold')).pack(anchor='w',pady=(0,8))
        self.query=tk.StringVar(win)
        searchbar=ttk.Frame(panel,style=p+'.TFrame');searchbar.pack(fill='x',pady=(0,8))
        entry=ttk.Entry(searchbar,textvariable=self.query,style=p+'.TEntry',font=('Segoe UI',11));entry.pack(side='left',fill='x',expand=True)
        self.flags={}
        for label,operation in [('Скрыть','visible'),('Закрепить','locked'),('Вперёд','front'),('Назад','back'),('Слой…','layer')]:
            button=ttk.Button(searchbar,text=tr(label),command=lambda op=operation:self.change(op),style=p+'.Tool.TButton');button.pack(side='left',padx=(5,0));self.flags[operation]=button
        app.tooltip(entry,'Поиск по типу, дате, цене и источнику объекта')
        selection_bar=ttk.Frame(panel,style=p+'.TFrame');selection_bar.pack(fill='x',pady=(0,5))
        for label,command in [('Выбрать все',lambda:self.objects.selection_set(self.objects.get_children())),('Снять выделение',lambda:self.objects.selection_remove(self.objects.selection())),('Экспорт JSON…',self.export)]:
            ttk.Button(selection_bar,text=tr(label),command=command,style=p+'.Tool.TButton').pack(side='left',padx=(0,5))
        host=ttk.Frame(panel,style=p+'.TFrame');host.pack(fill='both',expand=True)
        style=ttk.Style(win);style.configure(p+'.Drawing.Treeview',background=palette['bg'],fieldbackground=palette['bg'],foreground=palette['text'],font=('Segoe UI',10),rowheight=29)
        style.configure(p+'.Drawing.Treeview.Heading',background=palette['bg'],foreground=palette['text'],font=('Segoe UI',10,'bold'))
        style.layout(p+'.Drawing.Treeview.Heading',[('Treeheading.padding',{'sticky':'nswe','children':[('Treeheading.text',{'sticky':'we'})]})])
        style.configure(p+'.Drawing.Horizontal.TScrollbar',background=palette['grid'],troughcolor=palette['bg'],bordercolor=palette['bg'],lightcolor=palette['bg'],darkcolor=palette['bg'],arrowcolor=palette['muted'])
        style.map(p+'.Drawing.Treeview',background=[('selected','#075585')],foreground=[('selected','#ffffff')])
        self.objects=ttk.Treeview(host,columns=('kind','at','price','source','currency','layer','state'),show='headings',selectmode='extended',style=p+'.Drawing.Treeview',height=3)
        for key,label,width in [('kind','Объект',150),('at','Дата / время',180),('price','Цена',90),('source','Источник',150),('currency','Валюта',75),('layer','Слой / группа',140),('state','Состояние',155)]:
            self.objects.heading(key,text=tr(label));self.objects.column(key,width=width,minwidth=70)
        scroll=ttk.Scrollbar(host,command=self.objects.yview,style=p+'.Vertical.TScrollbar');scroll.pack(side='right',fill='y');self.objects.configure(yscrollcommand=scroll.set)
        horizontal=ttk.Scrollbar(panel,orient='horizontal',command=self.objects.xview,style=p+'.Drawing.Horizontal.TScrollbar');horizontal.pack(side='bottom',fill='x');self.objects.configure(xscrollcommand=horizontal.set)
        self.objects.pack(fill='both',expand=True);self.objects.bind('<<TreeviewSelect>>',lambda _:self.selection())
        self.query.trace_add('write',lambda *_:self.render())
        actions=ttk.Frame(base,padding=6,style=p+'.Info.TFrame');actions.pack(side='bottom',fill='x',pady=(0,6),before=body_host)
        ttk.Label(actions,text=tr('Действия'),style=p+'.Drawing.TLabel',font=('Segoe UI',12,'bold')).pack(anchor='w',pady=(0,8))
        row=ttk.Frame(actions,style=p+'.TFrame');row.pack(fill='x');self.actions={}
        for label,operation in [('Дублировать','copy'),('Изменить цену','price'),('Изменить дату','at'),('Удалить выбранный','delete'),('Текст…','text')]:
            b=ttk.Button(row,text=tr(label),style=p+'.Secondary.TButton',command=lambda op=operation:self.change(op));b.pack(side='left',fill='x',expand=True,padx=(0,6));self.actions[operation]=b
        grouping=ttk.Frame(actions,style=p+'.TFrame');grouping.pack(fill='x',pady=(5,0))
        for label,operation in [('Группировать','group'),('Разгруппировать','ungroup')]:
            b=ttk.Button(grouping,text=tr(label),style=p+'.Secondary.TButton',command=lambda op=operation:self.change(op));b.pack(side='left',fill='x',expand=True,padx=(0,6));self.actions[operation]=b
        ttk.Label(grouping,text=tr('Ctrl / Shift — несколько объектов; действия применяются ко всей группе.'),style=p+'.TLabel',wraplength=440).pack(side='left',padx=6)
        footer=ttk.Frame(base,style=p+'.TFrame');footer.pack(side='bottom',fill='x',before=actions)
        for label,operation in [('↶ Отменить','undo'),('↷ Повторить','redo')]:
            ttk.Button(footer,text=tr(label),style=p+'.Secondary.TButton',command=lambda op=operation:self.history(op)).pack(side='left',padx=(0,8))
        self.done_button=ttk.Button(footer,text=tr('✓ Готово'),style=p+'.Primary.TButton',command=win.destroy);self.done_button.pack(side='right')
        self.note=tk.StringVar(win,tr('Разметка сохраняется штатной кнопкой «Сохранить» в главном окне.'))
        ttk.Label(base,textvariable=self.note,style=p+'.TLabel',font=('Segoe UI',9),wraplength=970).pack(side='bottom',anchor='w',pady=(8,0),before=footer)
        win.bind('<Escape>',lambda _:win.destroy())
        win.bind('<Map>',lambda event:win.after_idle(lambda:native_titlebar(win,palette)) if event.widget is win else None,add='+')
        self.render()

    def reveal(self,widget):
        active='Объекты' if widget is self.objects else 'Инструменты'
        for key,button in self.tabs.items():button.configure(style=self.p+('.Primary.TButton' if key==active else '.Secondary.TButton'))
        self.win.update_idletasks()
        height=max(1,self.body_canvas.bbox('all')[3])
        at=widget.winfo_rooty()-self.body_canvas.winfo_rooty()+self.body_canvas.canvasy(0)
        self.body_canvas.yview_moveto(max(0,at)/height)
        widget.focus_set()

    def favorites(self):
        self.reveal(self.tools_box)
        self.gallery.edit_favorites()
        self.tabs['Инструменты'].configure(style=self.p+'.Secondary.TButton')
        self.tabs['Избранное'].configure(style=self.p+'.Primary.TButton')

    def valid(self):
        return chart_identity(self.app)==self.identity and self.app.instrument_cfg()['isin']==self.isin and self.app.user_state(self.app.instrument_cfg()).get('drawings',[])==self.listed

    def reject(self):self.note.set(tr('Акция или разметка изменились. Откройте окно рисования заново.'))

    def choose(self,kind):
        if not self.valid():self.reject();return
        from drawing_tools import start
        start(self.app,kind)
        self.win.destroy()

    def render(self):
        selected=self.objects.selection();self.objects.delete(*self.objects.get_children());query=self.query.get().casefold()
        for n,obj in enumerate(self.listed):
            values=(str(n+1)+'. '+tr(NAMES.get(obj.get('type'),obj.get('type','—'))),str(obj.get('at') or '—'),str(obj.get('price') or '—'),str(obj.get('source_mode') or '—'),str(obj.get('currency') or '—'),str(obj.get('layer','Основной'))+(' · '+str(obj['group_name']) if obj.get('group_name') else ''),tr('Скрыт') if obj.get('visible',True) is False else tr('Закреплён') if obj.get('locked',False) else tr('Видим'))
            if query in ' '.join(values).casefold():self.objects.insert('','end',iid=str(n),values=values)
        kept=[iid for iid in selected if self.objects.exists(iid)]
        if kept:self.objects.selection_set(kept)
        self.count.set(tr('Список объектов')+f' ({len(self.objects.get_children())} / {len(self.listed)})');self.selection()

    def selection(self):
        selected=self.objects.selection();obj=self.listed[int(selected[0])] if selected else None
        from drawing_management import selection
        indices=selection(self.listed,[int(i) for i in selected]) if selected else []
        locked=any(self.listed[i].get('locked') for i in indices)
        single=len(indices)==1
        for operation,button in self.actions.items():
            enabled=bool(indices) and (not locked or operation=='copy')
            if operation in ('price','at','text'):enabled=enabled and single
            if operation=='group':enabled=enabled and len(indices)>1
            if operation=='ungroup':enabled=enabled and any(self.listed[i].get('group_id') for i in indices)
            if operation=='price' and obj and obj.get('type')=='vline':enabled=False
            if operation=='at' and obj and obj.get('type')=='hline':enabled=False
            if operation=='text' and obj and obj.get('type') not in ('text','note','callout'):enabled=False
            button.configure(state='normal' if enabled else 'disabled')
        self.style_button.configure(state='normal' if single and not locked else 'disabled')
        for op,button in self.flags.items():button.configure(state='normal' if indices and (not locked or op in ('locked','visible')) else 'disabled')
        self.flags['visible'].configure(text=tr('Показать') if indices and all(self.listed[i].get('visible',True) is False for i in indices) else tr('Скрыть'))
        self.flags['locked'].configure(text=tr('Открепить') if locked else tr('Закрепить'))
        if len(indices)>1:self.detail.set(tr('Выбрано объектов: {count}',count=len(indices)))
        elif obj:self.detail.set(tr(NAMES.get(obj.get('type'),obj.get('type','—')))+' · '+str(obj.get('price','—'))+' · '+str(obj.get('at','—'))+'\n'+str(obj.get('source_mode','—'))+' · '+str(obj.get('currency','—')))
        else:self.detail.set(tr('Выберите объект в списке ниже'))

    def change(self,operation):
        selected=self.objects.selection()
        if not selected:return
        if not self.valid():self.reject();return
        from drawing_management import batch,selection
        indices=selection(self.listed,[int(i) for i in selected]);index=indices[0];value=None;identity=chart_identity(self.app)
        if operation in ('price','at','text') and len(indices)!=1:return
        if operation=='price':value=simpledialog.askfloat(tr('Цена объекта'),tr('Новая цена начальной точки'),initialvalue=self.listed[index].get('price'),parent=self.win)
        elif operation in ('visible','locked'):value=not any(self.listed[i].get(operation,operation=='visible') for i in indices)
        elif operation=='group':value=simpledialog.askstring(tr('Группировать'),tr('Имя группы'),initialvalue=tr('Группа'),parent=self.win)
        elif operation=='layer':value=simpledialog.askstring(tr('Слой объекта'),tr('Имя слоя'),initialvalue=self.listed[index].get('layer','Основной'),parent=self.win)
        elif operation=='text':value=simpledialog.askstring(tr('Текст рисунка'),tr('Введите текст (до 240 символов)'),initialvalue=self.listed[index].get('text',''),parent=self.win)
        elif operation=='at':value=simpledialog.askstring(tr('Дата объекта'),tr('Новая дата начальной точки'),initialvalue=self.listed[index].get('at'),parent=self.win)
        if operation in ('price','at','layer','text','group') and value is None:return
        if not self.valid() or identity!=chart_identity(self.app):self.reject();return
        try:
            changed=batch(self.listed,indices,operation,value)
            if commit(self.app,self.isin,self.listed,changed):self.listed=copy.deepcopy(changed);self.render();self.note.set(tr('Разметка изменена. Нажмите «Сохранить» в главном окне.'))
            else:self.reject()
            if operation in ('front','back'):
                target=str(len(changed)-1 if operation=='front' else 0)
                if self.objects.exists(target):self.objects.selection_set(target);self.objects.see(target)
        except (ValueError,TypeError) as error:messagebox.showerror(tr('Разметка'),rendered_message(str(error)),parent=self.win)

    def export(self):
        if not self.valid():self.reject();return
        identity=chart_identity(self.app)
        from drawing_management import export_document
        selected=self.objects.selection()
        try:document=export_document(self.info,identity,self.listed,[int(i) for i in selected] if selected else None)
        except (ValueError,TypeError) as exc:self.note.set(str(exc));return
        from paths import APP_DIR
        path=filedialog.asksaveasfilename(parent=self.win,title=tr('Экспорт рисунков'),initialdir=str(APP_DIR),initialfile='drawings-'+self.isin+'.json',defaultextension='.json',filetypes=[('JSON','*.json')])
        if not path:return
        if not self.valid() or identity!=chart_identity(self.app):self.reject();return
        def work():
            from pathlib import Path
            Path(path).write_text(document,encoding='utf-8')
        def done(_):
            if self.win.winfo_exists():self.note.set(tr('Рисунки экспортированы. Свечи и счёт не включены.'))
        def failed(_):
            if self.win.winfo_exists():self.note.set(tr('Не удалось сохранить файл'))
        if self.app.job(work,done,failed,lane='read',key=('drawing-export',self.isin)) is False:failed(None)

    def history(self,operation):
        if not self.valid():self.reject();return
        if travel(self.app,self.isin,operation):
            self.listed=copy.deepcopy(self.app.user_state(self.app.instrument_cfg()).get('drawings',[]));self.render()

    def show_tools(self):
        self.group_caption.set(tr('Ганн: 1×1 задаётся точками A→B') if self.tool_group.get()=='Ганн' else tr('Ручная разметка — не сигнал') if self.tool_group.get()=='Паттерны и волны' else tr('Фибоначчи: ручные опоры, не торговый сигнал') if self.tool_group.get()=='Фибоначчи' else tr('Кисть и фигуры: Esc — отмена') if self.tool_group.get()=='Свободная разметка' else tr('Инструменты рисования'))
        self.gallery.filter(self.tool_group.get())

    def calculator(self):
        from position_calculator import show
        return show(self.app)

    def templates(self):
        dialog=self.settings()
        if dialog is not None:dialog.preset_box.focus_set()

    def settings(self):
        selected=self.objects.selection()
        if not selected:self.note.set(tr('Сначала выберите объект для настройки стиля.'));return
        if not self.valid():self.reject();return
        existing=getattr(self,'_style_dialog',None)
        if existing is not None and existing.win.winfo_exists():existing.win.lift();return existing
        from drawing_style_dialog import Dialog
        self._style_dialog=Dialog(self,int(selected[0]))
        return self._style_dialog


def show(app):
    info=app.instrument_cfg();windows=getattr(app,'_drawing_windows',{});app._drawing_windows=windows
    existing=windows.get(info['isin'])
    if existing is not None and existing.winfo_exists():existing.lift();return existing
    app._drawing_dialog=Dialog(app,info)
    return app._drawing_dialog.win
