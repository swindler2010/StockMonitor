"""G06 / 49: local, evidence-based development advice; no trading authority.

Reads saved diagnostic metadata, never candles, accounts, secrets or an API.
Rule-based first stage of the Development Advisor, not an LLM response.
"""
import json
import sqlite3
import time
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PREFIXES = ('cp_eligibility:', 'cp_assessment_request:', 'data_quality:',
            'cp_reconciliation_job:', 'paper_auto_status:')
ROW_LIMITS = (512, 512, 2048, 2048, 512)
VALUE_LIMIT = 4_000_000


def epoch(value):
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value) if 0 < value < 32_503_680_000 else None
        at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return at.timestamp() if at.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def local_time(value):
    at = epoch(value)
    return datetime.fromtimestamp(at, ZoneInfo('Europe/Berlin')).strftime('%d.%m.%Y %H:%M:%S %Z') if at else 'время неизвестно'


def analyse(records, now, omitted=0, shadow=None):
    """Pure projection of saved facts. An old saved CURRENT is never a new READY."""
    eligibility, requests, dq, jobs, paper = {}, {}, Counter(), [], []
    invalid = 0
    for key, value in records:
        if not isinstance(value, dict):
            invalid += 1
            continue
        if key.startswith(PREFIXES[0]):
            eligibility[key[len(PREFIXES[0]):]] = value
        elif key.startswith(PREFIXES[1]):
            requests[key[len(PREFIXES[1]):]] = value
        elif key.startswith(PREFIXES[2]):
            state = value.get('state')
            if state in ('READY', 'GAP', 'UNCONFIRMED', 'INVALID', 'STALE', 'SOURCE_GAP', 'SOURCE_UNAVAILABLE'):
                dq[state] += 1
        elif key.startswith(PREFIXES[3]):
            jobs.append(value)
        elif key.startswith(PREFIXES[4]):
            paper.append(value)
    advice = []

    def add(code, priority, title, evidence, action, acceptance, section=1):
        advice.append(dict(id=code, priority=priority, title=title, evidence=evidence,
                           action=action, acceptance=acceptance, section=section))

    expired = sum((epoch(v.get('expires_at')) or now + 1) <= now for v in eligibility.values())
    unknown = sum(epoch(v.get('expires_at')) is None for v in eligibility.values())
    overdue = [epoch(v.get('requested_at')) for isin, v in requests.items()
               if epoch(v.get('requested_at')) is not None
               and 0 <= now - epoch(v['requested_at']) > 600
               and isin in eligibility and epoch(eligibility[isin].get('checked_at')) is not None
               and epoch(eligibility[isin]['checked_at']) < epoch(v['requested_at'])]
    if overdue:
        add('ASSESSMENT_QUEUE', 1, 'Ускорить ожидающий повторный расчёт',
            f'Запросов старше 10 минут без более новой сохранённой оценки: {len(overdue)}. '
            f'Самый ранний: {local_time(min(overdue))}.',
            'Выяснить на одном примере, почему расчёт ещё не начался или не закончился. Это признак задержки, а не доказанная ошибка.',
            'Программа сама выдаёт новый результат или понятную причину ожидания; проверки данных остаются включёнными.')
    if expired or unknown:
        add('ASSESSMENT_AGE', 2, 'Отделять старую оценку от текущего состояния',
            f'Сохранённых оценок: {len(eligibility)}; срок истёк у {expired}; срок неизвестен у {unknown}.',
            'Показывать возраст результата и состояние его обновления. Не выдавать прежний успешный расчёт за текущую готовность.',
            'Статистика показывает время снимка и отличает неизвестные данные от нуля; новая оценка проходит штатные проверки.')
    bad = sum(n for state, n in dq.items() if state != 'READY')
    if bad:
        labels = {'GAP':'пропуски', 'UNCONFIRMED':'не подтверждено', 'INVALID':'некорректные данные',
                  'STALE':'устарело', 'SOURCE_GAP':'неполный источник', 'SOURCE_UNAVAILABLE':'источник недоступен'}
        detail = ', '.join(f'{labels[s]}: {n}' for s, n in sorted(dq.items()) if s != 'READY')
        add('DATA_EVIDENCE', 2, 'Объяснять, какие свечи мешают анализу',
            f'В сохранённых проверках качества {bad} проблемных наборов свечей ({detail}). '
            'Сюда входят резервные и старые данные; это не число заблокированных акций.',
            'Выбрать одну рабочую акцию и найти именно те свечи, которые нужны её расчёту. Передавать восстановление CandlePilot и использовать уже накопленный опыт.',
            'Видны биржа, валюта, таймфрейм, дата и подтверждение исправления. CandlePilot устраняет причину, не придумывая свечи.')
    waiting = [v for v in jobs if v.get('status') in ('AWAITING_SOURCE', 'AWAITING_USER', 'RECHECK_QUEUED', 'QUEUED')]
    cooldown = sum((epoch(v.get('next_check')) or 0) > now for v in waiting)
    overdue_jobs = [v for v in waiting if epoch(v.get('next_check')) is not None and now - epoch(v['next_check']) > 1800]
    if waiting:
        add('SOURCE_WAIT', 2 if overdue_jobs else 3, 'Показывать, чего ожидает сверка источника',
            f'Заданий ожидания: {len(waiting)}; следующая попытка ещё не наступила у {cooldown}; '
            f'срок следующей попытки прошёл более 30 минут назад у {len(overdue_jobs)}.',
            'Разделить ограничение частоты запросов, отсутствие доказательств и задержку очереди. Отсутствие свечи в TV не объявлять доказательством отсутствия сделок.',
            'У каждого проверенного случая есть причина и следующая попытка; ожидание лимита API не показано как неисправность.')
    recent_paper = [v for v in paper if epoch(v.get('at')) is not None and 0 <= now - epoch(v['at']) <= 300]
    stale_paper = len(paper) - len(recent_paper)
    quote_blocked = sum(bool(v.get('quote_blocker')) for v in recent_paper)
    if quote_blocked:
        add('QUOTE_FRESHNESS', 1, 'Проверить приоритет свежей цены',
            f'Последние расчёты демоторговли моложе 5 минут: {len(recent_paper)}; цена заблокирована в {quote_blocked}.',
            'Проследить получение новой закрытой свечи и повторную оценку рабочей акции. Не увеличивать допустимый возраст цены ради исполнения.',
            'Новая подтверждённая цена обновляется автоматически; старая цена по-прежнему отклоняется.', section=2)
    ready_wait = sum(v.get('core') == 'READY' and v.get('signal') in ('WAIT', 'HOLD') for v in recent_paper)
    if ready_wait:
        add('ENTRY_OBSERVATION', 3, 'Разбирать ожидание покупки отдельно от ошибок',
            f'В последних расчётах анализ готов, но стратегия ждёт подходящего входа: {ready_wait}.',
            'Собирать причины отказа от входа. Проверять более ранние входы в существующей теневой V2, сохраняя обычную V1.',
            'Сравнить завершённые виртуальные сделки после комиссий, спреда и просадок. Отсутствие BUY не считается ошибкой.', section=2)
    shadow = shadow or {'state':'UNAVAILABLE'}
    books = shadow.get('books', {})
    if shadow.get('state') == 'AVAILABLE' and set(books) == {'V1', 'V2'}:
        evidence = f'В отдельных теневых портфелях завершено сделок: V1 — {books["V1"]}; V2 — {books["V2"]}.'
        if not min(books.values()):
            evidence += ' По завершённым сделкам преимущество новой стратегии ещё не подтверждено.'
    else:
        evidence = 'Данные двух теневых портфелей сейчас недоступны или неполны. Их результат не принят за ноль.'
    add('STRATEGY_COMPARISON', 3, 'Проверять улучшения входа, размера покупки и выхода', evidence,
        'Сравнивать существующие V1 и V2 на одинаковых данных: точку входа, размер покупки, стоп, цель и сопровождение. '
        'Сначала тест на истории и теневой портфель, затем решение владельца о включении.',
        'Показать прибыль после издержек, просадку и результаты закрытых сделок. Не объявлять одну стратегию лучше по нескольким сигналам.', section=2)
    if omitted or invalid:
        add('DIAGNOSTIC_COVERAGE', 3, 'Учитывать границы диагностического снимка',
            f'Не прочитано из-за размера, формата или лимита: {omitted + invalid}. Сводка неполная.',
            'Использовать подробную панель CP для выбранной акции. Ограниченный объём чтения сам по себе не означает неисправность; не запускать повторный аудит всех свечей.',
            'Недоступные данные явно обозначены; пустой или неполный снимок не превращён в сообщение «всё исправно».')
    advice.sort(key=lambda v: (v['section'], v['priority'], v['id']))
    for section in (1, 2):
        for number, item in enumerate((x for x in advice if x['section'] == section), 1):
            item['number'] = f'{section}.{number}'
    return dict(version=1, mode='LOCAL_RULES', collected_at=datetime.fromtimestamp(now, timezone.utc).isoformat(),
                saved_records=len(records), omitted=omitted + invalid, advice=advice,
                facts=dict(saved_assessments=len(eligibility), expired_assessments=expired,
                           saved_dq_reports=sum(dq.values()), review_jobs=len(jobs),
                           recent_paper_reports=len(recent_paper), old_paper_reports=stale_paper),
                note='Это советы по сохранённым данным, не новая проверка свечей и не разрешение на сделку. '
                     'Советы формируются в программе по правилам; внешняя ИИ-модель не вызывалась. Ожидание подходящей покупки само по себе не ошибка.')


