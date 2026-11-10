"""Edits only user drawing drafts; never touches candles or trading state."""
import copy
import math
from i18n import tr, rendered_message


def chart_identity(app):
    meta=getattr(app,'chart_meta',{})
    return tuple(meta.get(key) for key in ('source','venue','currency','symbol','timeframe','base_tf'))


class History:
    def __init__(self, limit=100):
        self.limit=limit;self.undo_items=[];self.redo_items=[]

    def change(self, before, after):
        if before==after:return
        self.undo_items.append((copy.deepcopy(before),copy.deepcopy(after)))
        self.undo_items=self.undo_items[-self.limit:];self.redo_items.clear()

    def undo(self,current):
        if not self.undo_items:return copy.deepcopy(current)
        before,after=self.undo_items[-1]
        if current!=after:raise ValueError('DRAWINGS_CHANGED_OUTSIDE_HISTORY')
        self.undo_items.pop();self.redo_items.append((before,after));return copy.deepcopy(before)

    def redo(self,current):
        if not self.redo_items:return copy.deepcopy(current)
        before,after=self.redo_items[-1]
        if current!=before:raise ValueError('DRAWINGS_CHANGED_OUTSIDE_HISTORY')
        self.redo_items.pop();self.undo_items.append((before,after));return copy.deepcopy(after)


def modify(objects,index,operation,value=None):
    result=copy.deepcopy(objects)
    if type(index)!=int or not 0<=index<len(result):raise ValueError('OBJECT_NOT_FOUND')
    if operation=='copy':result.insert(index+1,copy.deepcopy(result[index]))
    elif operation=='delete':result.pop(index)
    elif operation=='price':
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:raise ValueError('INVALID_PRICE')
        if result[index].get('type')=='vline':raise ValueError('VERTICAL_LINE_HAS_NO_PRICE')
        delta=value-result[index]['price'];result[index]['price']=value
        if 'end' in result[index]:result[index]['end']['price']+=delta
        if result[index].get('end',{}).get('price',value)<=0:raise ValueError('INVALID_END_PRICE')
    elif operation=='at':
        import pandas as pd
        target=pd.Timestamp(value);old=pd.Timestamp(result[index]['at'])
        if pd.isna(target):raise ValueError('INVALID_TIME')
        delta=target-old;result[index]['at']=str(target)
        if 'end' in result[index]:result[index]['end']['at']=str(pd.Timestamp(result[index]['end']['at'])+delta)
    else:raise ValueError('UNKNOWN_OPERATION')
    return result


def commit(app,isin,expected,changed):
    if app.instrument_cfg()['isin']!=isin:return False
    info=app.instrument_cfg();draft=copy.deepcopy(app.user_state(info))
    if draft.get('drawings',[])!=expected:return False
    if expected==changed:return True
    histories=getattr(app,'_drawing_histories',{});app._drawing_histories=histories
    histories.setdefault(isin,History()).change(expected,changed)
    draft['drawings']=copy.deepcopy(changed);app.drafts[isin]=draft;app.user_dirty=True
    app.refresh_chart();app.status.set(tr('Разметка изменена. Нажмите «Сохранить».'))
    return True


def begin_drag(app,event):
    if event.ydata is None:return False
    for artist,index in reversed(getattr(app,'drawing_artists',[])):
        if artist.contains(event)[0]:
            info=app.instrument_cfg()
            app._drawing_drag=dict(isin=info['isin'],expected=copy.deepcopy(app.user_state(info).get('drawings',[])),
                index=index,x=event.xdata,y=event.ydata,dates=app.current_df.index.copy(),meta=chart_identity(app))
            app.drag=None;return True
    return False


