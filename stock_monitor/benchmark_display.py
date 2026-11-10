"""Presentation only: preserve benchmark inputs, state codes and provenance."""
from i18n import tr

def reason_text(reason):
    # Translate only known complete messages; arbitrary provider errors stay intact.
    if reason.startswith('Data Quality '):
        if reason=='Data Quality INVALID: не подтверждены source/listing/currency/timezone':
            return tr(reason)
        return tr('Качество данных: {state}',state=reason.removeprefix('Data Quality '))
    return tr(reason)

def relative_text(context,meta):
    archive=context.get('benchmark_archive',{})
    state=context.get('state','UNAVAILABLE')
    if state in ('OUTPERFORMING','NEUTRAL','UNDERPERFORMING'):
        text=tr('RS 1D {state}: акция {stock:+.2f}% / индекс {benchmark:+.2f}% / Δ {difference:+.2f} п.п. · {first} → {last}',
                state=state,stock=context['stock_return_pct'],benchmark=context['benchmark_return_pct'],
                difference=context['difference_pp'],first=context['first'][:10],last=context['last'][:10])
        quality='READY'
    else:
        text='RS 1D: '+reason_text(context.get('reason','UNAVAILABLE'))
        quality=archive.get('quality','SOURCE_UNAVAILABLE')
        if meta:text+=' · '+str(context.get('benchmark_name'))
    if meta:
        text+=tr(' · {provider} {symbol} {venue} {currency} {timezone} · DQ {quality} · архив {archive}',
                 provider=meta.get('source'),symbol=meta.get('symbol'),venue=meta.get('venue'),
                 currency=meta.get('currency'),timezone=meta.get('timezone'),quality=quality,
                 archive=archive.get('status','INCOMPLETE'))
    if state in ('OUTPERFORMING','NEUTRAL','UNDERPERFORMING'):
        text+=' · '+tr(context['benchmark_return_basis'])
    return text
