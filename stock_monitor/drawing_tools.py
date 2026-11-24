"""Bounded annotation registry, construction and guarded multi-point capture."""
import copy
import math
import pandas as pd
from i18n import tr
from drawing_actions import chart_identity,commit
from drawing_complex import PATTERNS

GROUPS={
    'Линии тренда':(('Горизонталь','hline',1),('Вертикаль','vline',1),('Трендовая линия','trend',2),('Луч','ray',2),('Расширенная линия','extended',2),('Линия с углом','angle',2)),
    'Каналы':(('Параллельный канал','channel',3),('Регрессионный канал','regression',2),('Смещённый канал','offset_channel',3)),
    'Свободная разметка':(('Кисть','brush',0),('Ломаная','polyline',0),('Многоугольник','polygon',0)),
    'Фибоначчи':(('Коррекция Фибоначчи','fib',2),('Расширение Фибоначчи','fib_extension',3),('Веер Фибоначчи','fib_fan',2),('Дуги Фибоначчи','fib_arcs',2),('Временные зоны Фибоначчи','fib_timezones',2),('Спираль Фибоначчи','fib_spiral',2)),
    'Ганн':(('Веер Ганна','gann_fan',2),('Сетка Ганна','gann_box',2),('Квадрат Ганна','gann_square',2)),
    'Паттерны и волны':(('ABCD (разметка)','abcd',4),('XABCD (разметка)','xabcd',5),('Импульс Эллиотта','elliott_impulse',6),('Коррекция Эллиотта','elliott_correction',4),('Треугольник Эллиотта','elliott_triangle',6),('Циклы','cycles',2)),
    'Фигуры и аннотации':(('Прямоугольник','zone',2),('Эллипс','ellipse',2),('Круг','circle',2),('Треугольник','triangle',3),('Текст','text',1),('Заметка','note',1),('Выноска','callout',1),('Иконка (звезда)','icon',1),('Смайл (улыбка)','smile',1),('Флаг','flag',1)),
    'Позиции и измерения':(('Маркер цены','marker',1),('Стрелка','arrow',2),('Двойная стрелка','double_arrow',2),('Измерение диапазона','measure',2),('Проекция цены','price_projection',3),('Проекция времени','time_projection',3),('Long Position','long_position',3),('Short Position','short_position',3)),
}
REGISTRY={kind:dict(name=name,points=count,group=group) for group,items in GROUPS.items() for name,kind,count in items}
FIB=(0.,.236,.382,.5,.618,.786,1.)
EXTENSION=(0.,.618,1.,1.618,2.618)


def text_value(value):
    if not isinstance(value,str) or not 1<=len(value.strip())<=240 or any(ord(c)<32 and c not in '\n\t' for c in value):raise ValueError('Текст: от 1 до 240 символов')
    return value.strip()