def collect(path, now=None):
    started = time.perf_counter()
    records, omitted = [], 0
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        # Only diagnostic columns. Exclude histories/native evidence from large receipts
        # before Python serialization; no accounts or OHLCV tables are read.
        fields = ('checked_at', 'expires_at', 'requested_at', 'state', 'status', 'next_check', 'at', 'quote_blocker', 'core', 'signal')
        projection = 'json_extract(value,' + ','.join("'$." + k + "'" for k in fields) + ')'
        sql = ('SELECT key, CASE WHEN length(value)<=? AND json_valid(value) THEN ' + projection +
               ' END FROM settings WHERE key GLOB ? ORDER BY key LIMIT ?')
        rows = []
        for prefix, limit in zip(PREFIXES, ROW_LIMITS):
            count = db.execute('SELECT count(*) FROM settings WHERE key GLOB ?', (prefix+'*',)).fetchone()[0]
            part = db.execute(sql, (VALUE_LIMIT, prefix+'*', limit)).fetchall()
            omitted += count - len(part)
            rows.extend(part)
        for key, raw in rows:
            try:
                values = json.loads(raw) if raw is not None else None
                if not isinstance(values, list) or len(values) != len(fields):
                    omitted += 1
                    continue
                value = dict(zip(fields, values))
                # Whitelist facts only, so a provider message/path/secret cannot reach export.
                records.append((key, {k:value[k] for k in ('checked_at', 'expires_at', 'requested_at', 'state',
                                                         'status', 'next_check', 'at', 'core', 'signal') if k in value}
                                | ({'quote_blocker': bool(value.get('quote_blocker'))} if key.startswith('paper_auto_status:') else {})))
            except (ValueError, TypeError):
                omitted += 1
    shadow = collect_shadow(Path(path).parent / 'paper_strategy_shadow.sqlite3')
    report = analyse(records, time.time() if now is None else now, omitted, shadow)
    report['read_seconds'] = round(time.perf_counter() - started, 4)
    return report


