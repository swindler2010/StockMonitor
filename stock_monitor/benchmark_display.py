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


def select_frame(info,tf,normal,store,now=None):
    """Direct catalogue series only; stale history is visual, never permission."""
    import benchmarks as bm
    import data_quality as quality
    from analytics import resample
    expected=bm.CATALOG.get(info.get('isin'))
    valid=expected is not None and bm.identity(info)==bm.identity(expected)
    meta=bm.identity(expected) if expected else {}
    if not valid or any(normal.attrs.get(k)!=v for k,v in meta.items()):
        out=normal.iloc[:0].copy()
        out.attrs['data_quality']={'state':'INVALID','issues':['Index catalogue identity mismatch']}
        return out
    if tf not in ('1D','1W','1M'):
        out=normal.iloc[:0].copy()
        out.attrs['data_quality']={'state':'SOURCE_UNAVAILABLE','issues':['Direct index source supplies daily history only']}
        return out
    state=normal.attrs.get('data_quality',{}).get('state')
    if not normal.empty:
        # A real partial boundary week/month remains drawable with its original
        # aggregate DQ. Daily inputs already passed BenchmarkMixin's strict
        # audit; this display receipt never makes that period analytical input.
        provenance=normal.attrs.get('daily_provenance',{})
        if state=='INVALID' and not (tf in ('1W','1M') and provenance.get('raw_state') in ('READY','STALE')):
            return normal.iloc[:0].copy()
        out=normal.copy()
        if state!='READY':
            out.attrs['partial_chart']={'display_only':True,'raw_count':len(out),'excluded_rows':0,
                'reason':'Verified index daily inputs; incomplete aggregate coverage remains blocked'}
        return out
    if state!='STALE':return normal.iloc[:0].copy()
    # The analytical loader deliberately retains its freshness block. Display
    # may show its real historical rows only after an independent structural DQ.
    raw=bm.load(store,info)
    report=quality.audit(raw,'1D',meta,now=now)
    structural=quality.audit(raw,'1D',meta,now=now,check_stale=False)
    if report['state']!='STALE' or structural['state']!='READY':return normal.copy()
    raw.attrs['data_quality']=report
    out=resample(raw,'1D',tf,now=now)
    out.attrs['partial_chart']={'display_only':True,'raw_count':len(out),'excluded_rows':0,
        'reason':'Saved index history; latest publication is stale; analytical freshness remains blocked'}
    return out


def information(store,info):
    """Current local index facts, read only in the existing auxiliary worker."""
    import benchmarks as bm
    import data_quality as quality
    frame=bm.load(store,info)
    report=quality.audit(frame,'1D',bm.identity(info))
    return dict(archive=store.setting('benchmark_history:'+info['isin'],{}),quality=report['state'],
        count=len(frame),first=str(frame.index[0]) if len(frame) else None,
        last=str(frame.index[-1]) if len(frame) else None,
        close=float(frame.close.iloc[-1]) if len(frame) and report['state'] in ('READY','STALE') else None)


def information_text(info,facts,section='overview'):
    import benchmarks as bm
    from financial_format import format_value,format_date
    from i18n import language
    meta=bm.identity(info);archive=facts['archive']
    provider='Cboe' if meta['source']=='cboe' else 'Yahoo Finance'
    unit=tr(info['unit'])
    rows=[info['name'],tr('Прямой индекс · рыночный контекст'),
        tr('Источник')+': '+provider+' / '+meta['symbol']+' / '+meta['venue'],
        tr('Часовой пояс')+': '+meta['timezone'],tr('Валюта источника')+': '+meta['currency'],
        tr('Единица')+': '+unit,tr('Тип индекса')+': '+tr(info['return_basis']),
        tr('Последнее значение')+': '+format_value(facts.get('close'),language(),kind='number',digits=2)+' '+unit,
        tr('Последняя свеча')+': '+format_date(facts.get('last'),language()),
        tr('Свечей в архиве')+': '+str(facts['count']),
        tr('История')+': '+format_date(facts.get('first'),language())+' → '+format_date(facts.get('last'),language()),
        tr('Качество данных')+': '+facts['quality'],
        tr('Архив')+': '+str(archive.get('status','INCOMPLETE'))+' / '+str(archive.get('archive_quality') or archive.get('quality','UNKNOWN')),
        '',tr('Доступные интервалы: 1D / 1W / 1M. Внутридневная история этим источником не предоставляется.'),
        tr('Индекс используется как рыночный контекст. Торговый объём не применим.')]
    if section in ('finances','reports','events'):
        rows.insert(2,tr('Это индекс, а не компания: корпоративная отчётность, прибыль и дивиденды эмитента не применяются.'))
    return '\n'.join(rows)
