"""Bounded GETTEX/LSEG comparison on the existing CandlePilot history lane."""
from bisect import bisect_right
from collections import Counter
from functools import lru_cache
from math import isfinite

import pandas as pd

import candle_schedule
import gettex_chart
from cp_controls import _automatic_repair_enabled, digest, read, write

PREFIX = 'cp_time_sales:'
SCAN_LIMIT = 4
ROTATION = 'cp_time_sales_rotation:'
CORE_STREAK = 2
ASSISTANT_CURSOR = 'cp_stock_assistant_cursor'
ASSISTANT_SCAN_LIMIT = 16
ASSISTANT_JOB_LIMIT = 4


class TimeSales(gettex_chart.Client):
    def request(self, method, path, **kwargs):
        if path == gettex_chart.HOST + '/rest/api/timeseries/intraday':
            path = gettex_chart.HOST + '/rest/api/timeseries/tns'
            params = kwargs['params']
            kwargs['params'] = dict(ric=params['ric'], fids='0,6,178,16,18',
                fromDate=int(pd.Timestamp(params['fromDate'], tz='UTC').timestamp()*1000),
                toDate=int(pd.Timestamp(params['toDate'], tz='UTC').timestamp()*1000),
                count=100)
        return super().request(method, path, **kwargs)


def compare(payload, identity, first, step, official):
    request = payload['request']
    start, end = first-step, first+2*step
    if (payload['status'] != 'OK' or request['ric'] != identity['symbol']
            or request['filter'] != 'TRADES_ONLY'
            or int(request['fromDate']) != start.timestamp()*1000
            or int(request['toDate']) != end.timestamp()*1000
            or int(request['count']) != 100 or not isinstance(payload['data'], list)
            or len(payload['data']) >= 100):
        raise ValueError('TIME_SALES_UNVERIFIED_RESPONSE')
    rows = [(pd.Timestamp(int(row['0']), unit='ms', tz='UTC'),
             float(row['6']), float(row['178'])) for row in payload['data']]
    if any(not start <= at <= end for at, _, _ in rows):
        raise ValueError('TIME_SALES_OUTSIDE_WINDOW')
    if any(not isfinite(price) or not isfinite(volume) or price <= 0 or volume < 0
           for _, price, volume in rows):
        raise ValueError('TIME_SALES_INVALID_TRADE')
    intervals = []
    for left in pd.date_range(start, end, freq=step, inclusive='left'):
        right = left+step
        exchange = Counter((price, volume) for at, price, volume in official if left <= at < right)
        vendor = Counter((price, volume) for at, price, volume in rows if left <= at < right)
        intervals.append(dict(start=left.isoformat(), exchange_rows=exchange.total(),
            vendor_rows=vendor.total(), exchange_only=(exchange-vendor).total(),
            vendor_only=(vendor-exchange).total()))
    return dict(state='COMPARED', mode='DIAGNOSTIC_ONLY', official_gap_covered=True,
        intervals=intervals, vendor_completeness='UNCONFIRMED',
        fields=sorted({field for row in payload['data'] for field in row}),
        response_digest=digest(payload))


def receipt_key(job_key):
    from cp_reconciliation import PREFIX as JOB_PREFIX
    return PREFIX + job_key[len(JOB_PREFIX):]


def closed_core_windows(now):
    return _closed_core_windows(now.floor('min'))


@lru_cache(maxsize=16)
def _closed_core_windows(now):
    from data_quality import expected_index
    from timeframes import endpoints

    meta = dict(venue='GETTEX', timezone='Europe/Berlin')
    local = now.tz_convert(meta['timezone']).tz_localize(None)
    first = max(local.normalize()-pd.Timedelta(days=7), pd.Timestamp(f'{local.year}-01-01'))
    starts, ends = [], []
    for tf in ('1H', '4H'):
        labels = expected_index(meta, tf, first, local, now)
        if len(labels):
            frame = pd.DataFrame(index=labels[-1:])
            frame.attrs = {**meta, 'timeframe':tf}
            starts.append(labels[-1])
            ends.append(endpoints(frame)[0].tz_convert(meta['timezone']).tz_localize(None))
    # The most recent closed 1H and 4H bars may be disjoint during a new session.
    return tuple((str(start),str(end)) for start,end in zip(starts,ends))


