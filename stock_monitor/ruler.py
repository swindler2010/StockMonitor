from i18n import tr, rendered_message
def measurement(index,a,b,currency):
    delta=b[1]-a[1]
    percent=f'{delta/a[1]*100:+.2f}%' if a[1] else tr('процент недоступен')
    elapsed=abs(index[b[0]]-index[a[0]])
    unit='€' if currency=='EUR' else currency
    return rendered_message(f'{delta:+.2f} {unit} | {percent} | {abs(b[0]-a[0])} свечей | {elapsed}')
