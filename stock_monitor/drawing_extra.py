"""Geometric overlays only. Prices/points remain explicit user annotations."""
import math
import numpy as np
import pandas as pd
from matplotlib.patches import Ellipse,Polygon,Circle
from matplotlib.transforms import Affine2D,IdentityTransform
from matplotlib.colors import to_rgba
from drawing_tools import fib_levels,REGISTRY
from drawing_complex import render as complex_render


class DataCircle(Circle):
    """A visually round circle, anchored to data through pan/resize/zoom."""
    def __init__(self,axes,centre,radius_point,**kwargs):
        self._drawing_axes=axes;self._centre=centre;self._radius_point=radius_point
        super().__init__((0,0),1,transform=IdentityTransform(),**kwargs)

    def get_patch_transform(self):
        a,b=self._drawing_axes.transData.transform([self._centre,self._radius_point])
        radius=float(np.linalg.norm(b-a))
        return Affine2D().scale(radius).translate(*a)


def extra(app,obj,frame,locate,style,kw,register):
    kind=obj['type']
    if kind not in REGISTRY or kind in ('hline','vline','trend','ray','zone','marker'):return False
    points=obj['vertices'] if 'vertices' in obj else [obj]+[obj[k] for k in ('end','third') if k in obj]
    if not isinstance(points,list):return True
    if len(points)!=REGISTRY[kind]['points']:return True
    if 'vertices' in obj and any(points[0].get(key)!=obj.get(key) for key in ('at','price')):return True
    if any(not frame.index[0]<=pd.Timestamp(p['at'])<=frame.index[-1] for p in points):return True
    xy=[(locate(p['at']),float(p['price'])) for p in points]
    if any(not math.isfinite(x) or not math.isfinite(y) or y<=0 for x,y in xy):return True
    x,y=xy[0];ax=app.ax_price
    if complex_render(kind,xy,app,frame,style,kw,register):return True
    def line(xs,ys,**overrides):
        artist,=ax.plot(xs,ys,**{**kw,**overrides});register(artist)
    def label(x,y,text,**options):
        artist=ax.annotate(text,(x,y),xytext=(5,5),textcoords='offset points',color=style['color'],fontsize=style['fontsize'],alpha=style['opacity'],clip_on=True,parse_math=False,zorder=kw['zorder']+.0001,**options)
        return artist
    if kind in ('text','note'):
        text=str(obj.get('text',''))[:240]
        if text:
            options={'bbox':dict(boxstyle='round,pad=.4',facecolor=to_rgba(style['fill'],style['fill_opacity']),edgecolor=style['color'])} if kind=='note' else {}
            register(label(x,y,text,**options))
        return True
    ex,ey=xy[1]
    if kind=='extended':register(ax.axline((x,y),(ex,ey),**kw))
    elif kind in ('ellipse','circle','triangle'):
        patch_kw={k:v for k,v in kw.items() if k not in ('color','alpha')}
        patch_kw.update(edgecolor=to_rgba(style['color'],style['opacity']),facecolor=to_rgba(style['fill'],style['fill_opacity']))
        if kind=='ellipse':patch=Ellipse(((x+ex)/2,(y+ey)/2),abs(ex-x),abs(ey-y),**patch_kw)
        elif kind=='circle':patch=DataCircle(ax,xy[0],xy[1],**patch_kw)
        else:patch=Polygon(xy,closed=True,**patch_kw)
        register(ax.add_patch(patch))
    elif kind in ('arrow','double_arrow'):
        artist=ax.annotate('',xy=(ex,ey),xytext=(x,y),arrowprops=dict(arrowstyle='<->' if kind=='double_arrow' else '->',**kw),annotation_clip=True,zorder=kw['zorder'])
        register(artist.arrow_patch)
    elif kind=='measure':
        line([x,ex],[y,ey]);seconds=(pd.Timestamp(points[1]['at'])-pd.Timestamp(points[0]['at'])).total_seconds()
        register(label(ex,ey,f'{ey-y:+.4g} ({100*(ey-y)/y:+.2f}%) · {abs(ex-x):g} бар. · {abs(seconds)/60:g} мин.'))
    elif kind in ('fib','fib_extension'):
        if kind=='fib':levels=fib_levels(y,ey);left,right=min(x,ex),max(x,ex)
        else:levels=fib_levels(y,ey,xy[2][1]);left=min(p[0] for p in xy);right=max(len(frame)-1,max(p[0] for p in xy))
        for ratio,price in levels:
            line([left,right],[price,price])
            register(label(right,price,f'{ratio:g}'+(f' · {price:.4g}' if style['price_label'] else '')))
        line([x,ex],[y,ey],alpha=style['opacity']*.5)
    elif kind=='channel':
        cx,cy=xy[2]
        if ex==x:return True
        offset=cy-(y+(ey-y)*(cx-x)/(ex-x))
        line([x,ex],[y,ey]);line([x,ex],[y+offset,ey+offset])
        fill=ax.fill([x,ex,ex,x],[y,ey,ey+offset,y+offset],color=style['fill'],alpha=style['fill_opacity'],zorder=kw['zorder']-.001)[0]
        register(fill)
    return True