def build(kind,points,text=None):
    if kind not in REGISTRY:raise ValueError('Неизвестный инструмент')
    count=REGISTRY[kind]['points']
    if (count and len(points)!=count) or (not count and not (3 if kind=='polygon' else 2)<=len(points)<=256):raise ValueError('Неверное число точек')
    points=copy.deepcopy(points)
    for p in points:
        if set(p)!={'at','price'} or isinstance(p['price'],bool) or not math.isfinite(float(p['price'])) or float(p['price'])<=0 or pd.isna(pd.Timestamp(p['at'])):raise ValueError('Неверная точка')
        p['price']=float(p['price']);p['at']=str(pd.Timestamp(p['at']))
    if len(points)>1:
        a,b=points[:2];dx=(pd.Timestamp(b['at'])-pd.Timestamp(a['at'])).total_seconds();dy=b['price']-a['price']
        if dx==0 and dy==0:raise ValueError('Точки должны различаться')
        if kind in ('zone','ellipse') and (dx==0 or dy==0):raise ValueError('Фигуре нужны ширина и высота')
        if kind in ('fib','fib_extension') and (dx==0 or dy==0):raise ValueError('Фибоначчи: нужны разные время и цена')
        if kind=='regression' and dx<=0:raise ValueError('Каналу нужны две разные отметки времени')
        if kind in ('channel','offset_channel') and dx==0:raise ValueError('Каналу нужны две разные отметки времени')
        if kind.startswith('gann_') and (dx<=0 or dy==0):raise ValueError('Ганн: вторая точка должна быть позже первой и иметь другую цену')
        if kind in ('fib_fan','fib_arcs') and (dx<=0 or dy==0):raise ValueError('Фибоначчи: вторая точка должна быть позже первой и иметь другую цену')
        if kind=='fib_timezones' and dx<=0:raise ValueError('Временные зоны: вторая точка должна быть позже первой')
        if kind=='cycles' and dx<=0:raise ValueError('Циклы: вторая точка должна быть позже первой')
        if kind in PATTERNS and any(pd.Timestamp(points[n+1]['at'])<=pd.Timestamp(points[n]['at']) for n in range(len(points)-1)):raise ValueError('Точки разметки должны идти по времени слева направо')
        if len(points)==3:
            c=points[2];cx=(pd.Timestamp(c['at'])-pd.Timestamp(a['at'])).total_seconds();cy=c['price']-a['price']
            if kind in ('triangle','channel','offset_channel') and abs(dx*cy-dy*cx)<=1e-12:raise ValueError('Третья точка должна находиться вне первой линии')
    if kind=='polygon':
        a,b=points[:2];dx=(pd.Timestamp(b['at'])-pd.Timestamp(a['at'])).total_seconds();dy=b['price']-a['price']
        if not any(abs(dx*(p['price']-a['price'])-dy*(pd.Timestamp(p['at'])-pd.Timestamp(a['at'])).total_seconds())>1e-9 for p in points[2:]):raise ValueError('Многоугольнику нужны три неколлинеарные вершины')
    obj=dict(type=kind,**points[0])
    if len(points)>3 or kind in ('brush','polygon','polyline'):obj['vertices']=points
    else:
        if len(points)>1:obj['end']=points[1]
        if len(points)>2:obj['third']=points[2]
    if kind.startswith('gann_'):obj['gann_calibration']='anchors_1x1_linear'
    if kind in ('fib_fan','fib_arcs','fib_timezones'):obj['fib_geometry']={'fib_fan':'linear_price_fan','fib_arcs':'screen_radius_AB','fib_timezones':'visible_bar_intervals'}[kind]
    if kind in PATTERNS:obj['pattern_mode']='manual_labels_only'
    if kind in ('text','note','callout'):obj['text']=text_value(text)
    from drawing_projection import KINDS,RULE,position_levels
    if kind in KINDS:
        if any(p['price']>1e12 for p in points):raise ValueError('Цена ручной модели превышает допустимый предел')
        obj['annotation_rule']=RULE
    if kind in ('long_position','short_position'):
        position_levels(kind,points[0]['price'],points[1]['price'],points[2]['price'])
        if any(pd.Timestamp(p['at'])<=pd.Timestamp(points[0]['at']) for p in points[1:]):raise ValueError('Стоп и цель: укажите точки правее входа')
    if kind=='price_projection' and points[2]['price']+points[1]['price']-points[0]['price']<=0:raise ValueError('Проекция цены должна быть положительной')
    if kind=='time_projection' and pd.Timestamp(points[1]['at'])<=pd.Timestamp(points[0]['at']):raise ValueError('Проекция времени: B должна быть позже A')
    return obj


def fib_levels(a,b,c=None):
    return [(r,b+(a-b)*r) for r in FIB] if c is None else [(r,c+(b-a)*r) for r in EXTENSION]


def start(app,kind):
    if kind not in REGISTRY:raise ValueError('Неизвестный инструмент')
    from drawing_freeform import clear_preview
    clear_preview(app)
    app._drawing_generation=getattr(app,'_drawing_generation',0)+1
    if getattr(app,'measuring',False):app.clear_measurement()
    app.drawing_mode=kind;app.drawing_start=None;app._drawing_capture=None
    count=REGISTRY[kind]['points']
    if kind in ('brush','polygon','polyline'):
        from drawing_freeform import bind_escape
        bind_escape(app)
    hints={'circle':'Укажите центр круга, затем точку радиуса.', 'fib':'Укажите начало и конец движения; 0 — у конца, 1 — у начала.',
           'fib_extension':'Укажите начало движения, конец движения и конец коррекции.', 'channel':'Укажите две точки первой линии и точку параллельной границы.'}
    hints.update(fib_fan='Веер: укажите начало и конец движения слева направо; уровни по линейной цене.',fib_arcs='Дуги: укажите A и B слева направо. Центр B; радиус зависит от масштаба экрана.',fib_timezones='Временные зоны: укажите начало и конец базового интервала слева направо. Шаг — бары текущего ТФ.')
    hints.update(brush='Кисть: удерживайте левую кнопку мыши; отпускание завершает штрих. Esc — отмена.',polygon='Многоугольник: клики добавляют вершины; двойной клик завершает. Esc — отмена.',polyline='Ломаная: клики добавляют вершины; двойной клик завершает. Esc — отмена.',regression='Регрессия: укажите начало и конец участка. Снимок по close, канал ±2σ; не сигнал.')
    hints.update(angle='Угол измеряется на экране и меняется при масштабе; укажите A и B.',offset_channel='A–B — центр канала; третья точка задаёт смещение границы.',fib_spiral='Спираль: укажите центр и внешний конец; 3 витка в экранном масштабе.',price_projection='Проекция цены: движение A→B, затем точка начала C. Не прогноз.',time_projection='Проекция времени: интервал A→B, затем точка C; шаг в барах, не календарных днях.',long_position='Ручная Long-модель: вход, затем стоп ниже и цель выше правее входа. Без заявки.',short_position='Ручная Short-модель: вход, затем стоп выше и цель ниже правее входа. Без заявки.')
    if kind.startswith('gann_'):hints[kind]='Ганн: укажите две точки линии 1×1 слева направо. Масштаб задаётся этими точками.'
    if kind in PATTERNS:hints[kind]='Ручная разметка, не проверка паттерна. Укажите по времени: '+', '.join(PATTERNS[kind])+'.'
    app.status.set(tr(hints.get(kind, 'Укажите точку на графике.' if count==1 else f'Укажите {count} точки на графике.')))


