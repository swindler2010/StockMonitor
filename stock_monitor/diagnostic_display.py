"""Translate known analytical explanations at the display boundary only."""
import re
from i18n import tr, rendered_message

_TF=r'(?:5m|10m|15m|30m|1H|2H|3H|4H|1D|1W|1M)'
_PATTERNS=(
    (re.compile('('+_TF+r'): цена > EMA10 > EMA30, обе EMA растут'),'{tf}: цена > EMA10 > EMA30, обе EMA растут',('tf',)),
    (re.compile('('+_TF+r'): цена < EMA10 < EMA30, обе EMA снижаются'),'{tf}: цена < EMA10 < EMA30, обе EMA снижаются',('tf',)),
    (re.compile(r'Подтверждённая закрытием структура: (breakout|retest|reclaim)'), 'Подтверждённая закрытием структура: {structure}',('structure',)),
    (re.compile(r'Относительный объём ([0-9]+\.[0-9]{2}) × предыдущих 20 свечей'),'Относительный объём {volume} × предыдущих 20 свечей',('volume',)),
    (re.compile('('+_TF+r'): DATA BLOCKED / нет проверенного timing'),'{tf}: DATA BLOCKED / нет проверенного timing',('tf',)),
)

def diagnostic_text(value):
    text=str(value)
    translated=rendered_message(text)
    if translated!=text:return translated
    for pattern,template,names in _PATTERNS:
        match=pattern.fullmatch(text)
        if match:return tr(template,**dict(zip(names,match.groups())))
    match=re.fullmatch('('+_TF+r'): DATA BLOCKED / ([A-Z_]+); (.*)',text,flags=re.DOTALL)
    if match:
        tf,state,reason=match.groups()
        return f'{tf}: DATA BLOCKED / {state}; '+tr(reason)
    return text
