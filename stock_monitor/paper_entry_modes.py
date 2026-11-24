"""Entry rules shared by PAPER trading and historical experiments."""
from paper_strategy_v2 import POLICY, above, bounds, finite

V2_TIMEFRAME = '30m'
V2_RESEARCH_BLOCKER = 'V2_30M_RESEARCH_ONLY'

VERSION = 2
DB_NAME = 'paper_entry_modes_v2.sqlite3'
MODES = (
    ('V1', 'V1 — Долгосрочная', 'Недельный тренд, дневной вход и подтягиваемый стоп.'),
    ('V2', 'V2 — Быстрая 30m', 'Закрытая свеча 30 мин, вход через 30 мин. Стоп, цель и выход через 30 или 75 мин. Исполнение только после проверок.'),
    ('PULLBACK', 'V3 — Сбалансированная', 'Возврат выше EMA10 после отката по тренду. Выход у сопротивления или при срыве восстановления.'),
    ('MOMENTUM', 'V4 — Активная', 'Правила V5 на выбранном интервале: пробой, разворот, окно после отчёта. Стоп, цель или 3 часа.'),
    ('AGGRESSIVE', 'V5 — Агрессивная', 'Автоматический выбор ТФ. Пробой, подтверждённый разворот и дневной гэп с импульсом от 1 ATR. Входы 1H учитывают плавный рост относительно группы.'),
)
STRATEGY_CODES = dict(zip((key for key, _, _ in MODES), ('V1', 'V2', 'V3', 'V4', 'V5')))
UNIVERSE = ('US0378331005', 'US67066G1040', 'US0231351067')
NAMES = dict(zip(UNIVERSE, ('Apple', 'NVIDIA', 'Amazon')))
EXIT_POLICIES = {
    'V1': {'hard_stop': True},
    'V2': None,
    'PULLBACK': dict(hard_stop=True, take_profit=True),
    'MOMENTUM': dict(hard_stop=True, trailing_atr=1.5),
    'AGGRESSIVE': dict(hard_stop=True, take_profit=True, max_holding_minutes=240),
}


def targets(contexts, price):
    for context in contexts:
        level = context['facts']['resistance']
        if finite(level):
            bounded = level - level_uncertainty(context)
            if bounded > price:
                yield bounded


def level_uncertainty(context):
    return .01 if context['scope_initialization'].get('mode') == 'OWNER_ACCEPTED_DEMO_SENSITIVITY' else 0.


def decision(engine, mode, timeframe="1H", *, legacy_position=False):
    if engine.get('status') != 'READY':
        return dict(action='BLOCKED', reason='CORE_DATA_BLOCKED')
    if mode == 'V2':
        from paper_v2_30m import decision as frozen_decision
        return frozen_decision(engine,timeframe)
    if engine.get('demo_initialization') and not engine['demo_initialization'].get('consistent'):
        return dict(action='BLOCKED', reason='CORE_SEED_UNSTABLE')
    if mode == 'AGGRESSIVE' and not legacy_position:
        from paper_v5 import decide
        return decide(engine,timeframe)
    try:
        from trading_engine import profile
        others = [tf for tf in profile(timeframe) if tf != timeframe]
        h, m, d = (engine['timeframe_context'][tf] for tf in (timeframe, *others))
        f, medium, daily = (c['facts'] for c in (h, m, d))
        price, support = f['price'], f['support']
        atr_lo, atr = bounds(h, 'atr')
        if not (all(finite(v) and v > 0 for v in (price, support, atr_lo, atr))):
            raise ValueError('STRATEGY_RISK_INPUT_INVALID')
        ema_lo, ema_hi = bounds(h, 'ema10')
        support_lo = support - level_uncertainty(h)
        support_hi = support + level_uncertainty(h)
        if mode == 'PULLBACK':
            exit_signal = f['breakdown'] or (price < ema_lo and not above(h, 'histogram_delta', 0))
            entry = (f['reclaim'] and price > ema_hi and medium['regime'] == 'BULLISH'
                     and daily['regime'] != 'BEARISH')
            stop = min(support_lo, price - 1.5 * atr)
            target = min(targets((h, m, d), price), default=None)
            rr = 1.75
        elif mode == 'MOMENTUM':
            exit_signal = not above(h, 'histogram_delta', 0) and not above(h, 'rsi', 50)
            entry = (f['candle_up'] and price > ema_hi and medium['regime'] != 'BEARISH'
                     and f['volume_reliable'] and f['relative_volume'] >= 1.15
                     and above(h, 'histogram', 0) and above(h, 'histogram_delta', 0) and above(h, 'rsi', 52))
            stop = price - 1.5 * atr
            target = min(targets((h, m, d), price), default=None)
            rr = 1.5
        elif mode == 'AGGRESSIVE':
            exit_signal = f['breakdown'] or (not above(h, 'histogram_delta', 0) and not above(h, 'rsi', 42))
            return dict(action='SELL' if exit_signal else 'HOLD',
                        reason='AGGRESSIVE_EXIT' if exit_signal else 'V5_LEGACY_POSITION',atr=atr)
        else:
            raise ValueError('UNKNOWN_ENTRY_MODE')
        if exit_signal:
            return dict(action='SELL', reason=mode + '_EXIT', atr=atr)
        if not (0 < stop < price):
            raise ValueError('STRATEGY_STOP_INVALID')
        if not entry or f['extended']:
            return dict(action='WAIT', reason=mode + '_SETUP_PENDING', atr=atr)
        if target is None or (target - price) / (price - stop) < rr:
            return dict(action='WAIT', reason='STRATEGY_REWARD_TOO_LOW', atr=atr)
        return dict(action='BUY', reason=mode, stop=stop, target=target, atr=atr,
                    mode='EARLY', risk_fraction=POLICY['early_risk'], entry_rr=rr)
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        return dict(action='BLOCKED', reason=str(error))