def cancel(app,message):
    app.drawing_mode=None;app.drawing_start=None;app._drawing_capture=None;app.status.set(tr(message))


def current(app,capture):
    return (app.instrument_cfg()['isin']==capture['isin'] and chart_identity(app)==capture['meta'] and
            app.current_df.index.equals(capture['dates']) and
            app.user_state(app.instrument_cfg()).get('drawings',[])==capture['expected'])


def click(app,event):
    kind=getattr(app,'drawing_mode',None)
    if kind not in REGISTRY:return
    try:
        if event.xdata is None or event.ydata is None or not math.isfinite(event.xdata) or not math.isfinite(event.ydata):return
        i=int(round(event.xdata));price=float(event.ydata)
        if not 0<=i<len(app.current_df) or price<=0:return
    except (TypeError,ValueError,OverflowError):return
    capture=capture_for(app,kind)
    if capture is None:return
    capture['points'].append(dict(at=str(app.current_df.index[i]),price=price))
    if len(capture['points'])<REGISTRY[kind]['points']:
        app.drawing_start=capture['points'][0];app.status.set(tr('Укажите следующую точку')+f" ({len(capture['points'])+1}/{REGISTRY[kind]['points']}).");return
    if kind=='regression':
        from drawing_regression import submit
        submit(app,capture);return
    text=None
    if kind in ('text','note','callout'):
        from tkinter.simpledialog import askstring
        text=askstring(tr('Текст рисунка'),tr('Введите текст (до 240 символов)'),parent=app.root)
        if text is None:cancel(app,'Рисование отменено.');return
    if not current(app,capture):cancel(app,'Контекст графика изменился. Рисунок не добавлен.');return
    try:obj=build(kind,capture['points'],text)
    except (ValueError,TypeError) as error:cancel(app,str(error));return
    obj.update(source_mode=app.chart_meta.get('source'),currency=app.chart_meta.get('currency'))
    for key in ('venue','symbol'):
        if app.chart_meta.get(key):obj[key]=app.chart_meta[key]
    if commit(app,capture['isin'],capture['expected'],capture['expected']+[obj]):
        app.drawing_mode=None;app.drawing_start=None;app._drawing_capture=None
    else:cancel(app,'Разметка изменилась. Рисунок не добавлен.')


def capture_for(app,kind):
    info=app.instrument_cfg();capture=getattr(app,'_drawing_capture',None)
    if capture is None:
        if hasattr(app,'_accepted_trading_chart'):
            accepted=app._accepted_trading_chart
            if not isinstance(accepted,(tuple,list)) or len(accepted)!=2 or not isinstance(accepted[0],dict) or accepted[0].get('symbol')!=info['name']:
                cancel(app,'Дождитесь загрузки графика выбранной акции.');return
            selected=[row for row in accepted[0].get('watchlist',[]) if row.get('name')==accepted[0]['symbol']]
            tf=getattr(app,'timeframe',None)
            if len(selected)!=1 or selected[0].get('isin')!=info['isin'] or (tf is not None and accepted[0].get('timeframe')!=tf.get()):
                cancel(app,'Принятый график относится к другому инструменту или ТФ.');return
        if not app.chart_meta.get('source') or not app.chart_meta.get('currency'):
            cancel(app,'Идентичность графика ещё не загружена.');return
        capture=dict(kind=kind,isin=info['isin'],meta=chart_identity(app),dates=app.current_df.index.copy(),expected=copy.deepcopy(app.user_state(info).get('drawings',[])),points=[])
        app._drawing_capture=capture
    if capture['kind']!=kind or not current(app,capture):cancel(app,'График, акция или разметка изменились. Выберите инструмент рисования заново.');return
    return capture