def _due_jobs(db, isin, now, local_only=False):
    from cp_reconciliation import PREFIX as JOB_PREFIX
    try:
        core = tuple(at for pair in closed_core_windows(now) for at in pair)
    except ValueError:
        core = ()  # Unsupported session calendar: preserve ordinary history.
    core += (None,)*(4-len(core))
    lane = 'local' if local_only else 'provider'
    prefer_core = read(db, ROTATION+lane+':'+isin, 0) < CORE_STREAK
    return db.execute("""SELECT job.key,
        CASE WHEN json_extract(job.value,'$.tf')='15m' AND
          (json_extract(job.value,'$.first')>=? AND json_extract(job.value,'$.first')<?
           OR json_extract(job.value,'$.first')>=? AND json_extract(job.value,'$.first')<?)
          THEN 1 ELSE 0 END AS core
        FROM settings AS job
        LEFT JOIN settings AS receipt
            ON receipt.key=? || substr(job.key,?)
        WHERE job.key>=? AND job.key<?
        AND json_extract(job.value,'$.identity.source')='gettex_chart'
        AND json_extract(job.value,'$.identity.venue')='GETTEX'
        AND json_extract(job.value,'$.identity.currency')='EUR'
        AND json_extract(job.value,'$.identity.timezone')='Europe/Berlin'
        AND json_extract(job.value,'$.tf') IN ('5m','15m')
        AND json_extract(job.value,'$.first')=json_extract(job.value,'$.last')
        AND json_extract(job.value,'$.finding.state')='SOURCE_TIMEBASE_UNCONFIRMED'
        AND json_extract(job.value,'$.finding.market_state')='NO_TRADES'
        AND COALESCE(json_extract(receipt.value,'$.state'),
                     json_extract(job.value,'$.time_sales.state'),'')!='COMPARED'
        AND (NOT ? OR COALESCE(json_extract(receipt.value,'$.state'),'')!='LOCAL_COMPARED')
        AND (receipt.key IS NULL AND json_extract(job.value,'$.time_sales.retry_at') IS NULL
             OR julianday(COALESCE(json_extract(receipt.value,'$.retry_at'),
                                  json_extract(job.value,'$.time_sales.retry_at')))<=julianday(?))
        AND julianday(datetime(json_extract(job.value,'$.first'),
            CASE json_extract(job.value,'$.tf') WHEN '5m' THEN '+10 minutes' ELSE '+30 minutes' END))
            <= julianday(?)
        ORDER BY core=? DESC, COALESCE(json_extract(receipt.value,'$.checked_at'),
                          json_extract(job.value,'$.time_sales.checked_at'),''),
                 json_extract(job.value,'$.first') DESC, job.key LIMIT ?""",
        (*core,
         PREFIX, len(JOB_PREFIX)+1, JOB_PREFIX+isin+':', JOB_PREFIX+isin+';', local_only,
         now.isoformat(), now.tz_convert('Europe/Berlin').tz_localize(None).isoformat(),
         prefer_core, SCAN_LIMIT)).fetchall()


def _advance_rotation(db, isin, lane, core):
    key = ROTATION+lane+':'+isin
    previous = read(db,key,0)
    streak = min(previous+1, CORE_STREAK) if core else 0
    if streak != previous:
        write(db,key,streak)


def _checked_job(db, job_key, isin, now):
    from cp_reconciliation import _input

    job = read(db, job_key)
    if job.get('isin') != isin:
        raise ValueError('TIME_SALES_IDENTITY_CHANGED')
    inputs = _input(db, job)
    first = pd.Timestamp(job['first'], tz='Europe/Berlin').tz_convert('UTC')
    step = pd.Timedelta(minutes=gettex_chart.INTRADAY_MINUTES[job['tf']])
    if inputs['rows'] or first+2*step > now:
        raise ValueError('TIME_SALES_GAP_CHANGED')
    return job, inputs, first, step


