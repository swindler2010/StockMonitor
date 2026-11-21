"""Indicator dialog presentation. Validation and saving remain in Desktop014."""
import tkinter as tk
from tkinter import ttk

from i18n import tr
from themes import THEMES


LABELS = {'ema_fast':'EMA быстрая','ema_slow':'EMA медленная','sma':'Период SMA',
          'rsi':'Период RSI','atr':'Период ATR','bb':'Период Боллинджера',
          'bb_dev':'Отклонение Боллинджера','volume':'Период среднего объёма',
          'macd_fast':'MACD быстрый','macd_slow':'MACD медленный','macd_signal':'MACD сигнальный'}
ORDER = ('EMA','SMA','Bollinger','VWAP','RSI','MACD','ATR','Объём','Средний объём',
         'Относительный объём',*LABELS,'Volume Profile','Уровни анализа')


def styles(root,palette):
    """Own ttk elements only; never replace styles of another open dialog."""
    from PIL import Image,ImageDraw,ImageTk
    key=tuple(palette.values())
    cache=getattr(root,'_indicator_dialog_styles',None)
    if cache is None:root._indicator_dialog_styles=cache={}
    if key in cache:return cache[key][0]
    prefix='IndicatorDesign'+str(len(cache))
    style=ttk.Style(root)
    dark=palette['bg']!='#f8fafc'
    bg='#031221' if dark else palette['bg']
    field='#0a2136' if dark else '#ffffff'
    text=palette['text'];outline='#477797' if dark else '#7892ad'
    accent='#00c5ff' if dark else palette['accent']
    style.configure(prefix+'.TFrame',background=bg)
    style.configure(prefix+'.TLabel',background=bg,foreground=text,font=('Segoe UI',12))
    style.configure(prefix+'.TEntry',fieldbackground=field,foreground=text,insertcolor=text,
                    bordercolor=outline,lightcolor=field,darkcolor=field,padding=(7,1),font=('Segoe UI',12))
    style.map(prefix+'.TEntry',bordercolor=[('focus',accent)])
    style.configure(prefix+'.Vertical.TScrollbar',background=outline,troughcolor=bg,bordercolor=bg,
                    lightcolor=bg,darkcolor=bg,arrowcolor=text,width=13,arrowsize=13)
    style.map(prefix+'.Vertical.TScrollbar',background=[('active',accent)])
    photos=[]
    for selected in (False,True):
        bitmap=Image.new('RGBA',(96,96));draw=ImageDraw.Draw(bitmap)
        draw.rounded_rectangle((7,7,89,89),radius=13,fill='#075585' if selected else field,
                               outline=accent if selected else '#a8bdcc',width=6)
        if selected:draw.line(((25,48),(41,65),(73,29)),fill='#ffffff',width=10,joint='curve')
        photos.append(ImageTk.PhotoImage(bitmap.resize((22,22),Image.Resampling.LANCZOS),master=root))
    check=prefix+'.indicator'
    style.element_create(check,'image',photos[0],('selected',photos[1]),width=24,sticky='w')
    style.layout(prefix+'.TCheckbutton',[(check,{'side':'left','sticky':'w'}),
        ('Checkbutton.padding',{'sticky':'nsew','children':[('Checkbutton.label',{'sticky':'w'})]})])
    style.configure(prefix+'.TCheckbutton',background=bg,foreground=text,font=('Segoe UI',12),padding=(4,3))
    style.map(prefix+'.TCheckbutton',foreground=[('disabled',palette['muted']),('!disabled',text)])
    style.map(prefix+'.TCheckbutton',background=[('active',bg),('!active',bg)])
    for name,fill,border in (('Secondary',field,outline),('Primary','#006bb7' if dark else '#1769aa',accent),('Info',field,outline),('Field',field,outline)):
        bitmap=Image.new('RGBA',(96,96));draw=ImageDraw.Draw(bitmap)
        # Keep each curve inside the fixed nine-slice corner. A larger curve
        # would repeat along the stretched edge of compact input fields.
        radius=9 if name=='Field' else 15 if name in ('Primary','Secondary') else 23
        draw.rounded_rectangle((2,2,93,93),radius=radius,fill=fill,outline=border,width=4)
        photo=ImageTk.PhotoImage(bitmap.resize((32,32),Image.Resampling.LANCZOS),master=root);photos.append(photo)
        element=prefix+name
        style.element_create(element,'image',photo,border=4 if name=='Field' else 6 if name in ('Primary','Secondary') else 9,sticky='nsew')
        if name=='Info':style.layout(prefix+'.Info.TFrame',[(element,{'sticky':'nsew'})])
        elif name=='Field':
            style.layout(prefix+'.TEntry',[(element,{'sticky':'nsew','children':[
                ('Entry.padding',{'sticky':'nsew','children':[('Entry.textarea',{'sticky':'nsew'})]})]})])
        else:
            style.layout(prefix+'.'+name+'.TButton',[(element,{'sticky':'nsew','children':[
                ('Button.padding',{'sticky':'nsew','children':[('Button.label',{'sticky':'nsew'})]})]})])
            style.configure(prefix+'.'+name+'.TButton',foreground='#ffffff' if name=='Primary' else text,
                            padding=(12,7),font=('Segoe UI',11,'bold'))
    cache[key]=(prefix,photos)
    return prefix


