"""Manual what-if arithmetic only. No account reads, settings writes or orders."""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import tkinter as tk
from tkinter import ttk
from i18n import tr,language
from financial_format import format_value
from localized_widgets import CanonicalCombobox

FIELDS=(('capital','Модельный капитал','10000'),('cash','Доступные средства модели','10000'),('risk_pct','Риск модели, %','1'),
        ('entry','Цена входа','100'),('stop','Цена стопа','95'),('target','Цена цели','110'),
        ('step','Шаг количества','1'),('unit_cost','Издержки полного цикла на акцию','0'),('fixed_cost','Фиксированные издержки полного цикла','0'))


def number(value):
    try:
        if isinstance(value,bool) or len(str(value))>40:raise ValueError()
        result=Decimal(str(value).strip().replace(',','.'))
        if not result.is_finite() or abs(result)>Decimal('1e12') or (result and abs(result)<Decimal('1e-8')):raise ValueError()
        return result
    except (InvalidOperation,ValueError):raise ValueError('Введите конечные числа в пределах модели') from None


def calculate(values,direction='long'):
    v={key:number(values[key]) for key,_,_ in FIELDS}
    if direction not in ('long','short'):raise ValueError('Неверное направление модели')
    if any(v[k]<=0 for k in ('capital','risk_pct','entry','stop','target','step')) or any(v[k]<0 for k in ('cash','unit_cost','fixed_cost')) or v['risk_pct']>100:
        raise ValueError('Проверьте положительные цены, капитал, шаг и риск 0–100%')
    entry,stop,target=v['entry'],v['stop'],v['target']
    if not ((stop<entry<target) if direction=='long' else (target<entry<stop)):
        raise ValueError('Long: стоп < вход < цель. Short: цель < вход < стоп.')
    budget=v['capital']*v['risk_pct']/100
    per_share=abs(entry-stop)+v['unit_cost']
    by_risk=max(Decimal(0),budget-v['fixed_cost'])/per_share
    by_cash=max(Decimal(0),v['cash']-v['fixed_cost'])/(entry+v['unit_cost'])
    quantity=(min(by_risk,by_cash,Decimal('1e12'))/v['step']).to_integral_value(rounding=ROUND_FLOOR)*v['step']
    costs=quantity*v['unit_cost']+v['fixed_cost'] if quantity else Decimal(0)
    loss=quantity*abs(entry-stop)+costs
    reward=quantity*abs(target-entry)-costs
    reserved=quantity*entry+costs
    return dict(quantity=quantity,budget=budget,loss=loss,reward=reward,costs=costs,notional=quantity*entry,reserved=reserved,
                rr=reward/loss if loss else None,actual_risk=loss/v['capital'],direction=direction)


