"""Display-only formatting: original numeric inputs are never modified."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

LOCALES={
 'ru': {'decimal':',','group':' ','missing':'нет данных','million':'млн','billion':'млрд','thousand':'тыс'},
 'de': {'decimal':',','group':'.','missing':'keine Daten','million':'Mio.','billion':'Mrd.','thousand':'Tsd.'},
 'en': {'decimal':'.','group':',','missing':'no data','million':'M','billion':'B','thousand':'K'},
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
        for divisor,name in ((Decimal('1e9'),'billion'),(Decimal('1e6'),'million'),(Decimal('1e3'),'thousand')):
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
