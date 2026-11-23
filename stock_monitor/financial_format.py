"""Display-only formatting: original numeric inputs are never modified."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

LOCALES={
 'ru': {'decimal':',','group':' ','missing':'нет данных','million':'млн','billion':'млрд','thousand':'тыс','trillion':'трлн'},
 'de': {'decimal':',','group':'.','missing':'keine Daten','million':'Mio.','billion':'Mrd.','thousand':'Tsd.','trillion':'Bio.'},
 'en': {'decimal':'.','group':',','missing':'no data','million':'M','billion':'B','thousand':'K','trillion':'T'},
}


def language(value):return value if value in LOCALES else 'ru'


def format_value(value, locale='ru', kind='amount', currency=None, compact=False, digits=2):
    cfg=LOCALES[language(locale)]
    try:
        if value is None or isinstance(value,bool):raise ValueError()
        amount=Decimal(str(value))
        if not amount.is_finite():raise ValueError()
    except (InvalidOperation,ValueError):return cfg['missing']
    if kind not in ('amount','price','quantity','percent','number'):raise ValueError('UNKNOWN_FORMAT_KIND')
    if not isinstance(digits,int) or not 0<=digits<=12:raise ValueError('INVALID_PRECISION')
    if kind=='percent':amount*=100
    suffix=''
    if compact and kind in ('amount','number'):
        for divisor,name in ((Decimal('1e12'),'trillion'),(Decimal('1e9'),'billion'),(Decimal('1e6'),'million'),(Decimal('1e3'),'thousand')):
            if abs(amount)>=divisor:
                amount/=divisor;suffix=' '+cfg[name];break
    rounded=amount.quantize(Decimal(1).scaleb(-digits),rounding=ROUND_HALF_UP)
    if not rounded:rounded=abs(rounded)
    whole,_,fraction=f'{rounded:,.{digits}f}'.partition('.')
    whole=whole.replace(',',cfg['group']);fraction=fraction.rstrip('0')
    text=whole+(cfg['decimal']+fraction if fraction else '')+suffix
    if kind=='percent':text+=' %'
    elif currency and kind in ('amount','price'):text+=' '+currency
    return text


def saved_language(store):return language(store.setting('ui_language','ru'))


def save_language(store,value):
    if value not in LOCALES:raise ValueError('UNSUPPORTED_LANGUAGE')
    store.set_setting('ui_language',value)


def format_date(value,locale='ru',clock=False,assume_zone=None,short=False):
    """Presentation date in Berlin; external naive timestamps never gain a zone."""
    from datetime import datetime,date
    from zoneinfo import ZoneInfo
    if value is None:return '—'
    try:
        raw=str(value)
        if len(raw)==10:
            stamp=date.fromisoformat(raw)
            return stamp.strftime('%Y-%m-%d' if language(locale)=='en' else '%d.%m.%y')
        stamp=datetime.fromisoformat(raw)
        if stamp.tzinfo is None and assume_zone:stamp=stamp.replace(tzinfo=ZoneInfo(assume_zone))
        aware=stamp.tzinfo is not None
        if aware:stamp=stamp.astimezone(ZoneInfo('Europe/Berlin'))
        pattern=('%m-%d' if language(locale)=='en' else '%d.%m') if short else ('%Y-%m-%d' if language(locale)=='en' else '%d.%m.%y')
        result=stamp.strftime(pattern+(' %H:%M' if clock else ''))
        if clock and aware and not short:result+=' '+stamp.tzname()
        if clock and not aware:result+=' ['+{'ru':'пояс не указан','de':'Zone unbekannt','en':'zone unknown'}[language(locale)]+']'
        return result
    except (ValueError,TypeError,OverflowError):return '—'
