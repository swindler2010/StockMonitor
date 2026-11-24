"""Validated presentation properties for user drawings, not strategy levels."""
import copy
import math
import re

DEFAULT=dict(color='#00c5ff',width=1.3,linestyle='--',opacity=1.,
             fill='#00c5ff',fill_opacity=.18,fontsize=10,price_label=True)
LINES={'Сплошная':'-','Штриховая':'--','Точечная':':','Штрихпунктир':'-.'}


def validate(value):
    if not isinstance(value,dict) or set(value)-set(DEFAULT):raise ValueError('Неизвестные параметры стиля')
    result={**DEFAULT,**value}
    for key in ('color','fill'):
        if not isinstance(result[key],str) or not re.fullmatch('#[0-9a-fA-F]{6}',result[key]):raise ValueError('Цвет должен иметь вид #00c5ff')
    for key,low,high in [('width',.5,8),('opacity',.05,1),('fill_opacity',0,1),('fontsize',8,28)]:
        n=result[key]
        if isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) or not low<=n<=high:raise ValueError('Недопустимое значение: '+key)
    if result['linestyle'] not in LINES.values():raise ValueError('Неизвестный тип линии')
    if type(result['price_label']) is not bool:raise ValueError('Метка цены: требуется да/нет')
    return result


def effective(obj,color='#00c5ff'):
    """Legacy drawings retain their original palette and vertical dotted line."""
    default={**DEFAULT,'color':color,'fill':color,'linestyle':':' if obj.get('type')=='vline' else '--'}
    if obj.get('type')=='zone':default.update(width=1,linestyle='-',opacity=.18)
    value=obj.get('style',{})
    try:return validate({**default,**value})
    except (TypeError,ValueError):return validate(default)


def template(name,style):
    if not isinstance(name,str) or not 1<=len(name.strip())<=40 or any(ord(c)<32 for c in name):raise ValueError('Имя шаблона: от 1 до 40 символов')
    return dict(name=name.strip(),style=validate(style))


def save_template(app,isin,expected_drawings,expected_templates,item):
    from i18n import tr
    if app.instrument_cfg()['isin']!=isin:return False
    draft=copy.deepcopy(app.user_state(app.instrument_cfg()))
    if draft.get('drawings',[])!=expected_drawings or draft.get('drawing_style_templates',[])!=expected_templates:return False
    if not isinstance(expected_templates,list) or len(expected_templates)>20 or any(not isinstance(t,dict) or set(t)!={'name','style'} for t in expected_templates):raise ValueError('Сохранённые шаблоны повреждены; они не будут перезаписаны')
    if not isinstance(item,dict) or set(item)!={'name','style'}:raise ValueError('Некорректный шаблон')
    item=template(item['name'],item['style'])
    templates=copy.deepcopy(expected_templates)
    found=next((n for n,row in enumerate(templates) if row['name']==item['name']),None)
    if found is None:
        if len(templates)>=20:raise ValueError('Можно сохранить не более 20 шаблонов для акции')
        templates.append(item)
    else:templates[found]=item
    draft['drawing_style_templates']=templates;app.drafts[isin]=draft;app.user_dirty=True
    app.status.set(tr('Шаблон стиля изменён. Нажмите «Сохранить».'))
    return True
