"""Scrollable drawing catalogue and UI-only shared, asynchronous favourites."""
import tkinter as tk
from tkinter import ttk
from i18n import tr
from drawing_tools import GROUPS, REGISTRY
from ui_icons import photo

KEY = 'ui_drawing_favorites'
DEFAULT = ['trend', 'hline', 'channel', 'fib', 'zone', 'text', 'measure']


def clean(value):
    if not isinstance(value, list): return list(DEFAULT)
    return list(dict.fromkeys(k for k in value if isinstance(k, str) and k in REGISTRY))[:12]


class Favorites:
    def __init__(self, app):
        self.app=app; self.items=list(DEFAULT); self.loaded=False; self.busy=False
        self.views=[]; self.sequence=0; self.pending=False; self.error=False

    def refresh(self):
        if getattr(self.app,'closing',False): return
        self.views=[v for v in self.views if v.host.winfo_exists()]
        for view in self.views: view.paint_favorites()

    def load(self):
        if self.loaded or self.busy or not hasattr(self.app, 'job'): return
        self.busy=True
        def done(value):
            self.busy=False; self.error=False; self.items=clean(value); self.loaded=True; self.refresh()
        def fail(_):
            self.busy=False; self.error=True; self.refresh()
        if self.app.job(lambda:self.app.store.setting(KEY,DEFAULT),done,fail,
                        lane='read',key='drawing-favorites-read') is False: fail(None)

    def toggle(self, kind):
        if not self.loaded or self.busy or kind not in REGISTRY: return
        if kind in self.items: self.items.remove(kind)
        elif len(self.items)<12: self.items.append(kind)
        else: return
        self.save()

    def save(self):
        if self.busy or getattr(self.app,'closing',False): return
        captured=list(self.items); self.busy=True; self.error=False; self.sequence+=1
        self.refresh()
        def done(_): self.busy=False; self.pending=False; self.refresh()
        def fail(_): self.busy=False; self.pending=True; self.error=True; self.refresh()
        if self.app.job(lambda:self.app.store.set_setting(KEY,captured),done,fail,
                        lane='read',key='drawing-favorites-save:'+str(self.sequence)) is False: fail(None)


