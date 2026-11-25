"""Bounded mouse capture over the existing drawing identity/draft guards."""
import math
import pandas as pd
from i18n import tr

KINDS=('brush','polyline','polygon')
MAX_POINTS=256


def clear_preview(app):
    artist=getattr(app,'_free_preview',None)
    if artist is not None:
        try:artist.remove()
        except ValueError:pass
    if artist is not None and hasattr(app,'canvas'):app.canvas.draw_idle()
    app._free_preview=None
    app._free_mouse_down=False


def abort(app):
    if getattr(app,'drawing_mode',None) not in KINDS:return
    from drawing_tools import cancel
    clear_preview(app);cancel(app,'Рисование отменено.')
    if hasattr(app,'canvas'):app.canvas.draw_idle()


def point(app,event):
    try:
        if event.inaxes is not app.ax_price or event.xdata is None or event.ydata is None:return None
        if not math.isfinite(event.xdata) or not math.isfinite(event.ydata) or event.ydata<=0:return None
        i=round(event.xdata)
        if not 0<=i<len(app.current_df):return None
        return dict(at=str(app.current_df.index[i]),price=float(event.ydata))
    except (TypeError,ValueError,OverflowError):return None


def add(app,event):
    from drawing_tools import capture_for,current
    kind=app.drawing_mode;p=point(app,event)
    if p is None:return
    capture=capture_for(app,kind)
    if capture is None:clear_preview(app);return
    if capture['points'] and capture['points'][-1]==p:return
    if len(capture['points'])>=MAX_POINTS:
        app.status.set(tr('Достигнут предел 256 точек. Завершите фигуру или нажмите Esc.'));return
    capture['points'].append(p)
    xs=app.current_df.index.get_indexer([pd.Timestamp(v['at']) for v in capture['points']])
    ys=[v['price'] for v in capture['points']]
    if getattr(app,'_free_preview',None) is None:
        app._free_preview,=app.ax_price.plot(xs,ys,color='#00c5ff',linewidth=1.5,scalex=False,scaley=False,zorder=30)
    else:app._free_preview.set_data(xs,ys)
    if hasattr(app,'canvas'):app.canvas.draw_idle()


def finish(app):
    from drawing_tools import build,current,cancel
    from drawing_actions import commit
    capture=getattr(app,'_drawing_capture',None)
    clear_preview(app)
    if capture is None:return
    if not current(app,capture):cancel(app,'Контекст графика изменился. Рисунок не добавлен.');return
    try:obj=build(capture['kind'],capture['points'])
    except ValueError as error:cancel(app,str(error));return
    obj.update(source_mode=app.chart_meta['source'],currency=app.chart_meta['currency'])
    for key in ('venue','symbol'):
        if app.chart_meta.get(key):obj[key]=app.chart_meta[key]
    if commit(app,capture['isin'],capture['expected'],capture['expected']+[obj]):
        app.drawing_mode=None;app.drawing_start=None;app._drawing_capture=None
    else:cancel(app,'Разметка изменилась. Рисунок не добавлен.')


def bind_escape(app):
    if not getattr(app,'_free_escape_bound',False) and hasattr(app,'root'):
        app.root.bind('<Escape>',lambda _:abort(app),add='+');app._free_escape_bound=True


def press(app,event):
    kind=getattr(app,'drawing_mode',None)
    if kind not in KINDS:return False
    bind_escape(app)
    if event.button==3:abort(app);return True
    if event.button!=1:return True
    add(app,event)
    if kind=='brush':app._free_mouse_down=getattr(app,'_drawing_capture',None) is not None
    elif getattr(event,'dblclick',False):finish(app)
    return True


def motion(app,event):
    if getattr(app,'drawing_mode',None)!='brush' or not getattr(app,'_free_mouse_down',False):return False
    add(app,event);return True


def release(app,event):
    if getattr(app,'drawing_mode',None)!='brush' or not getattr(app,'_free_mouse_down',False):return False
    if event.button==1:
        add(app,event);finish(app)
    return True
