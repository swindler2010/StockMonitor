"""Render only saved manual geometry and explicit frozen regression snapshots."""
import math
from matplotlib.patches import Polygon
from matplotlib.colors import to_rgba
from i18n import tr


def render(kind,obj,xy,app,frame,style,kw,register):
    if kind not in ('brush','polygon','polyline','regression'):return False
    ax=app.ax_price
    def line(xs,ys):
        artist,=ax.plot(xs,ys,scalex=False,scaley=False,**kw);register(artist)
    def label(text):
        register(ax.annotate(text,xy[0],xytext=(4,4),textcoords='offset points',color=style['color'],alpha=style['opacity'],
                             fontsize=style['fontsize'],clip_on=True,parse_math=False,zorder=kw['zorder']+.0001))
    if kind in ('brush','polyline'):
        line([p[0] for p in xy],[p[1] for p in xy]);return True
    if kind=='polygon':
        patch=Polygon(xy,closed=True,edgecolor=to_rgba(style['color'],style['opacity']),facecolor=to_rgba(style['fill'],style['fill_opacity']),
                      linewidth=style['width'],linestyle=style['linestyle'],zorder=kw['zorder'])
        ax.add_artist(patch);patch.set_clip_path(ax.patch);register(patch);return True
    data=obj.get('regression') or {}
    if data.get('rule')!='OLS_CLOSE_POPULATION_RESIDUAL_2SIGMA_SNAPSHOT':label(tr('Нет снимка регрессии'));return True
    if ax.get_xscale()!='linear' or ax.get_yscale()!='linear':label(tr('Регрессия: требуется линейная шкала'));return True
    width=data.get('half_width')
    if not isinstance(width,(int,float)) or not math.isfinite(width) or width<0:return True
    (x,y),(ex,ey)=xy
    for delta in (-width,0,width):line([x,ex],[y+delta,ey+delta])
    label(tr('Смещённый снимок регрессии ±2σ' if obj.get('regression_transformed') else 'Снимок регрессии ±2σ')+' · '+str(data.get('samples','—'))+' · '+str(obj.get('regression_tf') or '—'))
    return True
