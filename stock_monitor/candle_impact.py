"""Dependency evidence for current CORE. No repair, persistence or permission.

Only an invalid old volume can be proved irrelevant here. Missing/unknown
prices remain recursive dependencies, regardless of their distance or count.
"""
import numpy as np
import data_quality

COLUMNS = ('ema10','ema30','rsi','macd','macd_signal','atr','volume_mean20')


def assess(frame, tf, now):
    archive = frame.attrs.get('archive_quality_before_scope', frame.attrs.get('data_quality', {}))
    result = dict(schema=1, timeframe=tf, status='UNPROVEN', eligible=False,
                  original_state=archive.get('state','UNKNOWN'),
                  warnings=['Исходный статус DQ сохранён. Оценка влияния не является торговым разрешением.'],
                  trade_permission=False, dependencies={
                      'ema10/ema30/rsi/macd/macd_signal':'recursive price history',
                      'atr':'recursive high/low/previous close history',
                      'volume_mean20':'previous 20 bars; terminal pair requires last 22',
                      'structure':'current/previous candles and previous 20-bar levels'},
                  reason='RECURSIVE_DEPENDENCY_NOT_PROVEN')
    if frame.empty or not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        return dict(result, reason='TIMESTAMPS_OR_EMPTY')
    report = data_quality.audit(frame,tf,expected=frame.attrs,now=now)
    result['current_quality'] = {k:report[k] for k in ('state','missing_count','stale_count','repairable') if k in report}
    result['fault_ranges'] = report.get('ranges',[])[:8]
    result['ranges_truncated'] = len(report.get('ranges',[]))>8
    result['fault_range_count'] = len(report.get('ranges',[]))
    result['indicator_results'] = {k:'DEPENDENCY_NOT_PROVEN' for k in COLUMNS}
    result['missing_count'] = report.get('missing_count',0)
    if report['state']=='READY':
        # Existing five-year scope path owns its initialization proof.
        return dict(result,status='NO_CURRENT_FAULT',reason='USE_EXISTING_REFERENCE_PROOF')
    if tf not in ('5m','15m','1H','1D') or frame.attrs.get('aggregation_base'):
        return dict(result,reason='DERIVED_PARENT_PROOF_REQUIRED')
    if (not report.get('repairable') or report.get('stale_count') or report.get('missing_count')
            or report.get('issues') != ['Невозможные OHLC/цены/объёмы']):
        return result
    inherited=frame.attrs.get('data_quality',{})
    if inherited.get('state') not in ('READY','INVALID') or (
            inherited.get('state')=='INVALID' and inherited.get('issues') != report['issues']):
        return dict(result,reason='INHERITED_SCOPE_UNPROVEN')
    if inherited.get('state')=='INVALID' and (
            not inherited.get('repairable') or inherited.get('ranges')!=report.get('ranges')
            or inherited.get('identity')!=report.get('identity') or inherited.get('stale_count')):
        return dict(result,reason='INHERITED_SCOPE_UNPROVEN')
    values=frame[['open','high','low','close']].to_numpy(dtype=float)
    if (not np.isfinite(values).all() or (values<=0).any()
            or (frame.high<frame[['open','low','close']].max(axis=1)).any()
            or (frame.low>frame[['open','high','close']].min(axis=1)).any()):
        return result
    volume=frame.volume.to_numpy(dtype=float)
    positions=np.flatnonzero(~np.isfinite(volume)|(volume<0))
    result.update(fault_count=len(positions),fault_fields=['volume'],
                  bars_after_last_fault=len(frame)-int(positions[-1])-1 if len(positions) else None)
    if not len(positions) or int(positions[-1])>=len(frame)-200:
        return dict(result,reason='FAULT_IN_ANALYTICAL_WINDOW')
    result.update(status='FINITE_DEPENDENCY_EXCLUDED',eligible=True,
                  reason='VOLUME_ONLY_OUTSIDE_WINDOW_REQUIRES_EXACT_CORE_COMPARISON')
    result['indicator_results'] = {k:'PRICE_INPUTS_UNCHANGED' for k in COLUMNS}
    result['indicator_results']['volume_mean20']='FAULT_OUTSIDE_FINITE_WINDOW'
    result['warnings'].append(f'Старые ошибки объёма: {len(positions)}; после последней {result["bars_after_last_fault"]} свечей. Ценовая история сохранена.')
    return result


def verify(frame, enriched, features):
    """Use existing indicator/fact functions, exact values, no fabricated rows."""
    from analytics import indicators
    from trading_engine import facts
    reference=indicators(frame)
    left=reference[list(COLUMNS)].tail(2)
    right=enriched[list(COLUMNS)].tail(2)
    exact=np.isfinite(left.to_numpy()).all() and left.equals(right)
    reference_facts=facts(reference,features['support'],features['resistance'],features['stop'])
    return bool(exact and reference_facts==features.get('facts'))


def summary(row):
    """Bounded, visible warning for existing health panel and read-only MCP."""
    impact=row.get('quality_impact') or {}
    if not impact:return ''
    status=impact.get('status','UNPROVEN')
    if status=='PROVEN_NO_CURRENT_EFFECT':
        return ('Влияние дефектов: не выявлено в текущих индикаторах и признаках CORE. '
                'Исходный DQ: '+str(impact.get('original_state','UNKNOWN'))+'. '
                'Доступен только анализ; торговый допуск не расширен.')
    return ('Влияние дефектов не исключено: '+str(impact.get('reason','UNKNOWN'))+
            '. Исходный DQ сохранён; блокировки действуют.')
