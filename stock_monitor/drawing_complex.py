"""Manual wave labels and explicitly anchor-calibrated linear Gann overlays."""
import math

GANN_RATIOS=((1,8),(1,4),(1,3),(1,2),(1,1),(2,1),(3,1),(4,1),(8,1))
PATTERNS={'abcd':('A','B','C','D'),'xabcd':('X','A','B','C','D'),
          'elliott_impulse':('0','1','2','3','4','5'),
          'elliott_correction':('0','A','B','C'),
          'elliott_triangle':('0','A','B','C','D','E')}
MAX_CYCLES=64


def gann_rays(a,b,horizon,boxed=False):
    if not all(math.isfinite(v) for v in (*a,*b,horizon)):raise ValueError('Неверный масштаб Ганна')
    x,y=a;ex,ey=b;dx=ex-x;dy=ey-y
    if dx<=0 or dy==0:raise ValueError('Ганн: вторая точка должна быть позже первой и иметь другую цену')
    slope=dy/dx;end=ex if boxed else max(ex,horizon)
    rays=[]
    for numerator,denominator in GANN_RATIOS:
        ratio=numerator/denominator
        run=min(end-x,dx/ratio) if boxed else end-x
        rays.append((f'{numerator}×{denominator}',(x,y),(x+run,y+slope*ratio*run)))
    return rays


def render(kind,xy,app,frame,style,kw,register):
    if kind not in (*PATTERNS,'gann_fan','gann_box','gann_square','cycles'):return False
    ax=app.ax_price
    def line(xs,ys):
        artist,=ax.plot(xs,ys,scalex=False,scaley=False,**kw);register(artist)
    def label(x,y,text):
        artist=ax.annotate(text,(x,y),xytext=(4,4),textcoords='offset points',color=style['color'],fontsize=style['fontsize'],alpha=style['opacity'],clip_on=True,parse_math=False,zorder=kw['zorder']+.0001)
        register(artist)
    if kind in PATTERNS:
        if len(xy)!=len(PATTERNS[kind]) or any(xy[n+1][0]<=xy[n][0] for n in range(len(xy)-1)):return True
        line([p[0] for p in xy],[p[1] for p in xy])
        for (x,y),text in zip(xy,PATTERNS[kind]):label(x,y,text)
        return True
    (x,y),(ex,ey)=xy
    if ex<=x:return True
    if kind=='cycles':
        count=max(1,math.floor((len(frame)-1-x)/(ex-x))+1)
        for n in range(min(MAX_CYCLES,count)):
            at=x+n*(ex-x)
            artist,=ax.plot([at,at],[0,1],transform=ax.get_xaxis_transform(),scalex=False,scaley=False,**kw);register(artist)
        label(x,y,f'Период: {ex-x:g} бар.'+(' · первые 64 повтора' if count>MAX_CYCLES else ''))
        return True
    if y==ey:return True
    if ax.get_xscale()!='linear' or ax.get_yscale()!='linear':
        label(x,y,'Ганн: требуется линейная шкала');return True
    if kind!='gann_fan':
        line([x,ex,ex,x,x],[y,y,ey,ey,y])
        for ratio in (.25,.5,.75):
            line([x,ex],[y+(ey-y)*ratio]*2)
            line([x+(ex-x)*ratio]*2,[y,ey])
    if kind in ('gann_fan','gann_square'):
        for name,a,b in gann_rays((x,y),(ex,ey),len(frame)-1,boxed=kind=='gann_square'):
            line([a[0],b[0]],[a[1],b[1]]);label(*b,name)
    if kind=='gann_square':line([x,ex],[ey,y])
    label(x,y,f'1×1 A→B · {abs((ey-y)/(ex-x)):.5g} {frame.attrs.get("currency") or "цены"}/бар')
    return True
