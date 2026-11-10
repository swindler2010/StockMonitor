from i18n import tr
from diagnostic_display import diagnostic_text
"""Display only: consume prepared engine output, never calculate a decision."""
import math


def _number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def format_engine(engine):
    if not isinstance(engine,dict) or engine.get('signal') not in ('BUY','SELL','HOLD','WAIT') or engine.get('status') not in ('READY','DATA BLOCKED'):
        return tr('WAIT • нет результата'),tr('Trading Engine: актуальный фоновый результат недоступен. Торговый сигнал не подтверждён.')
    blocked=engine['status']=='DATA BLOCKED' or bool(engine.get('blockers'))
    title='WAIT / DATA BLOCKED' if blocked else engine['signal']
    confidence=engine.get('confidence');score=engine.get('score')
    lines=[tr('Сила: ')+(f'{confidence:g}/100' if _number(confidence) and 0<=confidence<=100 else tr('нет данных'))+
           (f' · score {score:+g}' if _number(score) else ''),tr('Сила подтверждений, не вероятность прибыли.')]
    if blocked:lines.append(tr('Данные блокируют торговый сигнал.'))
    if engine.get('analysis_scope'):
        scope=engine['analysis_scope']
        lines.insert(0,tr('Область анализа: последние 5 лет, с ')+str(scope.get('start'))+tr('. Более старые дефекты сохранены в архиве вне этой области.'))
    for key,label,limit in (('bullish_reasons',tr('За рост'),3),('bearish_reasons',tr('За снижение'),3),
                            ('blockers',tr('Блокировка'),None),('entry_blockers',tr('Вход не подтверждён'),None),('warnings',tr('Примечания'),None)):
        values=engine.get(key) or []
        if not isinstance(values,(list,tuple)):values=[values]
        texts=[diagnostic_text(v.get('text','')) if isinstance(v,dict) else diagnostic_text(v) for v in values]
        if texts:lines.append(label+': '+ '; '.join(texts[:limit]))
    contexts=engine.get('timeframe_context') or {}
    mtf=[]
    if isinstance(contexts,dict):
        for tf,context in contexts.items():
            context=context if isinstance(context,dict) else {}
            facts=context.get('facts') or {};quality=context.get('data_quality') or {}
            mtf.append(f"{tf}: {facts.get('regime',tr('нет данных'))} / {quality.get('state',tr('не проверено'))}")
            if context.get('scope_initialization',{}).get('stable') is False:
                lines.append(f"{tf}{tr(': в пределах пяти лет не подтверждён устойчивый прогрев индикаторов; более старые данные не использованы.')}")
    lines.append('MTF: '+('; '.join(mtf) or tr('нет контекста')))
    benchmark=engine.get('benchmark_context') or {}
    if not isinstance(benchmark,dict):benchmark={}
    quality=benchmark.get('quality',tr('не проверено'))
    if isinstance(quality,dict):quality=quality.get('state',tr('не проверено'))
    difference=benchmark.get('difference_pp')
    lines.append('Benchmark: '+str(benchmark.get('benchmark_name') or benchmark.get('benchmark_id') or tr('не выбран'))+
                 ' · '+str(benchmark.get('state','UNAVAILABLE'))+' · DQ '+str(quality)+
                 (f" · Δ {difference:+.2f}{tr(' п.п.')}" if _number(difference) else ''))
    if benchmark.get('reason'):
        from benchmark_display import reason_text
        lines.append(reason_text(str(benchmark['reason'])))
    if engine.get('timestamp'):lines.append(tr('Закрытая свеча: ')+str(engine['timestamp']))
    return title,'\n'.join(lines)


def bind_engine(view,engine):
    title,details=format_engine(engine)
    view.decision.set(title);view.explain.set(details)


def accepted_chart(view,isin):
    """Reuse accepted technical data in the existing card without UI analysis."""
    from chart_worker import capture
    cached=getattr(view,'_accepted_trading_chart',None)
    if not cached or cached[0]!=capture(view):return None
    values,prepared=cached
    # A candle can change after chart acceptance, before the next UI refresh.
    token=prepared.get('_input_revisions')
    if token is None or not view.store.read_cache.current(token):return None
    from partial_chart import current as files_current
    if not files_current(prepared['frame'].attrs.get('partial_chart',{}).get('files',[])):return None
    selected=next((i for i in values['watchlist'] if i['name']==values['symbol']),{})
    return prepared if selected.get('isin')==isin else None