def _local_time(at):
    return str(at.tz_convert('Europe/Berlin').tz_localize(None))


def _official_trades(db, isin, start, end):
    return db.execute('SELECT ts,price,volume FROM trades WHERE isin=? AND ts>=? AND ts<? ORDER BY ts',
                      (isin, _local_time(start), _local_time(end))).fetchall()


def _local_comparison(inputs, first, step, official):
    """Use saved rows immediately, without treating neighbour trades as complete."""
    neighbors = {row[0]: row[5] for row in inputs.get('neighbors', {}).values() if row}
    intervals = []
    for left in (first-step, first, first+step):
        start, end = _local_time(left), _local_time(left+step)
        rows = [volume for at, _, volume in official if start <= at < end]
        intervals.append(dict(start=left.isoformat(), exchange_saved_rows=len(rows),
            exchange_saved_volume=sum(rows), chart_saved_volume=neighbors.get(start)))
    return dict(mode='DIAGNOSTIC_ONLY', official_gap_covered=True, intervals=intervals)


def _prepare(db, job_key, isin, now):
    job, inputs, first, step = _checked_job(db, job_key, isin, now)
    official = _official_trades(db, isin, first-step, first+2*step)
    _check_official_gap(job, inputs, first, step, official)
    return job, inputs, first, step, official


def _local_receipt(inputs, first, step, official, now):
    return dict(state='LOCAL_COMPARED', checked_at=now.isoformat(), retry_at=now.isoformat(),
        input_fingerprint=inputs['fingerprint'], local=_local_comparison(inputs, first, step, official),
        escalation=dict(owner='CandlePilot', reason='SOURCE_TIMEBASE_UNCONFIRMED'))


def _waiting_receipt(error, now):
    return dict(state='WAITING_EVIDENCE', mode='DIAGNOSTIC_ONLY', reason=str(error),
        checked_at=now.isoformat(), retry_at=(now+pd.Timedelta(hours=1)).isoformat(),
        escalation=dict(owner='CandlePilot', reason=str(error)))


def prepare_assistants(store, infos):
    """Rotate through stocks in bounded slices, independently of acquisition."""
    policy = store.setting('cp_gettex_source_policy', {})
    ordered = sorted({info['isin'] for info in infos if _automatic_repair_enabled(info, policy)})
    if not ordered:
        return
    previous = store.setting(ASSISTANT_CURSOR, '')
    start = bisect_right(ordered, previous)
    prepared = 0
    for isin in (ordered[start:]+ordered[:start])[:ASSISTANT_SCAN_LIMIT]:
        now = candle_schedule.clock()
        with store.connect() as db:
            db.execute('BEGIN')
            if read(db, 'cp_gettex_source_policy', {}) != policy:
                return
            jobs = _due_jobs(db, isin, now, local_only=True)
            if not jobs:
                continue
            job_key, core = jobs[0]
            job = read(db, job_key)
            prior = read(db, receipt_key(job_key))
            try:
                captured = _prepare(db, job_key, isin, now)
                result = _local_receipt(*captured[1:], now)
            except (ValueError, TypeError, KeyError) as error:
                captured = None
                result = _waiting_receipt(error, now)
        prepared += 1
        result['worker'] = 'stock_assistant'
        result['priority'] = 'CURRENT_1H_4H' if core else 'HISTORY'
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            receipt = receipt_key(job_key)
            if (read(db, 'cp_gettex_source_policy', {}) == policy
                    and read(db, job_key) == job and read(db, receipt) == prior):
                try:
                    current = _prepare(db, job_key, isin, now)
                    valid = current == captured
                except (ValueError, TypeError, KeyError) as error:
                    valid = captured is None and result['reason'] == str(error)
                if valid:
                    write(db, receipt, result)
                    _advance_rotation(db, isin, 'local', core)
        if prepared >= ASSISTANT_JOB_LIMIT:
            break
    if isin != previous:
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if read(db, ASSISTANT_CURSOR, '') == previous:
                write(db, ASSISTANT_CURSOR, isin)


