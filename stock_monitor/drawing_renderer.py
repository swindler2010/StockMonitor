"""Render user annotations only; never modifies candles, indicators or CORE."""
import math
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle
from matplotlib.colors import to_rgba
from drawing_styles import effective


def draw(app,objects,frame,color):
    app.drawing_artists=[]
    if frame.empty:return
    coordinates=frame.index.as_unit('ns').asi8
    positions=np.arange(len(frame))
    mode=frame.attrs.get('source');currency=frame.attrs.get('currency')
    def locate(at):return float(np.interp(pd.Timestamp(at).value,coordinates,positions))
    for index,obj in enumerate(objects):
        if obj.get('visible',True) is False:continue
        if obj.get('source_mode',mode)!=mode or obj.get('currency',currency)!=currency:continue
        try:
            kind=obj['type'];price=float(obj.get('price',0));at=obj.get('at')
            if not math.isfinite(price) or price<0:continue
            style=effective(obj,color)
            order=obj.get('stack_order')
            zorder=2+index*.001 if type(order) is int else 1 if kind=='zone' else 2
            kw=dict(color=style['color'],linewidth=style['width'],linestyle=style['linestyle'],alpha=style['opacity'],zorder=zorder)
            def register(artist):
                if not obj.get('locked',False):artist.set_picker(6);app.drawing_artists.append((artist,index))
            if kind=='hline':register(app.ax_price.axhline(price,**kw));continue
            if not at or not frame.index[0]<=pd.Timestamp(at)<=frame.index[-1]:continue
            x=locate(at)
            if kind=='vline':register(app.ax_price.axvline(x,**kw));continue
            if kind=='marker':
                artist,=app.ax_price.plot([x],[price],marker='o',**kw);register(artist)
                if style['price_label']:app.ax_price.annotate(f'{price:g}',(x,price),xytext=(5,5),textcoords='offset points',color=style['color'],alpha=style['opacity'],fontsize=style['fontsize'])
                continue
            if kind not in ('zone','trend','ray'):continue
            end=obj['end'];ex=locate(end['at']);ey=float(end['price'])
            if not math.isfinite(ey) or ey<=0:continue
            if kind=='zone':
                # Separate edge/fill alpha preserves the old .18 filled zone.
                kw.pop('color');kw.pop('alpha')
                register(app.ax_price.add_patch(Rectangle((min(x,ex),min(price,ey)),abs(ex-x),abs(ey-price),
                    edgecolor=to_rgba(style['color'],style['opacity']),facecolor=to_rgba(style['fill'],style['fill_opacity']),**kw)))
            else:
                if kind=='ray' and ex!=x:ey=price+(ey-price)/(ex-x)*(len(frame)-1-x);ex=len(frame)-1
                artist,=app.ax_price.plot([x,ex],[price,ey],**kw);register(artist)
        except (KeyError,TypeError,ValueError,OverflowError):continue
