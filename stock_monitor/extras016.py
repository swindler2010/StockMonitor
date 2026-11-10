import tkinter as tk
from tkinter import ttk
import pandas as pd
from dividends import total_return
from logos016 import LogoMixin

class Extras016(LogoMixin):
    def level_zones(self,info,frame):
        from significant_levels import zones
        meta=frame.attrs
        if meta.get('source')=='gettex':
            raw=self.store.load_official(info['isin'])
            daily=raw.resample('1D').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna() if not raw.empty else raw
        else:
            daily=self.store.load_series(info['isin'],'1D',meta.get('source'),meta.get('symbol'),meta.get('venue'),meta.get('currency'))
        nearest=zones(frame)
        if daily.empty:return nearest
        daily=daily.loc[daily.index>=daily.index[-1]-pd.DateOffset(years=20)]
        historical=[z for z in zones(daily) if z['historical'] and z['touches']>=2]
        current=float(frame.close.iloc[-2]) if len(frame)>1 else float(frame.close.iloc[-1])
        result=[]
        for side in ('support','resistance'):
            local=[z for z in nearest if z['side']==side]
            old=[z for z in historical if z['side']==side and (z['high']<current if side=='support' else z['low']>current)]
            chosen=sorted(local,key=lambda z:z['distance'])[:1]
            for zone in sorted(old+local,key=lambda z:(z['historical'],z['touches']),reverse=True):
                if len(chosen)>=3:break
                if not any(zone['low']<=z['high'] and zone['high']>=z['low'] for z in chosen):chosen.append(zone)
            result.extend(chosen)
        return result

    def toggle_chart_fullscreen(self,event=None):
        if getattr(self,'chart_fullscreen',False):
            self.chart_body.add(self.chart_side,weight=1)
            for widget,options in self.hidden_controls:widget.pack_forget()
            for widget,options in self.hidden_controls:widget.pack(**options)
            self.root.attributes('-fullscreen',self.previous_fullscreen);self.chart_fullscreen=False
            self.root.geometry(self.previous_geometry)
            # Windows delivers the fullscreen resize after the first idle turn.
            # Restore the split only once the pane geometry has settled.
            self._sash_restore_timer=None
            self._sash_restore_attempts=0
            def restore():
                self._sash_restore_attempts+=1
                if abs(self.chart_body.winfo_width()-self.previous_body_width)>2 and self._sash_restore_attempts<20:
                    self._sash_restore_timer=self.root.after(150,restore)
                    return
                self.chart_body.unbind('<Configure>',self._sash_restore_binding)
                self._sash_restore_timer=None
                if not self.chart_fullscreen:
                    self.chart_body.sashpos(0,round(self.previous_sash_ratio*self.chart_body.winfo_width()))
                    self.canvas.draw_idle()
            def schedule(event=None):
                if self._sash_restore_timer is not None:self.root.after_cancel(self._sash_restore_timer)
                self._sash_restore_timer=self.root.after(150,restore)
            self._sash_restore_binding=self.chart_body.bind('<Configure>',schedule,add='+')
            schedule()
        else:
            if getattr(self,'_sash_restore_timer',None) is not None:
                self.root.after_cancel(self._sash_restore_timer)
                self.chart_body.unbind('<Configure>',self._sash_restore_binding)
                self._sash_restore_timer=None
            self.previous_fullscreen=self.root.attributes('-fullscreen')
            self.previous_geometry=self.root.geometry()
            self.previous_body_width=self.chart_body.winfo_width()
            self.previous_sash=self.chart_body.sashpos(0)
            self.previous_sash_ratio=self.previous_sash/max(1,self.chart_body.winfo_width())
            self.hidden_controls=[]
            # Restore layout order, not creation order: benchmark controls are
            # created later but packed before the expanding chart body.
            for widget in self.chart_outer.pack_slaves():
                if widget.winfo_manager()=='pack':
                    self.hidden_controls.append((widget,widget.pack_info()))
                    if widget!=self.chart_body:widget.pack_forget()
            self.chart_body.forget(self.chart_side)
            self.root.attributes('-fullscreen',True);self.chart_fullscreen=True
        self.canvas.draw_idle()

    def escape_chart(self,event=None):
        if getattr(self,'chart_fullscreen',False):self.toggle_chart_fullscreen()

    def change_chart_mode(self,event=None):
        self.store.set_setting('chart_mode',self.chart_mode.get())
        self.user_view=(self.ax_price.get_xlim(),self.ax_price.get_ylim());self.refresh_chart()

    def dividends_for(self,info):
        import time
        key='dividends:'+info['isin'];saved=self.store.setting(key,None)
        if not hasattr(self,'dividend_pending'):self.dividend_pending=set()
        if (saved is None or time.time()-saved.get('fetched_at',0)>86400) and key not in self.dividend_pending:
            self.dividend_pending.add(key)
            from dividends import download
            symbol=info.get('profile_symbol') or info.get('history_symbol')
            if symbol:
                def done(data):
                    self.store.set_setting(key,data)
                    if self.symbol.get()==info['name']:self.refresh_chart()
                self.job(lambda:download(symbol),done,lambda _:None)
        return saved or {}

    def position_returns(self,info,position,price_pnl,currency):
        # A current quantity is not a historical holding record.
        receipts=position.get('dividend_receipts')
        if receipts is not None:
            events=[{'date':r['date'],'amount':r['amount'],'currency':r['currency']} for r in receipts]
            return total_return(price_pnl,events,lambda _:1,currency,position.get('dividends_complete',False))
        return total_return(price_pnl,[],lambda _:None,currency,False)

    def draw_dividends(self,info,frame):
        self.dividend_markers=[];data=self.dividends_for(info)
        for event in data.get('events',[]):
            at=pd.Timestamp(event['date']);index=frame.index.tz_localize(None) if frame.index.tz is not None else frame.index
            last_day=index[-1].normalize()+pd.Timedelta(days=6 if self.timeframe.get()=='1W' else 0)
            if at<index[0].normalize() or at>last_day:continue
            i=max(0,min(len(frame)-1,int(index.searchsorted(at,side='right')-1 if self.timeframe.get()=='1W' else index.searchsorted(at))))
            mark=self.ax_price.text(i,.02,'D',transform=self.ax_price.get_xaxis_transform(),ha='center',color='#60a5fa',bbox=dict(facecolor='white',alpha=.8),picker=5,zorder=30)
            self.dividend_markers.append((mark,event))

    def on_hover(self,event):
        for marker,dividend in getattr(self,'dividend_markers',[]):
            if event.inaxes==self.ax_price and marker.contains(event)[0]:
                q=self.user_state(self.instrument_cfg()).get('position',{}).get('quantity')
                currency=dividend.get('currency') or 'валюта выплаты не подтверждена'
                text=f"Ex-dividend {dividend['date']} • {dividend['amount']:g} {currency}/share • Yahoo corporate actions"
                if q is not None:text+=f" • для текущих {q:g} шт.: {q*dividend['amount']:.2f} {currency} (не подтверждение выплаты)"
                self.status.set(text);return
        return super().on_hover(event)
