"""Shared time axis and batched, display-only candle rendering."""
from datetime import datetime

import numpy as np
from matplotlib import dates as mdates
from matplotlib.collections import LineCollection, PolyCollection
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from intraday_data import BERLIN
from timeframes import MINUTES
from i18n import tr


class Plot:
    def __init__(self,parent,hover):
        self.hover=hover
        self.figure=Figure(figsize=(11,5.2),dpi=90,facecolor='#101827')
        self.figure.subplots_adjust(left=.07,right=.96,bottom=.12,top=.96,hspace=.2,wspace=.17)
        self.canvas=FigureCanvasTkAgg(self.figure,master=parent)
        self.canvas.get_tk_widget().pack(fill='both',expand=True)
        self.axes=[]
        self.cursors=[]
        self.background=None
        self.limits=None
        self.time_bounds=None
        self.anchors=[]
        self.canvas.mpl_connect('draw_event',self.cache)
        self.canvas.mpl_connect('motion_notify_event',self.motion)
        self.canvas.mpl_connect('scroll_event',self.zoom)
        self.canvas.mpl_connect('button_press_event',self.click)
        self.canvas.mpl_connect('button_release_event',self.release)
        self.drag=None

    def cache(self,_):
        self.background=self.canvas.copy_from_bbox(self.figure.bbox)

    def render(self,frames,events,clock=None,preserve=False):
        old=self.limits if preserve else None
        self.figure.clear()
        self.axes=[]
        self.cursors=[]
        self.background=None
        self.anchors=[]
        count=len(frames)
        extent=[]
        original=[]
        for k,(tf,frame) in enumerate(frames.items()):
            ax=self.figure.add_subplot(1 if count<=2 else 2,1 if count==1 else 2,k+1,
                                       sharex=self.axes[0] if self.axes else None)
            self.axes.append(ax)
            ax.set_facecolor('#131e30')
            ax.tick_params(colors='#b9c9de',labelsize=8)
            ax.grid(alpha=.12,color='#b9c9de')
            ax.text(.02,.91,tf+' · EUR',color='#e5edf9',transform=ax.transAxes,fontsize=9)
            self.cursors.append(ax.axvline(0,color='#aec4e9',lw=.75,visible=False,animated=True))
            if not frame.empty:
                ends=frame.attrs['ends']
                count_visible=len(frame) if clock is None else int(np.searchsorted(ends,clock,side='right'))
                if count_visible:
                    x=mdates.date2num(frame.index[:count_visible].tz_localize(BERLIN).to_pydatetime())
                    extent.extend((x[0],x[-1]))
                    original.append((ax,tf,frame,x,count_visible))
                    continue
            ax.text(.5,.48,tr('Нет закрытых свечей в выбранном диапазоне'),transform=ax.transAxes,
                    ha='center',color='#b9c9de',fontsize=9)
        if extent:
            focus=max(extent)
            self.time_bounds=(min(extent)-.12,focus+.25)
            low=max(self.time_bounds[0],focus-7)
            proposed=(low,self.time_bounds[1])
            if clock is not None:
                proposed=(max(self.time_bounds[0],focus-5),focus+.12)
            self.limits=old or proposed
        else:
            self.time_bounds=self.limits=None
        for ax,tf,frame,x,num in original:
            self.candles(ax,tf,frame,x,num)
        if self.axes and self.limits:
            self.axes[0].set_xlim(*self.limits)
            for label,item,timed in events:
                when=datetime.fromisoformat(label)
                when=when if when.tzinfo is not None else when.replace(tzinfo=BERLIN)
                at=mdates.date2num(when)
                if self.time_bounds[0]<=at<=self.time_bounds[1]+31:
                    for ax in self.axes:
                        ax.axvline(at,lw=.65,color='#c19b57',alpha=.48,linestyle='--')
                    description=item.get('title') or item.get('original') or item.get('kind')
                    self.anchors.append((at,description,timed))
        if self.axes:
            locator=mdates.AutoDateLocator(tz=BERLIN,minticks=3,maxticks=6)
            self.axes[0].xaxis.set_major_locator(locator)
            self.axes[0].xaxis.set_major_formatter(mdates.DateFormatter('%d.%m %H:%M',tz=BERLIN))
        self.canvas.draw_idle()

    def candles(self,ax,tf,frame,x,num):
        half=.32*MINUTES.get(tf,1440)/1440
        block=frame.iloc[:num]
        op,hi,lo,cl=block[['open','high','low','close']].to_numpy().T
        colors=np.where(cl>=op,'#39b8a0','#e17885')
        wick=np.stack([np.c_[x,lo],np.c_[x,hi]],axis=1)
        body=np.stack([np.c_[x-half,op],np.c_[x+half,op],np.c_[x+half,cl],np.c_[x-half,cl]],axis=1)
        ax.add_collection(LineCollection(wick,colors=colors,lw=.65))
        ax.add_collection(PolyCollection(body,facecolors=colors,edgecolors=colors,lw=.55))
        ax.plot(x,block.ema,color='#a1bafa',lw=.9)
        gaps=np.asarray(frame.attrs['breaks'][:num])
        if gaps.any():
            g=x[gaps]
            ranges=np.stack([np.c_[g,np.zeros(len(g))],np.c_[g,np.ones(len(g))]],axis=1)
            ax.add_collection(LineCollection(ranges,transform=ax.get_xaxis_transform(),colors='#e2a55e',alpha=.45,lw=.8))
        left,right=self.limits
        use=(x>=left)&(x<=right)
        if not use.any():
            use=np.ones(len(x),dtype=bool)
        self.autoscale(ax,lo[use],hi[use])
        ax._intraday_display=(tf,x,block,lo,hi)

    @staticmethod
    def autoscale(ax,lo,hi):
        low,high=float(lo.min()),float(hi.max())
        pad=max((high-low)*.1,high*.001)
        ax.set_ylim(low-pad,high+pad)

    def viewport(self,limits):
        self.limits=limits
        self.axes[0].set_xlim(*limits)
        for ax in self.axes:
            if hasattr(ax,'_intraday_display'):
                _,x,_,lo,hi=ax._intraday_display
                use=(x>=limits[0])&(x<=limits[1])
                if use.any():
                    self.autoscale(ax,lo[use],hi[use])
        self.canvas.draw_idle()

    def zoom(self,event):
        if event.inaxes not in self.axes or event.xdata is None or not self.limits:
            return
        lo,hi=self.limits
        factor=.77 if event.button=='up' else 1.29
        target=event.xdata
        self.viewport((target+(lo-target)*factor,target+(hi-target)*factor))

    def click(self,event):
        if event.inaxes in self.axes and event.button==1 and self.limits:
            self.drag=(event.x,*self.limits,event.inaxes.bbox.width)

    def release(self,_):
        self.drag=None

    def motion(self,event):
        if event.inaxes not in self.axes or event.xdata is None or self.background is None:
            return
        if self.drag:
            x,lo,hi,width=self.drag
            delta=(event.x-x)*(hi-lo)/width
            self.viewport((lo-delta,hi-delta))
            return
        self.canvas.restore_region(self.background)
        for ax,cursor in zip(self.axes,self.cursors):
            cursor.set_xdata([event.xdata,event.xdata])
            cursor.set_visible(True)
            ax.draw_artist(cursor)
        self.canvas.blit(self.figure.bbox)
        item=getattr(event.inaxes,'_intraday_display',None)
        if item:
            tf,x,frame,_,_=item
            j=min(len(x)-1,max(0,int(np.searchsorted(x,event.xdata,side='right'))-1))
            t=frame.index[j]
            row=frame.iloc[j]
            text=f'{tf}  {t:%d.%m %H:%M}  O {row.open:.2f}  H {row.high:.2f}  L {row.low:.2f}  C {row.close:.2f}  V {row.volume:,.0f}'
            near=[(at,name,timed) for at,name,timed in self.anchors if abs(event.xdata-at)<(self.limits[1]-self.limits[0])*.01]
            if near:
                _,name,timed=near[0]
                text+=' · '+str(name or '')[:90]+('' if timed else ' · '+tr('дата без времени'))
            self.hover(text)