class Gallery:
    def __init__(self, dialog, parent):
        self.dialog=dialog; self.app=dialog.app; self.p=dialog.p; self.palette=dialog.palette
        self.host=ttk.Frame(parent,height=310,style=self.p+'.TFrame'); self.host.pack(fill='x'); self.host.pack_propagate(False)
        self.editing=False; self.collapsed=set(); self.sections={}; self.star_buttons={}
        style=ttk.Style(self.host)
        style.layout(self.p+'.Favorite.TButton',style.layout(self.p+'.Secondary.TButton'))
        style.configure(self.p+'.Favorite.TButton',foreground='#ffd75e',padding=(4,2))
        manager=getattr(self.app,'_drawing_favorites',None)
        if manager is None: self.app._drawing_favorites=manager=Favorites(self.app)
        self.favorites=manager; manager.views.append(self)
        self.host.bind('<Destroy>',self.release,add='+')
        self.quick=ttk.Frame(self.host,padding=5,style=self.p+'.Info.TFrame'); self.quick.pack(side='left',fill='y',padx=(0,8))
        ttk.Label(self.quick,text=tr('Быстрый доступ'),style=self.p+'.Drawing.TLabel',font=('Segoe UI',9,'bold')).pack(pady=(0,4))
        self.quick_list=ttk.Frame(self.quick,style=self.p+'.TFrame'); self.quick_list.pack(fill='both',expand=True)
        self.edit=ttk.Button(self.quick,text=tr('Редактировать избранное').replace(' ','\n',1),width=15,command=self.edit_favorites,style=self.p+'.Secondary.TButton')
        self.edit.pack(side='bottom',fill='x',pady=(4,0))
        self.quick_note=tk.StringVar(self.host)
        ttk.Label(self.quick,textvariable=self.quick_note,wraplength=122,style=self.p+'.Drawing.TLabel',font=('Segoe UI',8)).pack(side='bottom')
        area=ttk.Frame(self.host,style=self.p+'.TFrame'); area.pack(side='left',fill='both',expand=True)
        self.canvas=tk.Canvas(area,height=210,highlightthickness=0,background=self.palette['bg'])
        scroll=ttk.Scrollbar(area,command=self.canvas.yview,style=self.p+'.Vertical.TScrollbar'); scroll.pack(side='right',fill='y')
        self.canvas.configure(yscrollcommand=scroll.set); self.canvas.pack(fill='both',expand=True)
        self.body=ttk.Frame(self.canvas,style=self.p+'.TFrame'); self.window=self.canvas.create_window(0,0,window=self.body,anchor='nw')
        self.canvas.bind('<Configure>',lambda e:self.canvas.itemconfigure(self.window,width=e.width))
        self.body.bind('<Configure>',lambda _:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        dialog.tool_buttons={}
        for group,items in GROUPS.items():
            section=ttk.Frame(self.body,padding=6,style=self.p+'.Info.TFrame'); section.pack(fill='x',pady=(0,6))
            heading=ttk.Button(section,text='-  '+tr(group),command=lambda g=group:self.collapse(g),style=self.p+'.Secondary.TButton')
            heading.pack(fill='x',pady=(0,5))
            grid=ttk.Frame(section,style=self.p+'.TFrame'); grid.pack(fill='x')
            for n in range(6): grid.columnconfigure(n,weight=1,uniform='tools')
            for n,(label,kind,count) in enumerate(items):
                tile=ttk.Frame(grid,style=self.p+'.TFrame'); tile.grid(row=n//6,column=n%6,sticky='nsew',padx=2,pady=2)
                button=ttk.Button(tile,text=tr(label).replace(' ','\n',1),image=photo(self.app.root,'drawing_'+kind,23,self.palette['text']),
                    compound='top',style=self.p+'.Tool.TButton',command=lambda k=kind:dialog.choose(k))
                button.pack(fill='both',expand=True); dialog.tool_buttons[kind]=button
                star=ttk.Button(tile,width=2,command=lambda k=kind:self.favorites.toggle(k),style=self.p+'.Secondary.TButton')
                self.star_buttons[kind]=star
                self.app.tooltip(button,tr(label))
                for widget in (tile,button,star): widget.bind('<MouseWheel>',self.wheel,add='+')
            self.sections[group]=(section,heading,grid)
            for widget in (section,heading,grid): widget.bind('<MouseWheel>',self.wheel,add='+')
        self.canvas.bind('<MouseWheel>',self.wheel); self.body.bind('<MouseWheel>',self.wheel)
        self.paint_favorites(); manager.load()

    def release(self,event):
        if event.widget is self.host:
            self.favorites.views=[view for view in self.favorites.views if view is not self]

    def wheel(self,event):
        if event.delta: self.canvas.yview_scroll(-1 if event.delta>0 else 1,'units')
        return 'break'

    def collapse(self,group):
        if group in self.collapsed:self.collapsed.remove(group)
        else:self.collapsed.add(group)
        self.filter(self.dialog.tool_group.get())

    def filter(self,group):
        for name,(section,heading,grid) in self.sections.items():
            section.pack_forget()
            if group not in ('Все группы',name):continue
            section.pack(fill='x',pady=(0,6))
            heading.configure(text=('+  ' if name in self.collapsed else '-  ')+tr(name))
            grid.pack_forget()
            if name not in self.collapsed:grid.pack(fill='x')
        self.canvas.yview_moveto(0)

    def edit_favorites(self):
        if self.favorites.error:
            if self.favorites.loaded:self.favorites.save()
            else:self.favorites.load()
            return
        self.editing=not self.editing
        self.dialog.tool_group.set('Все группы')
        self.paint_favorites()

    def paint_favorites(self):
        manager=self.favorites
        for child in self.quick_list.winfo_children():child.destroy()
        for n,kind in enumerate(manager.items):
            button=ttk.Button(self.quick_list,image=photo(self.app.root,'drawing_'+kind,20,self.palette['text']),text='★',compound='left',
                command=lambda k=kind:self.dialog.choose(k),style=self.p+'.Favorite.TButton',width=4)
            button.grid(row=n//2,column=n%2,padx=2,pady=2,sticky='ew'); self.app.tooltip(button,tr(REGISTRY[kind]['name']))
        self.edit.configure(text=tr('Повторить сохранение' if manager.error and manager.loaded else 'Повторить загрузку' if manager.error else 'Готово' if self.editing else 'Редактировать избранное'),
                            state='disabled' if manager.busy or not manager.loaded and not manager.error else 'normal')
        if not self.editing and not manager.error:self.edit.configure(text=tr('Редактировать избранное').replace(' ','\n',1))
        self.quick_note.set(tr('Сохранение избранного…' if manager.busy and manager.loaded else 'Загрузка…' if manager.busy else 'Избранное не сохранено' if manager.pending else 'Не удалось загрузить избранное' if manager.error else 'До 12 инструментов' if self.editing else ''))
        for kind,button in self.star_buttons.items():
            button.configure(text='★' if kind in manager.items else '☆',state='disabled' if manager.busy or not manager.loaded or len(manager.items)>=12 and kind not in manager.items else 'normal')
            if self.editing:button.pack(fill='x')
            else:button.pack_forget()
