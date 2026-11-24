"""Bounded manual geometry. No quote, indicator, account or execution access."""
import math
import numpy as np
from matplotlib.path import Path
from matplotlib.patches import PathPatch, Rectangle, Circle, Arc
from matplotlib.text import Annotation
from matplotlib.transforms import IdentityTransform, Affine2D
from matplotlib.offsetbox import DrawingArea, AnnotationBbox
from matplotlib.lines import Line2D
from i18n import tr

KINDS=('angle','offset_channel','fib_spiral','price_projection','time_projection',
       'long_position','short_position','callout','icon','smile','flag')
RULE='MANUAL_PROJECTIONS_DECORATIONS_V1'


def position_levels(kind,entry,stop,target):
    if not all(math.isfinite(v) and v>0 for v in (entry,stop,target)):
        raise ValueError('Уровни модели должны быть положительными')
    if not (stop<entry<target if kind=='long_position' else target<entry<stop):
        raise ValueError('Long: стоп < вход < цель. Short: цель < вход < стоп.')
    risk=abs(entry-stop);reward=abs(target-entry)
    return risk,reward,reward/risk


class ScreenSpiral(PathPatch):
    """Three turns, radius multiplies by phi every 90 degrees; B is outer end."""
    def __init__(self,axes,a,b,**kwargs):
        self.owner=axes;self.a=a;self.b=b
        theta=np.linspace(0,6*math.pi,321)
        radius=np.exp(math.log((1+math.sqrt(5))/2)/(math.pi/2)*(theta-6*math.pi))
        vertices=np.column_stack((radius*np.cos(theta),radius*np.sin(theta)))
        super().__init__(Path(vertices),transform=IdentityTransform(),fill=False,**kwargs)

    def get_patch_transform(self):
        a,b=self.owner.transData.transform([self.a,self.b]);delta=b-a
        return Affine2D().scale(float(np.linalg.norm(delta))).rotate(math.atan2(delta[1],delta[0])).translate(*a)


class ScreenAngle(Annotation):
    def __init__(self,axes,a,b,**kwargs):
        self.owner=axes;self.a=a;self.b=b
        super().__init__('',a,xytext=(5,6),textcoords='offset points',**kwargs)

    def draw(self,renderer):
        a,b=self.owner.transData.transform([self.a,self.b]);delta=b-a
        angle=math.degrees(math.atan2(delta[1],delta[0]))
        self.set_text(tr('Экранный угол')+f': {angle:+.1f}°')
        super().draw(renderer)