def travel(app,isin,operation):
    if app.instrument_cfg()['isin']!=isin:return False
    history=getattr(app,'_drawing_histories',{}).get(isin)
    if history is None:return False
    draft=copy.deepcopy(app.user_state(app.instrument_cfg()));current=draft.get('drawings',[])
    try:changed=getattr(history,operation)(current)
    except ValueError:return False
    if changed==current:return False
    draft['drawings']=changed;app.drafts[isin]=draft;app.user_dirty=True;app.refresh_chart()
    app.status.set(tr('Разметка изменена. Нажмите «Сохранить».'));return True


def finish_drag(app,event):
    saved=getattr(app,'_drawing_drag',None);app._drawing_drag=None
    if saved is None:return False
    if (event.inaxes!=app.ax_price or event.xdata is None or event.ydata is None or
        app.instrument_cfg()['isin']!=saved['isin'] or chart_identity(app)!=saved['meta'] or
        not app.current_df.index.equals(saved['dates'])):return True
    objects=saved['expected'];index=saved['index'];obj=objects[index]
    try:
        changed=copy.deepcopy(objects)
        if obj['type']!='vline':changed=modify(changed,index,'price',obj['price']+event.ydata-saved['y'])
        if obj['type']!='hline':
            import pandas as pd
            dates=saved['dates'];a=max(0,min(len(dates)-1,round(saved['x'])));b=max(0,min(len(dates)-1,round(event.xdata)))
            changed=modify(changed,index,'at',pd.Timestamp(obj['at'])+(dates[b]-dates[a]))
        commit(app,saved['isin'],objects,changed)
    except (ValueError,TypeError,KeyError):app.status.set(tr('Нельзя переместить объект в эту точку'))
    return True


def context_menu(app,event,index):
    import tkinter as tk
    from tkinter import simpledialog,messagebox
    info=app.instrument_cfg();isin=info['isin']
    expected=copy.deepcopy(app.user_state(info).get('drawings',[]))
    expected_meta=chart_identity(app)
    histories=getattr(app,'_drawing_histories',{});app._drawing_histories=histories
    history=histories.setdefault(isin,History())
    def apply(operation):
        if app.instrument_cfg()['isin']!=isin:return
        draft=copy.deepcopy(app.user_state(info));current=draft.get('drawings',[])
        if current!=expected:return
        try:
            if operation in ('undo','redo'):changed=getattr(history,operation)(current)
            else:
                value=None
                if operation=='price':
                    value=simpledialog.askfloat(tr('Цена объекта'),tr('Новая цена начальной точки'),initialvalue=current[index].get('price'),parent=app.root)
                    if value is None:return
                if operation=='at':
                    value=simpledialog.askstring(tr('Дата объекта'),tr('Новая дата начальной точки'),initialvalue=current[index].get('at'),parent=app.root)
                    if value is None:return
                # A modal dialog runs Tk callbacks: recheck identity and draft after it closes.
                if app.instrument_cfg()['isin']!=isin or app.user_state(app.instrument_cfg()).get('drawings',[])!=expected or chart_identity(app)!=expected_meta:return
                changed=modify(current,index,operation,value);history.change(current,changed)
            draft['drawings']=changed;app.drafts[isin]=draft;app.user_dirty=True;app.refresh_chart()
            app.status.set(tr('Разметка изменена. Нажмите «Сохранить».'))
        except ValueError as exc:messagebox.showerror(tr('Разметка'),rendered_message(str(exc)),parent=app.root)
    menu=tk.Menu(app.root,tearoff=False)
    for label,operation in [('Копировать','copy'),('Изменить цену','price'),('Изменить дату','at'),('Удалить','delete'),('Отменить','undo'),('Повторить','redo')]:
        disabled=(operation=='price' and expected[index].get('type')=='vline') or (operation=='at' and expected[index].get('type')=='hline')
        menu.add_command(label=tr(label),command=lambda op=operation:apply(op),state='disabled' if disabled else 'normal')
    widget=app.canvas.get_tk_widget()
    try:menu.tk_popup(widget.winfo_rootx()+int(event.x),widget.winfo_rooty()+widget.winfo_height()-int(event.y))
    finally:menu.grab_release()
