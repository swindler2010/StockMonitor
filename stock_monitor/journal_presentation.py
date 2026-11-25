"""Journal display only; leave ledger calculations and record values untouched."""
from i18n import tr


def model_summary(model):
    lines=[tr('Модельный портфель: только рекомендации с явно заданным количеством.')]
    def number(value):
        return f'{value:+.2f}' if value is not None else tr('нет данных')
    for currency,values in model['by_currency'].items():
        lines.append(tr('{currency}: за день {daily}; Price P/L {pnl}; Dividends {dividends}; Total Return {total}; реализованный {realized}; открытые позиции {unrealized}.',
            currency=currency,daily=number(values['daily_pnl']),pnl=number(values['pnl']),
            dividends=number(values.get('dividends')),total=number(values.get('total_return')),
            realized=number(values['realized']),unrealized=number(values['unrealized'])))
    if not model['by_currency']:lines.append(tr('Рассчитанных модельных позиций пока нет.'))
    if not model['complete']:lines.append(tr('НЕПОЛНЫЙ результат: есть отсутствующая история, ожидающие или отклонённые модельные операции.'))
    lines.append(tr('Price P/L по архиву без комиссий и сплит-корректировок; Dividends — модельные валовые выплаты, при подтверждённой валюте и покрытии. Источники и интервалы учитываются отдельно; стоп/цель не исполняются автоматически. Это не прибыль брокерского счёта.'))
    return '\n'.join(lines)


def outcome_label(status):
    return tr({'pending':'ожидание','unavailable':'нет данных','evaluated':'рассчитано'}.get(status,status))


def quality_label(status):
    return tr({'correct':'правильный','false':'ложный','late':'запоздалый','early_exit':'ранний выход',
               'bad_stop':'плохой стоп','missed_opportunity':'пропущенная возможность','unassessed':'не оценён'}.get(status,status))