def render(kind,obj,xy,app,frame,style,kw,register):
    if kind not in KINDS:return False
    ax=app.ax_price;a=xy[0];x,y=a
    def line(xs,ys,**options):
        artist,=ax.plot(xs,ys,scalex=False,scaley=False,**{**kw,**options});register(artist)
    def label(point,text,**options):
        props=dict(xytext=(5,5),textcoords='offset points',color=style['color'],
                   fontsize=style['fontsize'],alpha=style['opacity'],clip_on=True,parse_math=False,
                   zorder=kw['zorder']+.0001)
        item=ax.annotate(text,point,**{**props,**options});register(item)
    def patch(item):
        ax.add_artist(item);item.set_clip_path(ax.patch);register(item)
    if obj.get('annotation_rule')!=RULE:
        label(a,tr('Неизвестная версия ручной разметки'));return True
    if kind=='callout':
        text=obj.get('text','')
        if not isinstance(text,str) or not text.strip() or len(text)>240:return True
        label(a,text,xytext=(30,25),xycoords='data',bbox=dict(boxstyle='round,pad=.35',facecolor=style['fill'],edgecolor=style['color']),
              arrowprops=dict(arrowstyle='->',color=style['color']),annotation_clip=True)
        return True
    if kind in ('icon','smile','flag'):
        if kind=='icon':
            line([x],[y],marker='*',markersize=style['fontsize']*1.8,linestyle='None',markerfacecolor=style['fill'])
            return True
        area=DrawingArea(30,30,0,0)
        def add(item):item.set_alpha(style['opacity']);area.add_artist(item)
        if kind=='smile':
            add(Circle((15,15),12,facecolor='none',edgecolor=style['color'],linewidth=style['width']))
            for cx in (10,20):add(Circle((cx,18),1.2,color=style['color']))
            add(Arc((15,15),14,14,theta1=205,theta2=335,color=style['color'],linewidth=style['width']))
        else:
            add(Line2D([8,8],[2,28],color=style['color'],linewidth=style['width']))
            add(PathPatch(Path([(8,27),(25,24),(20,18),(8,20),(8,27)]),
                          facecolor=style['fill'],edgecolor=style['color'],linewidth=style['width']))
        item=AnnotationBbox(area,a,frameon=False,box_alignment=(.5,.5),annotation_clip=True,zorder=kw['zorder'])
        patch(item);return True
    b=xy[1];ex,ey=b
    if kind=='fib_spiral':
        patch(ScreenSpiral(ax,a,b,edgecolor=style['color'],linewidth=style['width'],linestyle=style['linestyle'],
                           alpha=style['opacity'],zorder=kw['zorder']))
        label(a,tr('Спираль: 3 витка, экранный масштаб'));return True
    if kind=='angle':
        line([x,ex],[y,ey]);line([x,ex if ex!=x else x+1],[y,y],alpha=style['opacity']*.4)
        item=ScreenAngle(ax,a,b,color=style['color'],fontsize=style['fontsize'],alpha=style['opacity'],
                         clip_on=True,parse_math=False,zorder=kw['zorder']+.0001)
        patch(item);return True
    c=xy[2];cx,cy=c
    if kind=='offset_channel':
        if ex==x:return True
        if ax.get_yscale()!='linear':label(a,tr('Канал: требуется линейная шкала'));return True
        offset=cy-(y+(ey-y)*(cx-x)/(ex-x))
        for delta in (-offset,0,offset):line([x,ex],[y+delta,ey+delta])
        from matplotlib.patches import Polygon
        patch(Polygon([(x,y-offset),(ex,ey-offset),(ex,ey+offset),(x,y+offset)],closed=True,
                      facecolor=style['fill'],edgecolor='none',alpha=style['fill_opacity'],zorder=kw['zorder']-.001))
        label(a,tr('Смещённый канал: A–B — центр'));return True
    if kind in ('price_projection','time_projection'):
        line([x,ex],[y,ey],alpha=style['opacity']*.4)
        if kind=='price_projection':
            end=(cx,cy+ey-y)
            if end[1]<=0:return True
            text=tr('Проекция цены')+f': {ey-y:+.5g} → {end[1]:.5g}'
        else:
            end=(cx+ex-x,cy)
            text=tr('Проекция времени')+f': +{ex-x:g} '+tr('баров')
        item=ax.annotate('',xy=end,xytext=c,arrowprops=dict(arrowstyle='->',color=style['color'],
                          linewidth=style['width'],alpha=style['opacity']),annotation_clip=True,zorder=kw['zorder'])
        register(item.arrow_patch);label(c,text);return True
    try:risk,reward,ratio=position_levels(kind,y,ey,cy)
    except ValueError:return True
    right=max(ex,cx)
    if right<=x:return True
    for level,color in ((ey,'#ff5866'),(cy,'#25e772')):
        patch(Rectangle((x,min(y,level)),right-x,abs(level-y),facecolor=color,edgecolor=color,
                        alpha=style['fill_opacity'],linewidth=style['width'],zorder=kw['zorder']))
        line([x,right],[level,level],color=color)
    line([x,right],[y,y])
    label(a,('Long' if kind=='long_position' else 'Short')+f' · R/R {ratio:.2f} · '+tr('ручная модель, без издержек'))
    if style['price_label']:
        for level,name in ((y,'Вход'),(ey,'Стоп'),(cy,'Цель')):label((right,level),tr(name)+f' {level:.5g}')
    return True
