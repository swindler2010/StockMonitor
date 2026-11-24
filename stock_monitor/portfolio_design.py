"""Read-only portfolio presentation around existing PAPER controls and ledger."""
import tkinter as tk
from tkinter import ttk
from decimal import Decimal, InvalidOperation
from i18n import tr, language
from financial_format import format_value
from themes import THEMES
from reference_design import Card, wrap_label


def metrics(state):
    """No estimated equity or P&L when the account projection has no valid value."""
    result={key:state.get(key) for key in ('equity','cash','fees')}
    try:
        equity=Decimal(str(state.get('equity')))
        start=Decimal(str(state.get('policy',{}).get('starting_cash')))
        result['return']=(equity/start-1) if equity.is_finite() and start.is_finite() and start>0 else None
    except (InvalidOperation,ValueError,ZeroDivisionError):result['return']=None
    if state.get('status')!='ACTIVE':result={key:None for key in (*result,'return')}
    return result


class Surface:
    def __init__(self,app,parent):
        from indicator_dialog import styles
        from ui_icons import photo
        self.parent=parent;self.root=app.root
        self.palette=THEMES.get(app.store.setting('theme','Тёмная'),THEMES['Тёмная'])
        self.p=p=styles(app.root,self.palette)
        style=ttk.Style(parent)
        field='#0a2136' if self.palette['bg']!='#f8fafc' else '#ffffff'
        style.configure(p+'.Paper.TLabel',background=style.lookup(p+'.TFrame','background'),foreground=self.palette['text'],font=('Segoe UI',10))
        style.configure(p+'.Drawing.TLabel',background=field,foreground=self.palette['text'])
        style.configure(p+'.Card.TLabel',background=field,foreground=self.palette['text'],font=('Segoe UI',10))
        style.configure(p+'.Paper.Treeview',background=style.lookup(p+'.TFrame','background'),fieldbackground=style.lookup(p+'.TFrame','background'),foreground=self.palette['text'],font=('Segoe UI',10),rowheight=28,borderwidth=0,relief='flat',bordercolor=self.palette['grid'],lightcolor=self.palette['bg'],darkcolor=self.palette['bg'])
        style.configure(p+'.Paper.Treeview.Heading',background=self.palette['panel'],foreground=self.palette['text'],font=('Segoe UI',10,'bold'),padding=(7,6),relief='flat')
        style.layout(p+'.Paper.Treeview.Heading',[('Treeheading.padding',{'sticky':'nswe','children':[('Treeheading.text',{'sticky':'we'})]})])
        style.configure(p+'.Horizontal.TScrollbar',background=self.palette['grid'],troughcolor=self.palette['bg'],bordercolor=self.palette['bg'],lightcolor=self.palette['bg'],darkcolor=self.palette['bg'],arrowcolor=self.palette['muted'],width=10,arrowsize=10)
        style.map(p+'.Paper.Treeview',background=[('selected','#075585')],foreground=[('selected','#ffffff')])
        style.configure(p+'.Paper.TNotebook',background=style.lookup(p+'.TFrame','background'),borderwidth=0,tabmargins=(0,4,0,3))
        style.configure(p+'.Paper.TNotebook.Tab',padding=(12,7),font=('Segoe UI',10),background=self.palette['panel'])
        style.map(p+'.Paper.TNotebook.Tab',background=[('selected','#005b8c'),('active',self.palette['grid'])],foreground=[('selected','#ffffff')])
        parent.configure(style=p+'.TFrame')
        heading=ttk.Frame(parent,style=p+'.TFrame',padding=(12,8));heading.pack(fill='x')
        ttk.Label(heading,image=photo(app.root,'portfolio',27,color='#00c5ff'),style=p+'.Paper.TLabel').pack(side='left',padx=(0,9))
        self.title=tk.StringVar(parent,tr('Демо — виртуальная торговля'))
        ttk.Label(heading,textvariable=self.title,style=p+'.Paper.TLabel',font=('Segoe UI',15,'bold')).pack(side='left')
        strip=ttk.Frame(parent,style=p+'.TFrame');strip.pack(fill='x',padx=10,pady=(0,5))
        self.values={}
        for column,(key,label) in enumerate((('equity','Капитал'),('cash','Свободно'),('return','Доходность'),('fees','Комиссии'))):
            box=Card(strip,label,p);box.grid(row=0,column=column,sticky='nsew',padx=(0,6) if column<3 else 0)
            strip.columnconfigure(column,weight=1,uniform='paper-kpi')
            variable=tk.StringVar(parent,'—');self.values[key]=variable
            ttk.Label(box,textvariable=variable,style=p+'.Drawing.TLabel',font=('Segoe UI',14,'bold')).pack(anchor='w')

    def card(self,parent,text):return Card(parent,text,self.p)

    def bind(self,state):
        self.title.set(tr('Демо — виртуальная торговля')+' · '+str(state.get('account') or '—'))
        for key,value in metrics(state).items():
            self.values[key].set('—' if value is None else format_value(value,language(),kind='percent' if key=='return' else 'amount',currency=None if key=='return' else 'EUR'))

    def finish(self, parent=None):
        """Style the same widgets in place; never replace their commands or bindings."""
        p=self.p;style=ttk.Style(self.parent)
        def visit(widget):
            current=str(widget.cget('style')) if isinstance(widget,ttk.Widget) else ''
            if not current:
                target=(p+'.Paper.Treeview' if isinstance(widget,ttk.Treeview) else
                        p+'.Paper.TNotebook' if isinstance(widget,ttk.Notebook) else
                        p+'.TCombobox' if isinstance(widget,ttk.Combobox) else
                        p+('.Vertical.TScrollbar' if str(widget.cget('orient'))=='vertical' else '.Horizontal.TScrollbar') if isinstance(widget,ttk.Scrollbar) else
                        p+'.TEntry' if isinstance(widget,ttk.Entry) else
                        p+'.Secondary.TButton' if isinstance(widget,ttk.Button) else
                        p+('.Card.TLabel' if isinstance(widget.master,Card) else '.Paper.TLabel') if isinstance(widget,ttk.Label) else
                        p+'.TFrame' if isinstance(widget,ttk.Frame) else None)
                if target:widget.configure(style=target)
            if isinstance(widget,(ttk.Label,ttk.Button,ttk.Checkbutton)):
                text=widget.cget('text')
                if text:widget.configure(text=tr(text))
            if isinstance(widget,ttk.Treeview):
                from tkinter.font import Font
                heading_font=Font(widget,font=style.lookup(p+'.Paper.Treeview.Heading','font'))
                for column in widget['columns']:
                    text=tr(widget.heading(column,'text'));widget.heading(column,text=text)
                    widget.column(column,width=max(int(widget.column(column,'width')),heading_font.measure(text)+22))
            if isinstance(widget,ttk.Notebook):
                for tab in widget.tabs():widget.tab(tab,text=tr(widget.tab(tab,'text')))
            if isinstance(widget,tk.Text):
                widget.configure(background=style.lookup(p+'.TFrame','background'),foreground=self.palette['text'],relief='flat',borderwidth=0,highlightthickness=0,font=('Segoe UI',10),padx=9,pady=6,spacing3=3)
            if isinstance(widget,ttk.Label) and int(float(widget.cget('wraplength') or 0))>0:wrap_label(widget)
            for child in widget.winfo_children():visit(child)
        visit(parent or self.parent)


def window(app,win,tabs):
    from main_screen import native_titlebar
    from design_layout import fit_window
    from ui_icons import photo
    palette=THEMES.get(app.store.setting('theme','Тёмная'),THEMES['Тёмная'])
    win.configure(background=palette['bg'])
    fit_window(win,1240,850,(900,700))
    win.iconphoto(False,photo(app.root,'portfolio',32,color='#00c5ff'))
    win.bind('<Map>',lambda e:win.after_idle(lambda:native_titlebar(win,palette)) if e.widget is win else None,add='+')
    win.bind('<Escape>',lambda _:win.destroy())
    win.after_idle(lambda:next((child._paper_design.finish(win) for child in tabs.winfo_children() if hasattr(child,'_paper_design')),None) if win.winfo_exists() else None)

