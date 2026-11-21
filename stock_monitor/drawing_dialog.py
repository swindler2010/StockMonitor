"""Drawing toolbox presentation over existing guarded draft operations."""
import copy
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from i18n import tr, rendered_message
from indicator_dialog import styles
from ui_icons import photo
from themes import THEMES
from drawing_actions import commit, modify, travel, chart_identity

TOOLS=(('Горизонталь','hline','━'),('Вертикаль','vline','│'),
       ('Трендовая линия','trend','╱'),('Луч','ray','↗'),
       ('Зона','zone','▱'),('Маркер цены','marker','◇'))
NAMES={kind:label for label,kind,_ in TOOLS}


class Dialog:
    def __init__(self,app,info):
        from main_screen import native_titlebar
        self.app=app;self.info=copy.deepcopy(info);self.isin=info['isin']
        self.listed=copy.deepcopy(app.user_state(info).get('drawings',[]))
        self.win=win=tk.Toplevel(app.root);app._drawing_windows[self.isin]=win
        win.title(tr('Рисование • {name}',name=info['name']))
        width=min(1080,max(900,app.root.winfo_width()-80));height=min(790,max(780,app.root.winfo_height()-90))
        win.geometry(f'{width}x{height}+{max(0,app.root.winfo_rootx()+30)}+{max(0,app.root.winfo_rooty()+35)}');win.minsize(850,780)
        palette=THEMES.get(app.store.setting('theme','Тёмная'),THEMES['Тёмная']);self.p=p=styles(app.root,palette)
        style=ttk.Style(win)
        style.layout(p+'.Tool.TButton',style.layout(p+'.Secondary.TButton'))
        style.configure(p+'.Tool.TButton',font=('Segoe UI',10,'bold'),foreground=palette['text'],padding=(6,4))
        style.configure(p+'.Drawing.TLabel',background='#0a2136' if palette['bg']!='#f8fafc' else '#ffffff',foreground=palette['text'])
        base=ttk.Frame(win,padding=14,style=p+'.TFrame');base.pack(fill='both',expand=True)
        ttk.Label(base,text=tr('Рисование')+' • '+info['name'],style=p+'.TLabel',font=('Segoe UI',18,'bold')).pack(anchor='w',pady=(0,12))
        tabs=ttk.Frame(base,style=p+'.TFrame');tabs.pack(fill='x',pady=(0,10))
        self.tabs={}
        for text,command in [('Инструменты',lambda:self.tools_box.focus_set()),('Объекты',lambda:self.objects.focus_set()),('Настройки',self.settings)]:
            b=ttk.Button(tabs,text=tr(text),style=p+'.Secondary.TButton',command=command);b.pack(side='left',fill='x',expand=True,padx=(0,7));self.tabs[text]=b
        self.tabs['Инструменты'].configure(style=p+'.Primary.TButton')
        self.tools_box=tools=ttk.Frame(base,padding=12,style=p+'.Info.TFrame');tools.pack(fill='x',pady=(0,10))
        ttk.Label(tools,text=tr('Линии и фигуры'),style=p+'.Drawing.TLabel',font=('Segoe UI',12,'bold')).pack(anchor='w',pady=(0,8))
        row=ttk.Frame(tools,style=p+'.TFrame');row.pack(fill='x');self.tool_buttons={}
        for n,(label,kind,icon) in enumerate(TOOLS):
            row.columnconfigure(n,weight=1,uniform='tool')
            button=ttk.Button(row,text=tr(label).replace(' ','\n',1),image=photo(app.root,'drawing_'+kind,24,palette['text']),compound='top',style=p+'.Tool.TButton',command=lambda k=kind:self.choose(k))
            button.grid(row=0,column=n,sticky='nsew',padx=3);self.tool_buttons[kind]=button
        detail=ttk.Frame(base,padding=12,style=p+'.Info.TFrame');detail.pack(fill='x',pady=(0,10))
        ttk.Label(detail,text=tr('Выбранный объект'),style=p+'.Drawing.TLabel',font=('Segoe UI',12,'bold')).pack(anchor='w')
        self.detail=tk.StringVar(win,tr('Выберите объект в списке ниже'))
        ttk.Label(detail,textvariable=self.detail,style=p+'.Drawing.TLabel',font=('Segoe UI',10),wraplength=920,justify='left').pack(anchor='w',pady=6)
        panel=ttk.Frame(base,padding=12,style=p+'.Info.TFrame');panel.pack(fill='both',expand=True,pady=(0,10))
        self.count=tk.StringVar(win);ttk.Label(panel,textvariable=self.count,style=p+'.Drawing.TLabel',font=('Segoe UI',12,'bold')).pack(anchor='w',pady=(0,8))
        self.query=tk.StringVar(win)
        entry=ttk.Entry(panel,textvariable=self.query,style=p+'.TEntry',font=('Segoe UI',11));entry.pack(fill='x',pady=(0,8))
        app.tooltip(entry,'Поиск по типу, дате, цене и источнику объекта')
        host=ttk.Frame(panel,style=p+'.TFrame');host.pack(fill='both',expand=True)
        style=ttk.Style(win);style.configure(p+'.Drawing.Treeview',background=palette['bg'],fieldbackground=palette['bg'],foreground=palette['text'],font=('Segoe UI',10),rowheight=29)
        style.configure(p+'.Drawing.Treeview.Heading',background=palette['bg'],foreground=palette['text'],font=('Segoe UI',10,'bold'))
        style.layout(p+'.Drawing.Treeview.Heading',[('Treeheading.padding',{'sticky':'nswe','children':[('Treeheading.text',{'sticky':'we'})]})])
        style.configure(p+'.Drawing.Horizontal.TScrollbar',background=palette['grid'],troughcolor=palette['bg'],bordercolor=palette['bg'],lightcolor=palette['bg'],darkcolor=palette['bg'],arrowcolor=palette['muted'])
        style.map(p+'.Drawing.Treeview',background=[('selected','#075585')],foreground=[('selected','#ffffff')])
        self.objects=ttk.Treeview(host,columns=('kind','at','price','source','currency'),show='headings',selectmode='browse',style=p+'.Drawing.Treeview',height=6)
        for key,label,width in [('kind','Объект',180),('at','Дата / время',215),('price','Цена',110),('source','Источник',200),('currency','Валюта',85)]:
            self.objects.heading(key,text=tr(label));self.objects.column(key,width=width,minwidth=70)
        scroll=ttk.Scrollbar(host,command=self.objects.yview,style=p+'.Vertical.TScrollbar');scroll.pack(side='right',fill='y');self.objects.configure(yscrollcommand=scroll.set)
        horizontal=ttk.Scrollbar(panel,orient='horizontal',command=self.objects.xview,style=p+'.Drawing.Horizontal.TScrollbar');horizontal.pack(side='bottom',fill='x');self.objects.configure(xscrollcommand=horizontal.set)
        self.objects.pack(fill='both',expand=True);self.objects.bind('<<TreeviewSelect>>',lambda _:self.selection())
        self.query.trace_add('write',lambda *_:self.render())
        actions=ttk.Frame(base,padding=12,style=p+'.Info.TFrame');actions.pack(side='bottom',fill='x',pady=(0,10),before=panel)
        ttk.Label(actions,text=tr('Действия'),style=p+'.Drawing.TLabel',font=('Segoe UI',12,'bold')).pack(anchor='w',pady=(0,8))
        row=ttk.Frame(actions,style=p+'.TFrame');row.pack(fill='x');self.actions={}
        for label,operation in [('Дублировать','copy'),('Изменить цену','price'),('Изменить дату','at'),('Удалить выбранный','delete')]:
            b=ttk.Button(row,text=tr(label),style=p+'.Secondary.TButton',command=lambda op=operation:self.change(op));b.pack(side='left',fill='x',expand=True,padx=(0,6));self.actions[operation]=b
        footer=ttk.Frame(base,style=p+'.TFrame');footer.pack(side='bottom',fill='x',before=actions)
        for label,operation in [('↶ Отменить','undo'),('↷ Повторить','redo')]:
            ttk.Button(footer,text=tr(label),style=p+'.Secondary.TButton',command=lambda op=operation:self.history(op)).pack(side='left',padx=(0,8))
        self.done_button=ttk.Button(footer,text=tr('✓ Готово'),style=p+'.Primary.TButton',command=win.destroy);self.done_button.pack(side='right')
        self.note=tk.StringVar(win,tr('Разметка сохраняется штатной кнопкой «Сохранить» в главном окне.'))
        ttk.Label(base,textvariable=self.note,style=p+'.TLabel',font=('Segoe UI',9),wraplength=970).pack(side='bottom',anchor='w',pady=(8,0),before=footer)
        win.bind('<Escape>',lambda _:win.destroy())
        win.bind('<Map>',lambda event:win.after_idle(lambda:native_titlebar(win,palette)) if event.widget is win else None,add='+')
        self.render()

    def valid(self):
        return self.app.instrument_cfg()['isin']==self.isin and self.app.user_state(self.app.instrument_cfg()).get('drawings',[])==self.listed

    def reject(self):self.note.set(tr('Акция или разметка изменились. Откройте окно рисования заново.'))

    def choose(self,kind):
        if not self.valid():self.reject();return
        self.app.drawing_mode=kind;self.app.drawing_start=None
        self.app.status.set(tr('Укажите ')+(tr('одну точку') if kind in ('hline','vline','marker') else tr('две точки'))+tr(' на графике.'))
        self.win.destroy()

    def render(self):
        selected=self.objects.selection();self.objects.delete(*self.objects.get_children());query=self.query.get().casefold()
        for n,obj in enumerate(self.listed):
            values=(str(n+1)+'. '+tr(NAMES.get(obj.get('type'),obj.get('type','—'))),str(obj.get('at') or '—'),str(obj.get('price') or '—'),str(obj.get('source_mode') or '—'),str(obj.get('currency') or '—'))
            if query in ' '.join(values).casefold():self.objects.insert('','end',iid=str(n),values=values)
        if selected and self.objects.exists(selected[0]):self.objects.selection_set(selected[0])
        self.count.set(tr('Список объектов')+f' ({len(self.objects.get_children())} / {len(self.listed)})');self.selection()

    def selection(self):
        selected=self.objects.selection();obj=self.listed[int(selected[0])] if selected else None
        for operation,button in self.actions.items():
            enabled=obj is not None and not(operation=='price' and obj.get('type')=='vline') and not(operation=='at' and obj.get('type')=='hline')
            button.configure(state='normal' if enabled else 'disabled')
        if obj:
            end=obj.get('end',{})
            self.detail.set(tr(NAMES.get(obj.get('type'),obj.get('type','—')))+' · '+str(obj.get('price','—'))+' · '+str(obj.get('at','—'))+
                (' → '+str(end.get('price','—'))+' · '+str(end.get('at','—')) if end else '')+'\n'+str(obj.get('source_mode','—'))+' · '+str(obj.get('currency','—')))
        else:self.detail.set(tr('Выберите объект в списке ниже'))

    def change(self,operation):
        selected=self.objects.selection()
        if not selected:return
        if not self.valid():self.reject();return
        index=int(selected[0]);value=None;identity=chart_identity(self.app)
        if operation=='price':value=simpledialog.askfloat(tr('Цена объекта'),tr('Новая цена начальной точки'),initialvalue=self.listed[index].get('price'),parent=self.win)
        elif operation=='at':value=simpledialog.askstring(tr('Дата объекта'),tr('Новая дата начальной точки'),initialvalue=self.listed[index].get('at'),parent=self.win)
        if operation in ('price','at') and value is None:return
        if not self.valid() or identity!=chart_identity(self.app):self.reject();return
        try:
            changed=modify(self.listed,index,operation,value)
            if commit(self.app,self.isin,self.listed,changed):self.listed=copy.deepcopy(changed);self.render();self.note.set(tr('Разметка изменена. Нажмите «Сохранить» в главном окне.'))
            else:self.reject()
        except (ValueError,TypeError) as error:messagebox.showerror(tr('Разметка'),rendered_message(str(error)),parent=self.win)

    def history(self,operation):
        if not self.valid():self.reject();return
        if travel(self.app,self.isin,operation):
            self.listed=copy.deepcopy(self.app.user_state(self.app.instrument_cfg()).get('drawings',[]));self.render()

    def settings(self):
        messagebox.showinfo(tr('Настройки рисования'),tr('Доступны шесть инструментов, перемещение на графике, изменение цены и даты, дублирование, удаление и история действий.\n\nЦвет, толщина, слои, шаблоны, Фибоначчи и Ганн пока не поддерживаются действующим графиком.'),parent=self.win)


def show(app):
    info=app.instrument_cfg();windows=getattr(app,'_drawing_windows',{});app._drawing_windows=windows
    existing=windows.get(info['isin'])
    if existing is not None and existing.winfo_exists():existing.lift();return existing
    app._drawing_dialog=Dialog(app,info)
    return app._drawing_dialog.win
