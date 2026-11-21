"""Presentation-only style editor using the existing drawing draft/history path."""
import copy
import tkinter as tk
from tkinter import ttk,colorchooser,messagebox
from i18n import tr
from drawing_styles import DEFAULT,LINES,effective,validate,template,save_template
from drawing_actions import modify,commit,chart_identity


class Dialog:
    def __init__(self,parent,index):
        from main_screen import native_titlebar
        self.parent=parent;self.app=parent.app;self.index=index;self.expected=copy.deepcopy(parent.listed)
        self.identity=chart_identity(self.app)
        self.templates=copy.deepcopy(self.app.user_state(self.app.instrument_cfg()).get('drawing_style_templates',[]))
        self.win=win=tk.Toplevel(parent.win);win.title(tr('Стиль выбранного объекта'));win.geometry('760x780');win.minsize(680,740)
        p=parent.p;palette=parent.palette
        style=ttk.Style(win);style.configure(p+'.Scope.TRadiobutton',background=palette['bg'],foreground=palette['text'],font=('Segoe UI',10),padding=(3,1),indicatorcolor=palette['bg'])
        style.map(p+'.Scope.TRadiobutton',indicatorcolor=[('selected',palette['accent']),('!selected',palette['bg'])],background=[('active',palette['bg'])])
        base=ttk.Frame(win,padding=16,style=p+'.TFrame');base.pack(fill='both',expand=True)
        ttk.Label(base,text=tr('Стиль выбранного объекта'),style=p+'.TLabel',font=('Segoe UI',17,'bold')).pack(anchor='w',pady=(0,10))
        obj=self.expected[index]
        ttk.Label(base,text=parent.info['name']+' · '+tr('Объект')+f' {index+1} · '+tr('Слой')+': '+str(obj.get('layer','Основной')),style=p+'.TLabel',font=('Segoe UI',10)).pack(anchor='w',pady=(0,12))
        self.scope=tk.StringVar(win,'Выбранному')
        scopes=ttk.Frame(base,style=p+'.TFrame');scopes.pack(fill='x',pady=(0,12))
        ttk.Label(scopes,text=tr('Применить стиль к:'),style=p+'.TLabel',font=('Segoe UI',10)).pack(side='left',padx=(0,12))
        for text in ('Выбранному','Слою','Всем'):
            ttk.Radiobutton(scopes,text=tr(text),variable=self.scope,value=text,style=p+'.Scope.TRadiobutton').pack(side='left',padx=8)
        grid=ttk.Frame(base,padding=12,style=p+'.Info.TFrame');grid.pack(fill='x')
        grid.columnconfigure(1,weight=1);self.values={};self.entries={}
        for row,(key,label) in enumerate([('color','Цвет линии'),('width','Толщина'),('linestyle','Тип линии'),('opacity','Непрозрачность'),('fill','Заливка фигур'),('fill_opacity','Непрозрачность заливки'),('fontsize','Размер текста и меток')]):
            ttk.Label(grid,text=tr(label),style=p+'.Drawing.TLabel',font=('Segoe UI',11)).grid(row=row,column=0,sticky='w',pady=6,padx=(0,20))
            field=ttk.Frame(grid,style=p+'.TFrame');field.grid(row=row,column=1,sticky='ew',pady=6)
            v=tk.StringVar(win);self.values[key]=v
            if key=='linestyle':control=ttk.Combobox(field,textvariable=v,values=list(LINES),state='readonly',width=24)
            else:control=ttk.Entry(field,textvariable=v,style=p+'.TEntry',font=('Segoe UI',11),width=24)
            control.pack(side='left',fill='x',expand=True);self.entries[key]=control
            if key in ('color','fill'):
                ttk.Button(field,text=tr('Выбрать'),command=lambda k=key:self.color(k)).pack(side='right',padx=(7,0))
        self.label=tk.BooleanVar(win)
        ttk.Checkbutton(grid,text=tr('Показывать цены в метках'),variable=self.label,style=p+'.TCheckbutton').grid(row=7,column=0,columnspan=2,sticky='w',pady=8)
        ttk.Label(base,text=tr('Непрозрачность: 0,05–1; заливка: 0–1. Цены в метках: маркер и Фибоначчи. Стили не меняют свечи и торговые уровни.'),style=p+'.TLabel',font=('Segoe UI',9),wraplength=690).pack(anchor='w',pady=9)
        saved=ttk.Frame(base,padding=12,style=p+'.Info.TFrame');saved.pack(fill='x',pady=(0,8))
        ttk.Label(saved,text=tr('Шаблоны стиля • эта акция'),style=p+'.Drawing.TLabel',font=('Segoe UI',11,'bold')).pack(anchor='w',pady=(0,6))
        row=ttk.Frame(saved,style=p+'.TFrame');row.pack(fill='x')
        self.preset=tk.StringVar(win);self.preset_box=ttk.Combobox(row,textvariable=self.preset,values=[t.get('name','') for t in self.templates[:20] if isinstance(t,dict)] if isinstance(self.templates,list) else [],state='readonly',width=22);self.preset_box.pack(side='left',fill='x',expand=True)
        ttk.Button(row,text=tr('Загрузить'),command=self.load).pack(side='left',padx=6)
        self.name=tk.StringVar(win);ttk.Entry(row,textvariable=self.name,style=p+'.TEntry',width=18,font=('Segoe UI',10)).pack(side='left')
        ttk.Button(row,text=tr('Сохранить шаблон'),command=self.save).pack(side='left',padx=(6,0))
        self.note=tk.StringVar(win,tr('Загрузка шаблона не меняет объект до нажатия «Применить».'))
        ttk.Label(base,textvariable=self.note,style=p+'.TLabel',font=('Segoe UI',9),wraplength=690).pack(fill='x',pady=5)
        footer=ttk.Frame(base,style=p+'.TFrame');footer.pack(side='bottom',fill='x',pady=(8,0),before=base.winfo_children()[0])
        ttk.Button(footer,text=tr('По умолчанию'),command=lambda:self.fill(DEFAULT),style=p+'.Secondary.TButton').pack(side='left')
        self.apply_button=ttk.Button(footer,text=tr('✓ Применить'),command=self.apply,style=p+'.Primary.TButton');self.apply_button.pack(side='right')
        ttk.Button(footer,text=tr('Отмена'),command=win.destroy,style=p+'.Secondary.TButton').pack(side='right',padx=8)
        self.fill(effective(obj,palette['accent']))
        win.bind('<Escape>',lambda _:win.destroy())
        win.bind('<Map>',lambda event:win.after_idle(lambda:native_titlebar(win,palette)) if event.widget is win else None,add='+')

    def fill(self,value):
        value=validate(value)
        for key,v in self.values.items():v.set(next(k for k,n in LINES.items() if n==value[key]) if key=='linestyle' else str(value[key]))
        self.label.set(value['price_label'])

    def read(self):
        value={key:(LINES[v.get()] if key=='linestyle' else v.get() if key in ('color','fill') else float(v.get().replace(',','.'))) for key,v in self.values.items()}
        value['price_label']=self.label.get();return validate(value)

    def valid(self):
        return self.parent.win.winfo_exists() and self.parent.valid() and self.parent.listed==self.expected and chart_identity(self.app)==self.identity

    def reject(self):self.note.set(tr('Акция, график или разметка изменились. Откройте стиль заново.'))

    def color(self,key):
        try:
            value=colorchooser.askcolor(color=self.values[key].get(),parent=self.win,title=tr('Цвет'))[1]
            if value:self.values[key].set(value)
        except tk.TclError:self.note.set(tr('Исправьте цвет: требуется значение вида #00c5ff.'))

    def apply(self):
        if not self.valid():self.reject();return
        try:
            style=self.read();layer=self.expected[self.index].get('layer','Основной')
            indices=[self.index] if self.scope.get()=='Выбранному' else [n for n,obj in enumerate(self.expected) if self.scope.get()=='Всем' or obj.get('layer','Основной')==layer]
            changed=copy.deepcopy(self.expected)
            for index in indices:changed=modify(changed,index,'style',style)
            if not self.valid():self.reject();return
            if commit(self.app,self.parent.isin,self.expected,changed):
                self.parent.listed=copy.deepcopy(changed);self.parent.render();self.parent.note.set(tr('Стиль изменён. Нажмите «Сохранить» в главном окне.'));self.win.destroy()
            else:self.reject()
        except (ValueError,KeyError) as error:self.note.set(str(error))

    def save(self):
        if not self.valid():self.reject();return
        try:
            item=template(self.name.get(),self.read())
            if save_template(self.app,self.parent.isin,self.expected,self.templates,item):
                self.templates=copy.deepcopy(self.app.user_state(self.app.instrument_cfg())['drawing_style_templates'])
                self.preset_box.configure(values=[t['name'] for t in self.templates]);self.preset.set(item['name'])
                self.note.set(tr('Шаблон добавлен в черновик. Нажмите «Сохранить» в главном окне.'))
            else:self.reject()
        except (ValueError,KeyError,TypeError) as error:self.note.set(str(error))

    def load(self):
        item=next((t for t in self.templates[:20] if isinstance(t,dict) and t.get('name')==self.preset.get()),None) if isinstance(self.templates,list) else None
        if item is None:return
        try:self.fill(item['style']);self.name.set(item['name']);self.note.set(tr('Шаблон загружен в поля. Для изменения объекта нажмите «Применить».'))
        except (ValueError,KeyError,TypeError) as error:self.note.set(str(error))