def collect_shadow(path):
    """Only two completed-trade counts. Never creates or writes a shadow ledger."""
    if not Path(path).is_file():
        return {'state':'UNAVAILABLE'}
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            rows = db.execute("SELECT name,json_extract(state,'$.closed') FROM books WHERE name IN ('V1','V2') LIMIT 2").fetchall()
        books = {name:value for name, value in rows if isinstance(value, int) and value >= 0}
        return dict(state='AVAILABLE' if len(books) == 2 else 'UNAVAILABLE', books=books)
    except (sqlite3.Error, ValueError, TypeError):
        return {'state':'UNAVAILABLE'}


def render(report, section=None):
    lines = ['Советы по развитию Stock Monitor',
             'Снимок: ' + local_time(report['collected_at']), report['note'], '',
             ('Покрытие неполное: ' + str(report['omitted']) + ' записей не вошли в ограниченный снимок. Ниже показана только прочитанная часть.' if report['omitted'] else 'Сохранённая диагностика прочитана в пределах выбранных разделов.'),
             'Прочитано: ' + ', '.join(f'{label}: {report["facts"][key]}' for key, label in (
                 ('saved_assessments', 'оценок'), ('saved_dq_reports', 'проверок качества'),
                 ('review_jobs', 'заданий сверки'), ('recent_paper_reports', 'расчётов демоторговли моложе 5 минут'))), '']
    for group, title in ((1, 'Развитие программы'), (2, 'Развитие торговли')):
        if section is not None and section != group:
            continue
        lines.append(f'{group}. {title}')
        lines.append('')
        items = [item for item in report['advice'] if item['section'] == group]
        if not items:
            lines.append('В прочитанной диагностике нет оснований для этих советов. Это не полная проверка исправности программы.')
        for item in items:
            lines.extend([f'{item["number"]}. {item["title"]}',
                          'Важность: ' + {1:'высокая', 2:'средняя', 3:'плановая'}[item['priority']],
                          'Что замечено: ' + item['evidence'], 'Что улучшить: ' + item['action'],
                          'Как проверить пользу: ' + item['acceptance'], ''])
    lines.append('Для Work: перед изменениями подтвердить причину на текущих данных; сохранить проверки качества, источника, свежести цены и риска. '
                 'Этот текст не разрешает сделки, изменение допуска или автоматическую правку кода.')
    return '\n'.join(lines)
