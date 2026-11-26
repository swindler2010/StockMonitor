from __future__ import annotations
from i18n import tr
from chart_locale import draw_localized
import os, json, tkinter as tk, urllib.request, urllib.error, re, gzip, io, threading, sys, copy, time
from tkinter import ttk, messagebox, filedialog, simpledialog
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import numpy as np
from zoneinfo import ZoneInfo
from paths import DATA_DIR
os.environ.setdefault('MPLCONFIGDIR',str(DATA_DIR/'matplotlib'))
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.patches import Rectangle
from storage import Store
from paths import RESOURCE_DIR, APP_DIR, DATA_DIR, migrate_legacy
from features import Features
from store_release import AlreadyRunning
from analytics import analyse as compute_analysis, resample
import market

VERSION='0.16-dev'
ROOT=RESOURCE_DIR
cfg=json.loads((ROOT/'config.json').read_text(encoding='utf-8'))

BG='#0b1220'; PANEL='#111827'; PANEL2='#172033'; TEXT='#e5e7eb'; MUTED='#8b98aa'; GRID='#273449'
GREEN='#22c55e'; RED='#ef4444'; BLUE='#60a5fa'; ORANGE='#f59e0b'; BORDER='#263244'; WHITE='#f8fafc'

class StockMonitor(Features):
    def __init__(self, root):
        self.root=root; self.root.title(tr('Stock Monitor v{p0}',p0=VERSION))
        self.root.geometry('1420x860'); self.root.minsize(1120,700); self.root.configure(bg=BG)
        self.symbol=tk.StringVar(value='Apple'); self.timeframe=tk.StringVar(value='4H')
        self.status=tk.StringVar(value='NO DATA • Подключите реальные OHLCV. Тестовые свечи удалены.')
        self.source_status=tk.StringVar(value='● NO DATA')
        self.data={}; self.data_meta={}
        migrate_legacy()
        self.store=Store(DATA_DIR)
        from i18n import configure
        configure(self.store)
        self.status.set(tr('NO DATA • Подключите реальные OHLCV. Тестовые свечи удалены.'))
        self.store.seed_defaults(cfg['watchlist'])
        from benchmarks import ensure_catalog
        ensure_catalog(self.store)
        self.watchlist=self.store.instruments()
        selected=self.store.setting('selected',{})
        names=[x['name'] for x in self.watchlist]
        self.symbol.set(selected.get('symbol') if selected.get('symbol') in names else names[0])
        self.timeframe.set(selected.get('timeframe','4H'))
        self.period=tk.StringVar(value='неделя' if selected.get('period','Последние')=='Последние' else selected['period'])
        self.indicators=self.store.setting('indicators',{'ema10':True,'ema30':True,'sma20':False,'bollinger':False,'vwap':False,'volume':True}) if hasattr(self,'store') else {}
        self._load_cached()
        self.http_cache={};self.http_lock=threading.Lock()
        self.init_features()
        self.hover_annotation=None; self.vline=None; self.hline=None; self.current_df=None
        self.cursor_labels=[];self.drag_start=None;self.drag_limits=None;self.user_view=None
        self.build_ui(); self.refresh_chart(); self.root.after(500, self._scheduled_refresh)
        self.priority_gettex_poll=self.store.setting('priority_gettex_poll',{})
        if self.priority_gettex_poll:self.root.after(100,self._poll_priority_gettex)

    def _load_cached(self):
        for item in self.watchlist:
            for base in ('15m','30m','1H','4H','1D','1W'):
                frame,sources=self.store.load_frame(item['isin'],base)
                if not frame.empty:
                    self.data[item['name']]=frame
                    self.data_meta[item['name']]={'base_tf':base,'source':' + '.join(sorted(sources))+' • локальный архив','gettex':'gettex' in sources}
                    break

    def _styles(self):
        s=ttk.Style();
        try:s.theme_use('clam')
        except:pass
        s.configure('.',background=BG,foreground=TEXT,font=('Segoe UI',10))
        s.configure('TFrame',background=BG); s.configure('Panel.TFrame',background=PANEL)
        s.configure('TLabel',background=BG,foreground=TEXT); s.configure('Panel.TLabel',background=PANEL,foreground=TEXT)
        s.configure('Muted.TLabel',background=BG,foreground=MUTED); s.configure('PanelMuted.TLabel',background=PANEL,foreground=MUTED)
        s.configure('Title.TLabel',background=BG,foreground=WHITE,font=('Segoe UI',22,'bold'))
        s.configure('Price.TLabel',background=PANEL,foreground=WHITE,font=('Segoe UI',24,'bold'))
        s.configure('Decision.TLabel',background=PANEL,foreground=ORANGE,font=('Segoe UI',19,'bold'))
        s.configure('TButton',background=PANEL2,foreground=TEXT,padding=(12,7),borderwidth=0)
        s.map('TButton',background=[('active','#22304a')])
        s.configure('Accent.TButton',background='#1d4ed8',foreground='white',padding=(12,7)); s.map('Accent.TButton',background=[('active','#2563eb')])
        s.configure('TRadiobutton',background=BG,foreground=TEXT); s.map('TRadiobutton',background=[('active',BG)])
        s.configure('TCombobox',fieldbackground=PANEL2,background=PANEL2,foreground=TEXT,arrowcolor=TEXT)
        s.map('TCombobox',fieldbackground=[('readonly',PANEL2)],foreground=[('readonly',TEXT)],selectbackground=[('readonly',PANEL2)],selectforeground=[('readonly',TEXT)])
        self.root.option_add('*TCombobox*Listbox.background',PANEL2);self.root.option_add('*TCombobox*Listbox.foreground',TEXT)
        self.root.option_add('*TCombobox*Listbox.selectBackground','#1d4ed8');self.root.option_add('*TCombobox*Listbox.selectForeground',WHITE)

    def build_ui(self):
        self._styles()
        outer=ttk.Frame(self.root,padding=18); outer.pack(fill='both',expand=True)
        header=ttk.Frame(outer); header.pack(fill='x',pady=(0,12))
        ttk.Label(header,text=tr('Stock Monitor'),style='Title.TLabel').pack(side='left')
        ttk.Label(header,text=tr('v{p0}',p0=VERSION),style='Muted.TLabel').pack(side='left',padx=10,pady=(10,0))
        self.source_label=ttk.Label(header,textvariable=self.source_status,style='Muted.TLabel'); self.source_label.pack(side='left',padx=(22,0),pady=(10,0))

        menu=tk.Menu(self.root);self.root.config(menu=menu)
        service=tk.Menu(menu,tearoff=False);menu.add_cascade(label=tr('Программа'),menu=service)
        service.add_command(label=tr('Связь с Codex'),command=self.show_connection)
        service.add_command(label=tr('Обновить программу'),command=self.check_updates)

        ttk.Button(header,text=tr('Настройки'),command=self.show_settings).pack(side='right')
        ttk.Button(header,text=tr('Сохранить'),command=self.save_user_changes).pack(side='right',padx=3)
        ttk.Button(header,text=tr('Портфель Scalable'),command=self.scalable_read).pack(side='right',padx=3)
        ttk.Button(header,text=tr('Новости'),command=self.show_news).pack(side='right',padx=3)
        ttk.Button(header,text=tr('ⓘ'),width=3,command=self.show_card).pack(side='right',padx=3)
        ttk.Button(header,text=tr('Рисование'),command=self.show_drawing).pack(side='right',padx=3)
        ttk.Button(header,text=tr('Индикаторы'),command=self.show_indicators).pack(side='right',padx=3)
        ttk.Button(header,text=tr('Обновить'),command=self.refresh_all_async).pack(side='right',padx=3)
        controls=ttk.Frame(outer); controls.pack(fill='x',pady=(0,12))
        self.symbol_box=ttk.Combobox(controls,textvariable=self.symbol,values=[x['name'] for x in self.watchlist],state='readonly',width=16)
        cb=self.symbol_box
        cb.pack(side='left'); ttk.Button(controls,text=tr('Удалить'),command=self.remove_instrument).pack(side='left',padx=4); cb.bind('<<ComboboxSelected>>',lambda e:self.refresh_chart())
        for tf in ['15m','30m','1H','4H','1D','1W']:
            ttk.Radiobutton(controls,text=tr(tf),value=tf,variable=self.timeframe,command=self.refresh_chart).pack(side='left',padx=(12 if tf=='15m' else 5,0))
        ttk.Button(controls,text=tr('Добавить акцию'),style='Accent.TButton',command=self.add_instrument).pack(side='right')
        ttk.Button(controls,text=tr('Импорт OHLCV CSV'),command=self.load_csv).pack(side='right',padx=(0,8))

        periods=ttk.Frame(outer);periods.pack(fill='x',pady=(0,8))
        ttk.Label(periods,text=tr('Период графика:')).pack(side='left')
        for label in ['Последние','1 месяц','6 месяцев','1 год','2 года','5 лет']:
            ttk.Radiobutton(periods,text=tr(label),value=label,variable=self.period,command=self.change_period).pack(side='left',padx=5)


        body=ttk.Panedwindow(outer,orient='horizontal'); body.pack(fill='both',expand=True)
        left=ttk.Frame(body); side=ttk.Frame(body,style='Panel.TFrame',padding=18); body.add(left,weight=5); body.add(side,weight=2)

        self.fig=Figure(figsize=(10,7),dpi=100,facecolor=BG)
        self.ax_price=self.fig.add_axes([.07,.29,.91,.66],facecolor=BG)
        self.ax_vol=self.fig.add_axes([.07,.08,.91,.16],sharex=self.ax_price,facecolor=BG)
        self.canvas=FigureCanvasTkAgg(self.fig,master=left); self.canvas.get_tk_widget().pack(fill='both',expand=True)
        self.canvas.mpl_connect('motion_notify_event',self.on_motion)
        self.canvas.mpl_connect('axes_leave_event',self.on_leave)
        self.canvas.mpl_connect('button_press_event',self.on_press)
        self.canvas.mpl_connect('button_release_event',self.on_release)
        self.canvas.mpl_connect('scroll_event',self.on_scroll)

        self.instrument=tk.StringVar(value='APPLE • APC • GETTEX')
        ttk.Label(side,textvariable=self.instrument,style='PanelMuted.TLabel',font=('Segoe UI',10,'bold')).pack(anchor='w')
        self.metrics={k:tk.StringVar(value='—') for k in ['price','ema10','ema30','volume','support','resistance','stop','add','updated','rsi','atr','position']}
        ttk.Label(side,textvariable=self.metrics['price'],style='Price.TLabel').pack(anchor='w',pady=(8,3))
        self.decision=tk.StringVar(value='WAIT FOR DATA'); ttk.Label(side,textvariable=self.decision,style='Decision.TLabel').pack(anchor='w',pady=(0,16))
        ttk.Separator(side).pack(fill='x',pady=(0,12))
        for key,label in [('ema10','EMA10'),('ema30','EMA30'),('rsi','RSI 14'),('atr','ATR 14'),('volume','Объём'),('support','Поддержка'),('resistance','Сопротивление'),('stop','STOP'),('add','ADD'),('position','Позиция / P&L')]:
            row=ttk.Frame(side,style='Panel.TFrame'); row.pack(fill='x',pady=5)
            ttk.Label(row,text=tr(label),style='PanelMuted.TLabel').pack(side='left'); ttk.Label(row,textvariable=self.metrics[key],style='Panel.TLabel').pack(side='right')
        ttk.Separator(side).pack(fill='x',pady=14)
        ttk.Label(side,text=tr('Логика проверки'),style='Panel.TLabel',font=('Segoe UI',11,'bold')).pack(anchor='w')
        ttk.Label(side,text=tr('HOLD  →  SELL  →  STOP  →  ADD'),style='PanelMuted.TLabel').pack(anchor='w',pady=(6,14))
        self.explain=tk.StringVar(value='Сигналы отключены, пока нет реальных данных.')
        ttk.Label(side,textvariable=self.explain,style='PanelMuted.TLabel',wraplength=300,justify='left').pack(anchor='w')
        ttk.Button(side,text=tr('Позиция и уровни'),command=self.edit_position).pack(anchor='w',pady=8)
        ttk.Label(side,textvariable=self.metrics['updated'],style='PanelMuted.TLabel',wraplength=300).pack(anchor='w',side='bottom',pady=(18,0))

        footer=ttk.Frame(outer); footer.pack(fill='x',pady=(10,0))
        ttk.Label(footer,textvariable=self.status,style='Muted.TLabel').pack(side='left')
        ttk.Label(footer,textvariable=self.ai_status,style='Muted.TLabel').pack(side='right')

    def instrument_cfg(self):
        return next((x for x in self.watchlist if x['name']==self.symbol.get()),self.watchlist[0])

    def get_tf(self,df,base_tf):
        return resample(df,base_tf,self.timeframe.get())

    def analyse(self,df):
        x,result=compute_analysis(df,self.instrument_cfg())
        return x,result['decision'],result['support'],result['resistance'],result['stop'] if result['stop'] is not None else np.nan

    def empty_chart(self,msg='Нет реальных данных'):
        self.ax_price.clear(); self.ax_vol.clear(); self._format_axes()
        self.ax_price.text(.5,.54,msg,transform=self.ax_price.transAxes,ha='center',va='center',color=TEXT,fontsize=17,fontweight='bold')
        self.ax_price.text(.5,.46,tr('DEMO удалено • загрузите OHLCV выбранной площадки'),transform=self.ax_price.transAxes,ha='center',va='center',color=MUTED,fontsize=10)
        self.ax_price.set_xticks([]); self.ax_price.set_yticks([]); self.ax_vol.set_xticks([]); self.ax_vol.set_yticks([]); draw_localized(self)
        self.current_df=None; self.decision.set('WAIT FOR DATA'); self.explain.set(tr('Сигналы отключены, пока нет реальных данных.'))
        for k in self.metrics:self.metrics[k].set('—')

    def _format_axes(self):
        for ax in (self.ax_price,self.ax_vol):
            ax.set_facecolor(BG); ax.tick_params(colors=MUTED,labelsize=9); ax.grid(color=GRID,alpha=.45,linewidth=.6)
            for sp in ax.spines.values():sp.set_color(BORDER)

    def refresh_chart(self):
        self.store.set_setting('selected',{'symbol':self.symbol.get(),'timeframe':self.timeframe.get(),'period':self.period.get()})
        info=self.instrument_cfg(); self.instrument.set(f"{info['name'].upper()} • {info['symbol']} • {info['venue']}")
        name=self.symbol.get()
        if name not in self.data:
            self.source_status.set(tr('● NO DATA')); self.status.set(tr('NO DATA • Никаких тестовых свечей и торговых сигналов.')); self.empty_chart(); return
        meta=self.data_meta[name]; df=self.frame_for(info,self.timeframe.get())
        if df.empty or len(df)<5:
            self.status.set(tr('Недостаточно данных для {p0}. Источник содержит {p1}.',p0=self.timeframe.get(),p1=meta['base_tf'])); self.empty_chart('Недостаточно данных для таймфрейма'); return
        x,decision,support,resistance,stop=self.analyse(df)
        if np.isfinite(stop) and not info.get('auto_stop'):
            info['auto_stop']=float(stop);self.store.save_instrument(info)
        self.store.set_setting('auto_levels:'+info['isin']+':'+self.timeframe.get(),{'at':str(x.index[-1]),'support':support,'resistance':resistance})
        from chart_periods import visible_frame
        x=visible_frame(x,self.period.get()); self.current_df=x
        custom=info.get('levels',{}); support=float(custom.get('support') or support); resistance=float(custom.get('resistance') or resistance)
        if custom.get('stop'):stop=float(custom['stop'])
        if np.isfinite(stop) and x.iloc[-1].close<=stop:decision='STOP'
        self.ax_price.clear(); self.ax_vol.clear(); self._format_axes(); xs=np.arange(len(x)); width=.64
        for i,(_,r) in enumerate(x.iterrows()):
            c=GREEN if r.close>=r.open else RED; self.ax_price.vlines(i,r.low,r.high,color=c,linewidth=1.05,zorder=2)
            y=min(r.open,r.close); h=max(abs(r.close-r.open),max(r.close*.00015,.01))
            self.ax_price.add_patch(Rectangle((i-width/2,y),width,h,facecolor=c,edgecolor=c,alpha=.92,zorder=3)); self.ax_vol.bar(i,r.volume,width=width,color=c,alpha=.55)
        enabled=self.store.setting('indicators',{'ema10':True,'ema30':True,'sma20':False,'bollinger':False,'vwap':False,'volume':True})
        if enabled.get('ema10',True):self.ax_price.plot(xs,x.ema10.values,label=tr('EMA10'),color=BLUE,linewidth=1.6)
        if enabled.get('ema30',True):self.ax_price.plot(xs,x.ema30.values,label=tr('EMA30'),color=ORANGE,linewidth=1.6)
        if enabled.get('sma20'):
            sma=x.close.rolling(20,min_periods=1).mean();self.ax_price.plot(xs,sma,label=tr('SMA20'),color='#a78bfa',linewidth=1.35)
        if enabled.get('bollinger'):
            mid=x.close.rolling(20,min_periods=5).mean();dev=x.close.rolling(20,min_periods=5).std();upper=mid+dev*2;lower=mid-dev*2
            self.ax_price.plot(xs,upper,label=tr('Bollinger'),color='#94a3b8',linewidth=.9);self.ax_price.plot(xs,lower,color='#94a3b8',linewidth=.9);self.ax_price.fill_between(xs,lower.to_numpy(),upper.to_numpy(),color='#64748b',alpha=.08)
        if enabled.get('vwap'):
            volume=x.volume.replace(0,np.nan);vwap=((x.high+x.low+x.close)/3*volume).cumsum()/volume.cumsum();self.ax_price.plot(xs,vwap,label=tr('VWAP'),color='#e879f9',linewidth=1.25)
        self.ax_price.axhline(support,color=GREEN,alpha=.35,linewidth=1); self.ax_price.axhline(resistance,color=RED,alpha=.35,linewidth=1)
        for drawing in info.get('drawings',[]):
            if drawing.get('type')=='hline' and drawing.get('price'):
                self.ax_price.axhline(float(drawing['price']),color=BLUE,linewidth=1.4,linestyle='--',alpha=.95,label=tr('Уровень пользователя'))
            elif drawing.get('type')=='vline' and drawing.get('at'):
                try:
                    at=pd.Timestamp(drawing['at']);idx=int(np.argmin(np.abs((x.index-at).asi8)));self.ax_price.axvline(idx,color=BLUE,linewidth=1.3,linestyle=':',alpha=.95)
                except Exception:pass
        chart_sources=' + '.join(sorted(set(x['source'].astype(str)))) if 'source' in x else 'unknown'
        currency=info.get('currency','EUR');venue=info.get('venue','не указана')
        self.ax_price.set_title(f"{info['name']} • {self.timeframe.get()} • {venue} • {chart_sources.upper()} • {currency}",color=TEXT,fontsize=13,pad=12)
        leg=self.ax_price.legend(loc='upper left',facecolor=PANEL,edgecolor=BORDER,labelcolor=TEXT,framealpha=.9); self.ax_price.set_ylabel(currency,color=MUTED); self.ax_vol.set_ylabel('Объём',color=MUTED)
        step=max(1,len(x)//8); ticks=xs[::step]; labels=[x.index[i].strftime('%d.%m\n%H:%M') if self.timeframe.get() in ('15m','30m','1H','4H') else x.index[i].strftime('%d.%m\n%Y') for i in ticks]
        self.ax_vol.set_xticks(ticks,labels); self.ax_price.tick_params(labelbottom=False); draw_localized(self)
        if self.user_view:
            self.ax_price.set_xlim(self.user_view[0]);self.ax_price.set_ylim(self.user_view[1]);self.ax_vol.set_xlim(self.user_view[0])
        last=x.iloc[-1]; self.decision.set(decision); self.metrics['price'].set(f'{last.close:.2f} €'); self.metrics['ema10'].set(f'{last.ema10:.2f} €'); self.metrics['ema30'].set(f'{last.ema30:.2f} €')
        self.metrics['volume'].set(f"{int(last.volume):,}".replace(',',' ')); self.metrics['support'].set(f'{support:.2f} €'); self.metrics['resistance'].set(f'{resistance:.2f} €')
        self.metrics['rsi'].set(f'{last.rsi:.1f}' if np.isfinite(last.rsi) else '—'); self.metrics['atr'].set(f'{last.atr:.2f} €')
        qty=float(info.get('position',{}).get('quantity') or 0); avg=float(info.get('position',{}).get('average_price') or 0)
        pnl=qty*(last.close-avg); self.metrics['position'].set(f"{qty:g}{tr(' шт. / ')}{pnl:+.2f} €" if qty and avg else tr('не указана'))
        self.metrics['stop'].set(f'{stop:.2f} €' if np.isfinite(stop) else tr('нужно ≥30 свечей')); self.metrics['add'].set('REVIEW' if decision=='ADD' else 'WAIT')
        self.metrics['updated'].set(f"{tr('Последняя свеча: ')}{x.index[-1].strftime('%d.%m.%Y %H:%M')} • {venue} • {currency} • {meta['source']}")
        badge='● '+str(last.get('source','unknown')).upper()+' • '+x.index[-1].strftime('%d.%m %H:%M')
        self.source_status.set(tr(badge)); self.status.set(tr('Источник: {p0}',p0=meta['source']))
        risk=max(0,(last.close-stop)/last.close*100) if np.isfinite(stop) else np.nan
        reasons={'HOLD':'Цена выше EMA10 и EMA30; тренд сохраняется.','SELL':'Цена ниже EMA30; проверь выход из позиции.','STOP':'Цена достигла стоп-уровня; требуется срочная проверка.','ADD':'Восходящий тренд и откат к EMA10; добавление только после проверки риска.','WAIT':'Условия для HOLD/SELL/ADD не подтверждены.'}
        risk_text=f'{risk:.1f}%' if np.isfinite(risk) else 'недоступен'
        stop_text=f'{stop:.2f} €' if np.isfinite(stop) else 'недоступен'
        self.explain.set(f"{reasons.get(decision, '')}{tr(' Стоп: ')}{stop_text}{tr('; риск до стопа: ')}{risk_text}{tr(' от цены. Данные: ')}{meta['source']}{tr('. Решение требует проверки человеком.')}")
        self.notify_signal(info,decision,x.index[-1])

    def on_motion(self,event):
        if self.drag_start is not None:
            self.on_drag(event);return
        self.on_hover(event)

    def on_hover(self,event):
        if self.current_df is None or event.inaxes not in (self.ax_price,self.ax_vol) or event.xdata is None:return
        i=int(round(event.xdata)); x=self.current_df
        if i<0 or i>=len(x):return
        r=x.iloc[i]; dt=x.index[i]
        if self.vline is not None:
            try:self.vline.remove(); self.hline.remove(); self.hover_annotation.remove()
            except:pass
        self.vline=self.ax_price.axvline(i,color=MUTED,alpha=.65,linewidth=.8,linestyle='--'); y=event.ydata if event.inaxes==self.ax_price else r.close
        self.hline=self.ax_price.axhline(y,color=MUTED,alpha=.45,linewidth=.8,linestyle='--')
        txt=(f"{dt.strftime('%d.%m.%Y %H:%M')} • {r.get('source','unknown')}\nO {r.open:.2f}   H {r.high:.2f}\nL {r.low:.2f}   C {r.close:.2f}\nVol {int(r.volume):,}\nEMA10 {r.ema10:.2f}   EMA30 {r.ema30:.2f}").replace(',',' ')
        self.hover_annotation=self.ax_price.annotate(txt,xy=(i,r.close),xytext=(14,14),textcoords='offset points',color=TEXT,fontsize=9,bbox=dict(boxstyle='round,pad=.55',fc=PANEL,ec=BORDER,alpha=.96),zorder=10)
        for artist in getattr(self,'cursor_labels',[]):
            try:artist.remove()
            except ValueError:pass
        self.cursor_labels=[self.ax_price.text(0,y,f'{y:.2f}',transform=self.ax_price.get_yaxis_transform(),ha='right',va='center',color=WHITE,bbox=dict(fc=PANEL,ec=BLUE),zorder=20),self.ax_vol.text(i,0,dt.strftime('%d.%m.%Y %H:%M'),transform=self.ax_vol.get_xaxis_transform(),ha='center',va='top',color=WHITE,bbox=dict(fc=PANEL,ec=BLUE),zorder=20)]
        draw_localized(self)

    def on_leave(self,event):
        if self.drag_start is not None:return
        for artist in [self.vline,self.hline,self.hover_annotation]+getattr(self,'cursor_labels',[]):
            if artist is not None:
                try:artist.remove()
                except ValueError:pass
        self.vline=self.hline=self.hover_annotation=None;self.cursor_labels=[]
        draw_localized(self)

    def on_press(self,event):
        if event.button!=1 or event.inaxes not in (self.ax_price,self.ax_vol) or event.xdata is None:return
        self.drag_start=(event.xdata,event.ydata,event.inaxes);self.drag_limits=(self.ax_price.get_xlim(),self.ax_price.get_ylim())

    def on_drag(self,event):
        if event.xdata is None or self.drag_start is None or self.drag_limits is None:return
        sx,sy,start_ax=self.drag_start;xlim,ylim=self.drag_limits;dx=event.xdata-sx
        nx=(xlim[0]-dx,xlim[1]-dx);self.ax_price.set_xlim(nx);self.ax_vol.set_xlim(nx)
        if start_ax==self.ax_price and event.ydata is not None and sy is not None:
            dy=event.ydata-sy;self.ax_price.set_ylim(ylim[0]-dy,ylim[1]-dy)
        draw_localized(self)

    def on_release(self,event):
        if self.drag_start is None:return
        self.drag_start=None;self.drag_limits=None;self.user_view=(self.ax_price.get_xlim(),self.ax_price.get_ylim());draw_localized(self)

    def on_scroll(self,event):
        if event.inaxes not in (self.ax_price,self.ax_vol) or event.xdata is None:return
        scale=.82 if event.button=='up' else 1.22
        xlim=self.ax_price.get_xlim();cx=event.xdata;nx=(cx-(cx-xlim[0])*scale,cx+(xlim[1]-cx)*scale)
        self.ax_price.set_xlim(nx);self.ax_vol.set_xlim(nx)
        if event.inaxes==self.ax_price and event.ydata is not None:
            ylim=self.ax_price.get_ylim();cy=event.ydata;self.ax_price.set_ylim(cy-(cy-ylim[0])*scale,cy+(ylim[1]-cy)*scale)
        self.user_view=(self.ax_price.get_xlim(),self.ax_price.get_ylim());draw_localized(self)

    def change_period(self):
        if self.period.get()!='Последние' and self.timeframe.get() not in ('1D','1W'):
            self.timeframe.set('1D')
        self.refresh_chart()

    def _scheduled_refresh(self):
        self.refresh_all_async()
        self.root.after(15*60*1000, self._scheduled_refresh)

    def refresh_all_async(self):
        if '--smoke-test' in sys.argv:return
        for info in self.watchlist:self.start_feed(info)

    def start_feed(self,info):
        if info['isin'] in self.feed_active:return
        self.feed_active.add(info['isin'])
        threading.Thread(target=self._gettex_worker,args=(copy.deepcopy(info),),daemon=True).start()

    def add_instrument(self):
        self.add_stock_dialog()

    def edit_position(self):
        self.position_dialog()

    def refresh_gettex_async(self):
        self.start_feed(self.instrument_cfg())

    def _gettex_worker(self,info):
        with self.feed_slots:
            if self.closing:return
            history=self._download_history_7d(info)
            long_history=pd.DataFrame()
            symbol=info.get('history_symbol') or ('APC.F' if info['isin']=='US0378331005' else None)
            if symbol:
                try:long_history=market.history(symbol,'5y','1d',info.get('currency','EUR'))
                except Exception as e:self._write_log('Daily proxy: '+str(e))
            trades=pd.DataFrame();error=None
            try:trades=self._download_gettex_trades(info['isin'])
            except Exception as e:error=str(e)
            if not self.closing:self.events.put((self._finish_feed,(info,trades,history,long_history,error)))

    def _finish_feed(self,info,trades,history,long_history,error):
        self.feed_active.discard(info['isin'])
        if not any(i['isin']==info['isin'] for i in self.watchlist):return
        if not long_history.empty:self.store.save_frame(info['isin'],'proxy','1D',long_history)
        if not trades.empty:self._accept_gettex(info,trades,history)
        else:
            if not history.empty:self.store.save_frame(info['isin'],'proxy','15m',history)
            self._load_cached()
        if error:
            self.feed_errors[info['isin']]=error;self._write_log(info['isin']+': '+error)
        else:self.feed_errors.pop(info['isin'],None)
        # Evaluate every watchlist position, even when another stock is selected.
        current=next(i for i in self.watchlist if i['isin']==info['isin'])
        frame=self.data.get(info['name'])
        if frame is not None and not frame.empty:
            enriched,result=compute_analysis(frame,current)
            if result['stop'] is not None and not current.get('auto_stop'):
                current['auto_stop']=result['stop'];self.store.save_instrument(current)
            self.notify_signal(current,result['decision'],frame.index[-1])
        if self.symbol.get()==info['name']:
            self.refresh_chart()
            if error:self.status.set('GETTEX: ' + error[:140] + tr(' • доступный архив/proxy сохранён'))

    def _write_log(self,msg):
        try:
            log_dir=DATA_DIR; log_dir.mkdir(exist_ok=True)
            with (log_dir/'stock_monitor.log').open('a',encoding='utf-8') as f:
                f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
        except Exception:
            pass

    def _gettex_error(self,msg):
        self.source_status.set(tr('● GETTEX ERROR'))
        self.status.set('GETTEX ERROR: ' + msg[:220])
        if self.symbol.get() not in self.data:self.empty_chart('GETTEX временно недоступен')

    def _http_bytes(self,url):
        with self.http_lock:
            cached=self.http_cache.get(url)
            if cached and time.monotonic()-cached[0]<300:return cached[1]
            req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 StockMonitor/0.11','Accept':'*/*'})
            with urllib.request.urlopen(req,timeout=30) as r:data=r.read()
            self.http_cache[url]=(time.monotonic(),data)
            if len(self.http_cache)>20:
                oldest=min(self.http_cache,key=lambda k:self.http_cache[k][0]);self.http_cache.pop(oldest)
            return data

    def _download_gettex_trades(self,isin):
        page='https://www.gettex.de/handel/delayed-data/posttrade-data/'
        html=self._http_bytes(page).decode('utf-8','ignore')
        links=re.findall(r'href=[\"\']([^\"\']*posttrade\.[^\"\']+\.csv\.gz[^\"\']*)[\"\']',html,re.I)
        if not links: raise RuntimeError('официальная страница сейчас не содержит файлов post-trade (возможен выходной/истёкший архив)')
        from urllib.parse import urljoin
        # Newest MUND is normally enough because each file contains the last 24h.
        links=[urljoin(page,x.replace('&amp;','&')) for x in links]
        links=sorted(set(links),reverse=True)
        ordered=[x for x in links if '.mund.' in x.lower()]+[x for x in links if '.munc.' in x.lower()]
        last_error=None
        for url in ordered[:8]:
            try:
                raw=self._http_bytes(url)
                try: raw=gzip.decompress(raw)
                except OSError: pass
                df=self._parse_gettex_csv(raw,isin)
                if len(df): return df
            except Exception as e:last_error=e
        raise RuntimeError(f'сделки для ISIN {isin} не найдены' + (f' ({last_error})' if last_error else ''))

    def _parse_gettex_csv(self,raw,isin,file_time=None):
        # GETTEX publishes MiFID post-trade CSV.  The exact header labels can
        # change, so parse both the official semantic names and row values.
        text=None
        for enc in ('utf-8-sig','utf-16','cp1252','latin1'):
            try:
                candidate=raw.decode(enc)
                if isin in candidate or enc=='latin1':
                    text=candidate; break
            except (UnicodeError, UnicodeDecodeError):
                continue
        if not text:
            return pd.DataFrame()

        lines=[x for x in text.splitlines() if x.strip()]
        if not lines:return pd.DataFrame()
        if re.match(r'^[A-Z]{2}[A-Z0-9]{9}\d,\d{2}:\d{2}:\d{2}',lines[0]):
            if file_time is None:raise ValueError('GETTEX без даты: необходим timestamp имени файла; текущая дата не подставляется.')
            selected=[line for line in lines if line.startswith(isin+',')]
            if not selected:return pd.DataFrame()
            q=pd.read_csv(io.StringIO('\n'.join(selected)),header=None,names=['isin','time','currency','price','volume'],dtype=str)
            q=q.loc[q.currency.eq('EUR')]
            if q.empty:return pd.DataFrame()
            anchor=pd.Timestamp(file_time).tz_convert('UTC')
            stamps=pd.to_datetime(anchor.strftime('%Y-%m-%d')+' '+q.time,format='mixed',errors='coerce',utc=True)
            stamps=stamps.where(stamps<=anchor,stamps-pd.Timedelta(days=1))
            out=pd.DataFrame({'price':pd.to_numeric(q.price,errors='coerce').to_numpy(),'volume':pd.to_numeric(q.volume,errors='coerce').to_numpy()},index=pd.DatetimeIndex(stamps))
            out=out.loc[out.index.notna()&(out.price>0)&(out.volume>=0)]
            out.index=out.index.tz_convert('Europe/Berlin').tz_localize(None)
            return out.sort_index().rename_axis(None)
        sample='\n'.join(lines[:8])
        sep=max([';',',','|','\t'],key=lambda d:sample.count(d))
        try:
            df=pd.read_csv(io.StringIO(text),sep=sep,dtype=str,on_bad_lines='skip',engine='python')
        except Exception:
            return pd.DataFrame()
        if df.empty:return pd.DataFrame()

        # Locate Apple/Tesla by exact ISIN regardless of column label.
        clean=df.fillna('').astype(str).apply(lambda c:c.str.strip())
        mask=clean.apply(lambda c:c.eq(isin)).any(axis=1)
        q=df.loc[mask].copy()
        if q.empty:return pd.DataFrame()

        cols={c:self._norm_col(c) for c in q.columns}
        def pick_exact(names):
            for c,n in cols.items():
                if n in names:return c
            return None
        def pick_contains(words,exclude=()):
            for c,n in cols.items():
                if any(w in n for w in words) and not any(x in n for x in exclude):return c
            return None

        # MiFID standard wording is usually 'Trading date and time', not
        # 'trade time'.  This was the bug in v0.7.
        timecol=pick_exact({
            'tradingdateandtime','tradingdatetime','executiondateandtime',
            'executiondatetime','transactiondateandtime','transactiondatetime',
            'publicationdateandtime','publicationdatetime','timestamp','datetime'
        }) or pick_contains([
            'tradingdateandtime','executiondateandtime','transactiondateandtime',
            'publicationdateandtime','executiontime','tradetime','transactiontime',
            'timestamp','datetime'
        ],['status'])
        pricecol=pick_exact({'price','transactionprice','tradeprice','executionprice'}) or \
                 pick_contains(['transactionprice','tradeprice','executionprice','price'],
                               ['currency','type','notation'])
        qtycol=pick_exact({'quantity','transactionquantity','tradequantity','volume','size'}) or \
               pick_contains(['transactionquantity','tradequantity','quantity','volume','size','numberofunits'])
        currencycol=pick_exact({'pricecurrency','currency'})
        if currencycol:
            q=q.loc[q[currencycol].str.strip().str.upper().eq('EUR')]
            if q.empty:return pd.DataFrame()

        # Header-independent fallback: inspect values only in rows containing ISIN.
        if timecol is None:
            best=(0,None)
            for c in q.columns:
                parsed=pd.to_datetime(q[c],errors='coerce',utc=True)
                score=int(parsed.notna().sum())
                if score>best[0]:best=(score,c)
            if best[0]:timecol=best[1]

        if pricecol is None:
            candidates=[]
            for c in q.columns:
                if c==timecol:continue
                rawc=q[c].astype(str).str.strip().str.replace(' ','',regex=False)
                z=pd.to_numeric(rawc.str.replace(',','.',regex=False),errors='coerce')
                vals=z[(z>0.01)&(z<100000)]
                if len(vals):
                    # Price is commonly decimal; favour plausible monetary columns.
                    decimal_share=rawc.str.contains(r'[\.,]\d+',regex=True).mean()
                    candidates.append((float(decimal_share),int(vals.notna().sum()),c))
            if candidates:pricecol=max(candidates)[2]

        if timecol is None or pricecol is None:
            raise ValueError('GETTEX CSV найден, но не распознаны Trading date and time / Price')

        t=pd.to_datetime(q[timecol],errors='coerce',utc=True)
        price=pd.to_numeric(q[pricecol].astype(str).str.strip().str.replace(' ','',regex=False).str.replace(',','.',regex=False),errors='coerce')
        if qtycol:
            qty=pd.to_numeric(q[qtycol].astype(str).str.strip().str.replace(' ','',regex=False).str.replace(',','.',regex=False),errors='coerce')
        else:
            qty=pd.Series(1.0,index=q.index)
        out=pd.DataFrame({'price':price.to_numpy(),'volume':qty.fillna(0).to_numpy()},index=t)
        out=out[~out.index.isna()].dropna(subset=['price'])
        out=out[(out.price>0)&(out.volume>=0)]
        if out.empty:return out
        out.index=out.index.tz_convert(ZoneInfo('Europe/Berlin')).tz_localize(None)
        return out.sort_index()

    def _norm_col(self,c):
        return re.sub(r'[^a-z0-9]','',str(c).lower())

    def _download_history_7d(self,info):
        symbol=info.get('history_symbol') or ('APC.F' if info.get('isin')=='US0378331005' else None)
        if not symbol:return pd.DataFrame()
        try:return market.history(symbol,'1mo','15m',info.get('currency','EUR'))
        except Exception as e:
            self._write_log('History proxy: '+str(e));return pd.DataFrame()

    def _accept_gettex(self,info,trades,history=None):
        # Keep every observed execution. The feed only exposes recent files, so
        # offline gaps older than its retention cannot be reconstructed as GETTEX.
        self.store.save_trades(info['isin'],trades)
        trades=self.store.load_trades(info['isin'])
        # Canonical base is 15m so 15m/30m/1H/4H can all be rendered.
        ohlc=trades.price.resample('15min').ohlc()
        vol=trades.volume.resample('15min').sum().rename('volume')
        live=ohlc.join(vol).dropna(subset=['open','high','low','close'])
        if live.empty:raise RuntimeError('GETTEX вернул сделки, но OHLCV построить не удалось')
        self.store.save_frame(info['isin'],'gettex','15m',live)
        if history is not None and not history.empty:self.store.save_frame(info['isin'],'proxy','15m',history)
        df,sources=self.store.load_frame(info['isin'])
        source='GETTEX official post-trade • delayed ≤15 min • EUR'
        if 'proxy' in sources:
            source='7d EUR history proxy + GETTEX official delayed ≤15 min (fresh data)'
        self.data[info['name']]=df
        self.data_meta[info['name']]={'base_tf':'15m','source':' + '.join(sorted(sources))+' • EUR','gettex':True}
        if self.symbol.get()==info['name']:self.refresh_chart()

    def load_csv(self):
        path=filedialog.askopenfilename(title=tr('Реальные OHLCV CSV'),filetypes=[('CSV','*.csv')]);
        if not path:return
        try:
            df=pd.read_csv(path); lower={str(c).lower().strip():c for c in df.columns}
            for need in ['open','high','low','close','volume']:
                if need not in lower:raise ValueError(f'Нет столбца {need}')
            datecol=next((lower[k] for k in ['datetime','date','time','timestamp'] if k in lower),None)
            if not datecol:raise ValueError('Нужен столбец datetime/date/time/timestamp. В v0.5 время больше не выдумывается.')
            idx=pd.to_datetime(df[datecol],errors='coerce'); df=df.rename(columns={lower[k]:k for k in ['open','high','low','close','volume']}); df.index=idx
            df=df[['open','high','low','close','volume']].apply(pd.to_numeric,errors='coerce').dropna().sort_index(); df=df[~df.index.duplicated(keep='last')]
            df=df[~df.index.isna()]
            if df.index.tz is not None:df.index=df.index.tz_convert('Europe/Berlin').tz_localize(None)
            if (df[['open','high','low','close']]<=0).any().any() or (df.volume<0).any() or (df.high<df[['open','close','low']].max(axis=1)).any() or (df.low>df[['open','close','high']].min(axis=1)).any():raise ValueError('Некорректные OHLCV значения.')
            df['source']='csv'
            if len(df)<5:raise ValueError('Слишком мало корректных OHLCV строк.')
            base=simpledialog.askstring(tr('Таймфрейм CSV'),tr('Какой таймфрейм у файла? 15m / 30m / 1H / 4H / 1D / 1W'),initialvalue='4H',parent=self.root)
            base=(base or '').upper().strip();base={'15M':'15m','30M':'30m'}.get(base,base)
            if base not in ('15m','30m','1H','4H','1D','1W'):raise ValueError('Укажи 1H, 4H, 1D или 1W.')
            info=self.instrument_cfg(); venue=simpledialog.askstring(tr('Площадка'),tr('Площадка данных для {p0}:',p0=info['name']),initialvalue=info['venue'],parent=self.root) or info['venue']
            self.data[self.symbol.get()]=df; self.data_meta[self.symbol.get()]={'base_tf':base,'source':f'CSV • {venue} • EUR'}
            self.store.save_frame(info['isin'],'csv',base,df); self.refresh_chart()
        except Exception as e:messagebox.showerror(tr('Ошибка данных'),tr(str(e)))


from desktop014 import Desktop014
from extras016 import Extras016
from save_exit import SaveExitMixin
from benchmark_ui import BenchmarkMixin
class StockMonitor(SaveExitMixin, BenchmarkMixin, Extras016, Desktop014, StockMonitor):
    pass

if __name__=='__main__':
    if '--compute-worker' in sys.argv:
        from compute_process import serve
        serve();sys.exit(0)
    if '--self-test' in sys.argv:
        from ai_service import validate_config
        DATA_DIR.mkdir(parents=True,exist_ok=True)
        test_store=Store(DATA_DIR/'self-test')
        test_store.set_setting('roundtrip',{'ok':True})
        assert test_store.setting('roundtrip')['ok']
        assert validate_config({'provider':'local'})['provider']=='local'
        (DATA_DIR/'self-test-ok.json').write_text(json.dumps({'database':True,'modules':True,'version':VERSION,'gui_tested':False}),encoding='utf-8')
        sys.exit(0)
    root=None
    try:
        root=tk.Tk()
        from ui_performance import install
        install(root,DATA_DIR)
        monitor=StockMonitor(root)
        if '--smoke-test' in sys.argv:
            root.after(2000,lambda:(DATA_DIR.joinpath('smoke-ok.txt').write_text('UI and AI startup OK',encoding='utf-8'),monitor.close()))
        root.mainloop()
    except AlreadyRunning:
        if root is not None:
            root.destroy()
        if '--smoke-test' not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,'Stock Monitor уже запущен. Переключитесь на открытое окно программы.','Stock Monitor',64)
        sys.exit(0)
    except Exception:
        import traceback
        DATA_DIR.mkdir(parents=True,exist_ok=True)
        (DATA_DIR/'startup-error.log').write_text(traceback.format_exc(),encoding='utf-8')
        if '--smoke-test' not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,'Ошибка запуска. Журнал: '+str(DATA_DIR/'startup-error.log'),'Stock Monitor',16)
        sys.exit(1)


