"""Bounded least-squares snapshot in the existing readonly Dispatcher lane."""
import copy
import hashlib
import numpy as np
import pandas as pd
from i18n import tr

MAX_BARS=5000


def fit(values):
    y=np.asarray(values,dtype=float)
    if y.ndim!=1 or not 3<=len(y)<=MAX_BARS or not np.isfinite(y).all() or np.any(y<=0):
        raise ValueError('Регрессия: требуется от 3 до 5000 корректных цен закрытия')
    x=np.arange(len(y),dtype=float);center=x-x.mean()
    slope=float(np.dot(center,y-y.mean())/np.dot(center,center))
    base=float(y.mean()-slope*x.mean());residual=y-(base+slope*x)
    sigma=float(np.sqrt(np.mean(residual**2)))
    start,end=base,base+slope*(len(y)-1)
    if min(start,end)-2*sigma<=0:raise ValueError('Регрессия: канал выходит за допустимые цены')
    return dict(start=start,end=end,half_width=2*sigma,sigma=sigma,samples=len(y),
                rule='OLS_CLOSE_POPULATION_RESIDUAL_2SIGMA_SNAPSHOT',digest=hashlib.sha256(y.tobytes()).hexdigest())


def values_for(app,capture):
    a,b=[pd.Timestamp(p['at']) for p in capture['points']]
    frame=app.current_df.loc[a:b]
    if not 3<=len(frame)<=MAX_BARS:raise ValueError('Регрессия: требуется от 3 до 5000 корректных цен закрытия')
    return frame['close'].to_numpy(dtype=float,copy=True)


def submit(app,capture):
    from drawing_tools import current,build,cancel
    from drawing_actions import commit
    capture=copy.deepcopy(capture);generation=getattr(app,'_drawing_generation',0)
    try:values=values_for(app,capture)
    except (ValueError,KeyError) as error:cancel(app,str(error));return False
    app.drawing_mode=None;app._drawing_capture=None;app.drawing_start=None
    app.status.set(tr('Расчёт снимка регрессии…'))
    def done(result):
        if getattr(app,'closing',False) or generation!=getattr(app,'_drawing_generation',0):return
        if not current(app,capture):app.status.set(tr('Контекст графика изменился. Рисунок не добавлен.'));return
        try:
            if not np.array_equal(values,values_for(app,capture)):raise ValueError('Цены изменились. Повторите построение регрессии.')
            points=copy.deepcopy(capture['points']);points[0]['price']=result['start'];points[1]['price']=result['end']
            obj=build('regression',points);obj['regression']=result;obj['regression_tf']=capture['meta'][4]
            obj.update(source_mode=app.chart_meta['source'],currency=app.chart_meta['currency'])
            for key in ('venue','symbol'):
                if app.chart_meta.get(key):obj[key]=app.chart_meta[key]
            if not commit(app,capture['isin'],capture['expected'],capture['expected']+[obj]):raise ValueError('Разметка изменилась. Рисунок не добавлен.')
        except (ValueError,KeyError) as error:app.status.set(tr(str(error)))
    def failed(message):
        if not getattr(app,'closing',False) and generation==getattr(app,'_drawing_generation',0):app.status.set(tr(str(message)))
    accepted=app.job(lambda:fit(values),done,failed,lane='chart',key=('drawing-regression',capture['isin'],generation))
    if not accepted:failed('Очередь занята. Повторите построение регрессии.')
    return bool(accepted)