class Dialog:
    def __init__(self,app,settings):
        from main_screen import native_titlebar
        from ui_icons import photo
        self.app=app
        self.palette=THEMES.get(app.store.setting('theme','Тёмная'),THEMES['Тёмная'])
        root=app.root
        self.win=win=tk.Toplevel(root)
        win.title(tr('Индикаторы • Все акции'))
        width=min(660,max(500,win.winfo_screenwidth()-80))
        height=min(870,max(560,win.winfo_screenheight()-100))
        win.geometry(f'{width}x{height}')
        win.minsize(500,500)
        win.iconphoto(False,photo(root,'indicators',32,color='#00c5ff'))
        self.prefix=p=styles(root,self.palette)
        bg=ttk.Style(root).lookup(p+'.TFrame','background')
        win.configure(background=bg)
        outer=ttk.Frame(win,padding=8,style=p+'.TFrame');outer.pack(fill='both',expand=True)
        self.actions=ttk.Frame(outer,padding=(7,10),style=p+'.TFrame')
        self.actions.pack(side='bottom',fill='x')
        ttk.Separator(outer).pack(side='bottom',fill='x',pady=2)
        container=ttk.Frame(outer,style=p+'.TFrame');container.pack(fill='both',expand=True)
        self.canvas=canvas=tk.Canvas(container,highlightthickness=0,background=bg)
        scroll=ttk.Scrollbar(container,orient='vertical',command=canvas.yview,style=p+'.Vertical.TScrollbar')
        scroll.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        self.body=body=ttk.Frame(canvas,padding=(14,8,18,12),style=p+'.TFrame')
        window=canvas.create_window((0,0),window=body,anchor='nw')
        body.columnconfigure(0,weight=3);body.columnconfigure(1,weight=2)
        body.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(window,width=event.width))
        self.values={};self.fields={}
        # Explicit display order, identical to the selected reference; settings
        # keys and defaults still belong to the existing indicator controller.
        keys=[k for k in ORDER if k in settings]+[k for k in settings if k not in ORDER]
        for row,key in enumerate(keys):
            value=settings[key]
            if isinstance(value,bool):
                variable=tk.BooleanVar(win,value=value)
                widget=ttk.Checkbutton(body,text=tr(key),variable=variable,style=p+'.TCheckbutton')
                widget.grid(row=row,column=0,columnspan=2,sticky='w')
            else:
                variable=tk.StringVar(win,value=str(value))
                ttk.Label(body,text=tr(LABELS.get(key,key)),style=p+'.TLabel').grid(row=row,column=0,sticky='w')
                widget=ttk.Entry(body,textvariable=variable,width=10,style=p+'.TEntry',font=('Segoe UI',12))
                widget.grid(row=row,column=1,sticky='w',padx=(12,0))
            self.values[key]=variable;self.fields[key]=widget
            widget.bind('<FocusIn>',lambda _,target=widget:self.reveal(target),add='+')
        note=ttk.Frame(body,padding=(12,10),style=p+'.Info.TFrame')
        note.grid(row=len(keys),column=0,columnspan=2,sticky='ew',pady=(16,4))
        ttk.Label(note,text='ⓘ',foreground='#00c5ff',style=p+'.TLabel',font=('Segoe UI',22)).pack(side='left',padx=(0,10))
        label=ttk.Label(note,text=tr('Настройки графика. Сценарии используют EMA 10/30, RSI/ATR 14, MACD 12/26/9 и средний объём 20.'),
                        style=p+'.TLabel',wraplength=500,justify='left')
        label.pack(fill='x',expand=True)
        note.bind('<Configure>',lambda event:label.configure(wraplength=max(160,event.width-65)))
        def wheel(event):
            if canvas.bbox('all') and canvas.bbox('all')[3]>canvas.winfo_height():
                canvas.yview_scroll(-int(event.delta/120) or (-1 if event.delta>0 else 1),'units')
                return 'break'
        win.bind('<MouseWheel>',wheel,add='+')
        win.bind('<Escape>',lambda _:win.destroy())
        win.after_idle(lambda:native_titlebar(win,self.palette))

    def reveal(self,widget):
        self.win.update_idletasks()
        top=self.canvas.canvasy(0);bottom=top+self.canvas.winfo_height()
        y=widget.winfo_y();height=widget.winfo_height();total=max(1,self.body.winfo_height())
        if y<top:self.canvas.yview_moveto(max(0,(y-8)/total))
        elif y+height>bottom:self.canvas.yview_moveto(max(0,(y+height-self.canvas.winfo_height()+12)/total))

    def wire(self,apply,defaults):
        from ui_icons import photo
        p=self.prefix
        self.apply_button=ttk.Button(self.actions,text=tr('✓ Применить'),command=apply,style=p+'.Primary.TButton')
        self.apply_button.pack(side='right')
        self.defaults_button=ttk.Button(self.actions,text=tr('По умолчанию'),image=photo(self.app.root,'reset',20,color=self.palette['text']),compound='left',command=defaults,style=p+'.Secondary.TButton')
        self.defaults_button.pack(side='left')
        return self.win