class Dialog:
    def __init__(self,app):
        from indicator_dialog import styles
        from themes import THEMES
        from main_screen import native_titlebar
        self.win=win=tk.Toplevel(app.root);app._position_calculator=win
        win.title(tr('Позиционный калькулятор — модель'));win.geometry('720x730');win.minsize(650,700)
        palette=THEMES.get(app.store.setting('theme','Тёмная'),THEMES['Тёмная']);p=styles(app.root,palette)
        base=ttk.Frame(win,style=p+'.TFrame');base.pack(fill='both',expand=True)
        footer=ttk.Frame(base,padding=(16,10),style=p+'.TFrame');footer.pack(side='bottom',fill='x')
        canvas=tk.Canvas(base,background=palette['bg'],highlightthickness=0)
        scrollbar=ttk.Scrollbar(base,command=canvas.yview);scrollbar.pack(side='right',fill='y')
        canvas.configure(yscrollcommand=scrollbar.set);canvas.pack(fill='both',expand=True)
        body=ttk.Frame(canvas,padding=16,style=p+'.TFrame');window=canvas.create_window(0,0,window=body,anchor='nw')
        def resized(event):
            canvas.itemconfigure(window,width=event.width)
            for widget in body.winfo_children():
                if isinstance(widget,ttk.Label):widget.configure(wraplength=max(300,event.width-40))
        canvas.bind('<Configure>',resized)
        body.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
        win.bind('<MouseWheel>',lambda e:canvas.yview_scroll(-int(e.delta/120),'units'))
        ttk.Label(body,text=tr('Позиционный калькулятор'),style=p+'.TLabel',font=('Segoe UI',17,'bold')).pack(anchor='w')
        ttk.Label(body,text=tr('Только ручная модель. Примеры не являются котировкой. Счёт, риск стратегии и заявки не изменяются.'),style=p+'.TLabel',wraplength=660).pack(anchor='w',pady=10)
        ttk.Label(body,text=tr('Без разделителей тысяч. Точка или запятая — десятичный знак.'),style=p+'.TLabel',wraplength=660).pack(anchor='w',pady=(0,6))
        form=ttk.Frame(body,style=p+'.TFrame');form.pack(fill='x');form.columnconfigure(1,weight=1)
        self.direction=tk.StringVar(win,'Покупка (Long)');self.currency=tk.StringVar(win,'EUR')
        ttk.Label(form,text=tr('Направление модели'),style=p+'.TLabel').grid(row=0,column=0,sticky='w',pady=4)
        CanonicalCombobox(form,textvariable=self.direction,values=['Покупка (Long)','Продажа (Short)'],state='readonly').grid(row=0,column=1,sticky='ew')
        ttk.Label(form,text=tr('Валюта всех сумм и цен'),style=p+'.TLabel').grid(row=1,column=0,sticky='w',pady=4)
        ttk.Combobox(form,textvariable=self.currency,values=['EUR','USD','GBP'],state='readonly').grid(row=1,column=1,sticky='ew')
        self.values={}
        for n,(key,name,default) in enumerate(FIELDS,2):
            ttk.Label(form,text=tr(name),style=p+'.TLabel').grid(row=n,column=0,sticky='w',pady=4,padx=(0,15))
            self.values[key]=tk.StringVar(win,default)
            ttk.Entry(form,textvariable=self.values[key],style=p+'.TEntry').grid(row=n,column=1,sticky='ew',pady=4)
        self.output=tk.StringVar(win)
        for variable in [*self.values.values(),self.direction,self.currency]:variable.trace_add('write',lambda *_:self.output.set(tr('Параметры изменены. Нажмите «Рассчитать модель».')))
        ttk.Label(body,textvariable=self.output,style=p+'.TLabel',wraplength=660,justify='left').pack(fill='x',pady=12)
        self.button=ttk.Button(footer,text=tr('Рассчитать модель'),style=p+'.Primary.TButton',command=self.run);self.button.pack(side='left')
        ttk.Button(footer,text=tr('Закрыть'),command=win.destroy).pack(side='right')
        win.bind('<Escape>',lambda _:win.destroy())
        win.bind('<Map>',lambda event:win.after_idle(lambda:native_titlebar(win,palette)) if event.widget is win else None,add='+')
        self.run()

    def run(self):
        try:
            result=calculate({k:v.get() for k,v in self.values.items()},'long' if self.direction.get()=='Покупка (Long)' else 'short')
            money=lambda v:format_value(v,language(),currency=self.currency.get())
            lines=[tr('Количество')+': '+format_value(result['quantity'],language(),kind='quantity',digits=8),
                   tr('Риск / бюджет')+': '+money(result['loss'])+' / '+money(result['budget']),
                   tr('Результат у цели после издержек')+': '+money(result['reward']),
                   tr('Прибыль / риск')+': '+(format_value(result['rr'],language(),digits=3) if result['rr'] is not None else '—'),
                   tr('Номинал / резерв средств')+': '+money(result['notional'])+' / '+money(result['reserved']),
                   tr('Издержки модели')+': '+money(result['costs'])]
            if not result['quantity']:lines.append(tr('Средств или бюджета риска недостаточно для одного шага количества.'))
            if result['direction']=='short':lines.append(tr('Short — только модель: наличие акций, заём и разрешение торговли не проверяются.'))
            self.output.set('\n'.join(lines))
        except (ValueError,KeyError,InvalidOperation) as error:self.output.set(tr(str(error)))


def show(app):
    existing=getattr(app,'_position_calculator',None)
    if existing is not None and existing.winfo_exists():existing.lift();return existing
    dialog=Dialog(app);app._position_calculator_dialog=dialog
    return dialog.win