def turn(store, info):
    from cp_reconciliation import _input

    isin = info['isin']
    now = candle_schedule.clock()
    if not _automatic_repair_enabled(info, store.setting('cp_gettex_source_policy', {})):
        return
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        policy = read(db, 'cp_gettex_source_policy', {})
        if not _automatic_repair_enabled(info, policy):
            return
        for job_key, core in _due_jobs(db, isin, now):
            try:
                job, inputs, first, step, official = _prepare(db, job_key, isin, now)
            except (ValueError, KeyError, TypeError) as error:
                write(db, receipt_key(job_key), _waiting_receipt(error, now))
                continue
            break
        else:
            return
        identity = job['identity']
        base = job['tf']
        start, end = first-step, first+2*step
        receipt = receipt_key(job_key)
        prepared = _local_receipt(inputs, first, step, official, now)
        prepared['priority'] = 'CURRENT_1H_4H' if core else 'HISTORY'
        local = prepared['local']
        prior = read(db, receipt, {})
        if prior.get('input_fingerprint') != inputs['fingerprint'] or prior.get('local') != local:
            write(db, receipt, prepared)
        for setting in ('cp_source_retry:gettex_chart', gettex_chart.state_key(isin,base),
                        gettex_chart.recovery_key(isin,identity,base)):
            value = read(db, setting)
            retry = value.get('retry_after') if isinstance(value, dict) else value
            if retry and pd.Timestamp(retry) > now:
                return
        if not gettex_chart.reserve_request(db, now, isin, base, recovery=True):
            return
        _advance_rotation(db, isin, 'provider', core)
    requested_at = candle_schedule.clock()
    try:
        payload, returned = TimeSales().fetch(isin, base, start, end)
        if returned != identity:
            raise ValueError('TIME_SALES_IDENTITY_CHANGED')
        comparison = compare(payload, identity, first, step,
            [(pd.Timestamp(at, tz=job['timezone']), p, v) for at, p, v in official])
    except (ValueError, KeyError, TypeError, gettex_chart.requests.RequestException) as error:
        status = getattr(getattr(error, 'response', None), 'status_code', None)
        if status in (429, 503):
            candle_schedule.finish(store, isin, identity, base,
                {'key': gettex_chart.recovery_key(isin,identity,base),
                 'reason': 'BOUNDED_TNS_DIAGNOSTIC'}, error=error)
        comparison = dict(state='UNAVAILABLE', mode='DIAGNOSTIC_ONLY',
            error=type(error).__name__, http_status=status,
            retry_at=(now+pd.Timedelta(hours=24)).isoformat())
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = read(db, job_key)
        if (current != job or read(db, 'cp_gettex_source_policy', {}) != policy
                or _input(db, job)['fingerprint'] != inputs['fingerprint']
                or official != _official_trades(db, isin, start, end)):
            return
        finished_at = candle_schedule.clock()
        write(db, receipt, dict(comparison, checked_at=finished_at.isoformat(), local=local,
            escalation=prepared['escalation'],
            priority=prepared['priority'],
            worker=prior.get('worker','history'),
            request_seconds=round((finished_at-requested_at).total_seconds(), 3),
            input_fingerprint=inputs['fingerprint']))
    return comparison


def _check_official_gap(job, inputs, first, step, official):
    proof = inputs['proof']
    identity = {**job['identity'], 'source': 'gettex', 'symbol': job['isin']}
    five_minutes = pd.Timedelta(minutes=5)
    expected = {(_local_time(at), _local_time(at+five_minutes))
                for at in pd.date_range(first, first+step, freq=five_minutes, inclusive='left')}
    if (proof.get('basis') != 'complete_published_trade_files'
            or proof.get('isin') != job['isin'] or proof.get('identity') != identity
            or not proof.get('reference')
            or not expected.issubset(map(tuple, proof.get('no_trades', [])))
            or proof.get('incomplete_publication_intervals')):
        raise ValueError('TIME_SALES_OFFICIAL_COVERAGE_UNCONFIRMED')
    if any(first <= pd.Timestamp(at, tz='Europe/Berlin') < first+step
           for at, _, _ in official):
        raise ValueError('TIME_SALES_OFFICIAL_COVERAGE_CONFLICT')
