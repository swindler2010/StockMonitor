"""Collect every available official slice, with file-date anchored UTC times."""
import gzip,re,threading,time
from urllib.parse import urljoin,urlparse
from pathlib import Path
import pandas as pd
from urllib.error import URLError, HTTPError

LOCK=threading.Lock()
PAGE='https://www.gettex.de/handel/delayed-data/posttrade-data/'


def published_files(html):
    """Keep the actual published port/path; one entry per shared file."""
    result={}
    for link in re.findall(r'''href=["']([^"']*posttrade\.[^"']+\.csv\.gz[^"']*)["']''',html,re.I):
        url=urljoin(PAGE,link.replace('&amp;','&'));parts=urlparse(url)
        name=Path(parts.path).name
        if (parts.scheme=='https' and parts.hostname=='erdk.bayerische-boerse.de'
            and re.fullmatch(r'posttrade\.\d{8}\.\d{2}\.\d{2}\.(mund|munc)\.csv\.gz',name,re.I)):
            result[name]=url
    return result


def _replacement_lines(content,name,retained=()):
    """Validate additions; retained raw rows keep their per-instrument DQ.

    A shared publication may contain a rejected row for an unrelated security.
    Preserving that exact original is not a fresh validation of its OHLCV.
    Multiplicity matters: an additional copy still has to pass validation.
    """
    import math
    from collections import Counter
    unchanged=Counter(retained)
    lines=content.decode('utf-8-sig').splitlines()
    parts=name.split('.')
    end=pd.to_datetime(parts[1]+parts[2]+parts[3],format='%Y%m%d%H%M',utc=True)
    lo=(end-pd.Timedelta(minutes=15)).strftime('%H:%M:%S');hi=end.strftime('%H:%M:%S')
    if not lines:raise ValueError('UNCONFIRMED_REPLACEMENT_FILE: '+name)
    for line in lines:
        if unchanged[line]:
            unchanged[line]-=1
            continue
        f=line.split(',')
        if (len(f)!=5 or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d',f[0])
            or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?',f[1])
            or not re.fullmatch('[A-Z]{3}',f[2])):
            raise ValueError('UNCONFIRMED_REPLACEMENT_FILE: '+name)
        stamp=f[1]
        # The source may include an exact endpoint with fractional zeroes.
        stamp=stamp.rstrip('0').rstrip('.') if '.' in stamp else stamp
        inside=(lo<=stamp<=hi) if lo<hi else (stamp>=lo or stamp<=hi)
        try:price,volume=float(f[3]),float(f[4])
        except ValueError:raise ValueError('INVALID_REPLACEMENT_VALUE: '+name)
        if not inside or not math.isfinite(price) or not math.isfinite(volume) or price<=0 or volume<0:
            raise ValueError('INVALID_REPLACEMENT_TIME_OR_VALUE: '+name)
    return lines

def fetch(http, url):
    """Retry transient transport failures only; preserve the original error."""
    for attempt in range(3):
        try:return http(url)
        except HTTPError as exc:
            if exc.code not in (408,500,502,503,504) or attempt==2:raise
        except (TimeoutError, URLError, ConnectionError):
            if attempt==2:raise
        time.sleep(0.5 * 2**attempt)

def download(isin,http,parse,store,start=None,end=None):
    try:return _download(isin,http,parse,store,start,end)
    except Exception as error:
        from delivery_observations import outcome
        import candle_schedule
        if not getattr(error,'_delivery_outcome_recorded',False):
            outcome(store.directory,isin,'trades',candle_schedule.gettex_meta(isin),'ERROR',error)
        raise


def _download(isin,http,parse,store,start=None,end=None):
    import candle_schedule as scheduler
    cooldown=store.setting('cp_source_retry:gettex',None)
    if cooldown and pd.Timestamp.now(tz='UTC')<pd.Timestamp(cooldown):raise ValueError('GETTEX source cooldown')
    original_http=http
    def guarded_http(url):
        retry=store.setting('cp_source_retry:gettex',None)
        if retry and pd.Timestamp.now(tz='UTC')<pd.Timestamp(retry):raise ValueError('GETTEX source cooldown')
        try:return original_http(url)
        except HTTPError as error:
            if error.code==429:scheduler.finish(store,isin,scheduler.gettex_meta(isin),'5m',{'key':'cp_source_status:gettex'},error)
            raise
    http=guarded_http
    html=fetch(http,PAGE).decode('utf-8','ignore')
    locations=published_files(html)
    cache=store.directory/'gettex_files';cache.mkdir(exist_ok=True)
    if start is not None:
        # Recovery can consume historical files acquired by CP even after the
        # provider removes their links from today's index. No broad re-import.
        # Cached history needs no invented network URL. In particular retain
        # :8000 and the published path when an official location is available.
        for p in cache.glob('posttrade.*.csv.gz'):locations.setdefault(p.name,None)
    frames=[];errors=[];network_returns=[];cached_files=0
    for name,url in sorted(locations.items()):
        match=re.fullmatch(r'posttrade\.(\d{8})\.(\d{2})\.(\d{2})\.(mund|munc)\.csv\.gz',name,re.I)
        if not match:continue
        at=pd.to_datetime(''.join(match.group(i) for i in (1,2,3)),format='%Y%m%d%H%M',utc=True)
        if start is not None and (at<pd.Timestamp(start).tz_convert('UTC').floor('h')-pd.Timedelta(hours=1) or at>pd.Timestamp(end).tz_convert('UTC').ceil('h')+pd.Timedelta(hours=1)):continue
        path=cache/name
        try:
            with LOCK:
                if path.exists():
                    raw=path.read_bytes();cached_files+=1
                else:
                    if url is None:raise ValueError('CACHED_FILE_DISAPPEARED_WITHOUT_PUBLISHED_URL')
                    raw=fetch(http,url)
                    network_returns.append(pd.Timestamp.now(tz='UTC').isoformat())
                    gzip.decompress(raw) # Do not persist an HTML error as a data file.
                    path.write_bytes(raw)
            frame=parse(gzip.decompress(raw),isin,file_time=at)
            if not frame.empty:frames.append(frame)
        except Exception as e:errors.append(name+': '+str(e))
    if errors:store.set_setting('gettex_file_errors:'+isin,errors[-10:])
    else:store.set_setting('gettex_file_errors:'+isin,[])
    # Retain source evidence until archive classification has consumed it.
    # SQLite trades alone cannot prove that a missing interval had no trades.
    if not frames:
        from delivery_observations import outcome
        error=ValueError('В доступных файлах GETTEX нет сделок EUR для '+isin+(' • '+errors[0] if errors else ''))
        outcome(store.directory,isin,'trades',scheduler.gettex_meta(isin),'ERROR' if errors else 'EMPTY',error if errors else None)
        error._delivery_outcome_recorded=True
        raise error
    result=pd.concat(frames).sort_index()
    if start is not None:
        begin=pd.Timestamp(start).tz_convert('Europe/Berlin').tz_localize(None)
        finish=pd.Timestamp(end).tz_convert('Europe/Berlin').tz_localize(None)
        result=result.loc[(result.index>=begin)&(result.index<finish)]
    result=result.rename_axis('at').reset_index().drop_duplicates().set_index('at').rename_axis(None)
    # http may itself cache; callback return must not be called HTTP receipt.
    observed=result.copy(deep=False)
    observed.attrs.update(scheduler.gettex_meta(isin))
    from delivery_observations import observe
    observe(store.directory,isin,'trades',observed,receipt_kind='TRANSPORT_RETURN_OR_CACHE' if network_returns else 'DISK_CACHE',
            transport_returned_at=max(network_returns) if network_returns else None)
    return result


def attach_known_interruptions(info,store,now=None):
    """Attach reviewed exchange evidence on the existing CP writer; no HTTP.

    Only an established native equity listing. Keep file evidence, history,
    instruments and grants unchanged. A repeat writes nothing.
    """
    import json
    from data_quality import GETTEX_EQUITY_INTERRUPTION
    if info.get('kind','stock')!='stock':return False
    clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    if clock<pd.Timestamp('2026-11-02T17:15:00Z'):return False # actual evidence verification
    isin=info['isin'];key='interval_evidence:'+isin
    meta=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
        routing=('_cp_focus','_cp_foreground','_cp_position','_gettex_poll_only','_review_only','_eligibility_only')
        if not row or json.loads(row[0])!={k:v for k,v in info.items() if k not in routing}:raise ValueError('Evidence instrument changed')
        row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone();proof=json.loads(row[0]) if row else {}
        if proof.get('identity')!=meta:return False
        if not db.execute("SELECT 1 FROM candles WHERE isin=? AND source='gettex' LIMIT 1",(isin,)).fetchone():return False
        entries=proof.get('exchange_closures',[])
        if GETTEX_EQUITY_INTERRUPTION in entries:return False
        proof['exchange_closures']=[*entries,GETTEX_EQUITY_INTERRUPTION];proof['closure_checked_at']=clock.isoformat()
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(proof)))
    return True


def classify_cached_session(info,store,parse,now=None,*,current_only=False):
    """One session (or published current-session prefix) per existing feed job.

    Completeness uses the already verified GETTEX 15-minute munc/mund contract.
    Unsupported calendars/formats and incomplete files never certify absence.
    Progress and proof commit together; restart resumes unchanged completed days.
    """
    import hashlib,json,math
    from timeframes import schedule
    isin=info['isin'];clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    meta=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin')
    cache=store.directory/'gettex_files'
    proof_key='interval_evidence:'+isin;progress_key='history_evidence_progress:'+isin
    previous=store.setting(proof_key,{})
    progress=store.setting(progress_key,{})
    days={}
    for p in cache.glob('posttrade.*.csv.gz'):
        m=re.fullmatch(r'posttrade\.(\d{8})\.\d{2}\.\d{2}\.(mund|munc)\.csv\.gz',p.name)
        if m:days.setdefault(m[1],[]).append(p)
    # Preserve verified manual evidence; do not reprocess 20Oct or any fully
    # described session just because the producer was subsequently automated.
    today=clock.tz_convert('Europe/Berlin').strftime('%Y%m%d')
    latest=max(progress,key=lambda d:progress[d].get('checked_at',''),default=None)
    # Current evidence gets the first available turn, then the oldest pending
    # session gets a turn. New live files cannot monopolize historical work.
    order=sorted(days,key=lambda d:(0 if d==today and latest!=today else 1,d))
    for date in order:
        if current_only and date!=today:continue
        paths=days[date]
        day=pd.to_datetime(date,format='%Y%m%d')
        if date>today:continue
        eligible_names=None;prefix_end=None
        def source_signature():
            files=cache.glob('posttrade.'+date+'.*.csv.gz')
            stamps=sorted((p.name,p.stat().st_size,p.stat().st_mtime_ns) for p in files if eligible_names is None or p.name in eligible_names)
            # Preserve old closed-session signatures: no repeated archive audit.
            value=stamps if prefix_end is None else [stamps,prefix_end.isoformat()]
            return hashlib.sha256(json.dumps(value).encode()).hexdigest()
        signature=source_signature()
        done=progress.get(date,{})
        if date!=today and done.get('signature')==signature and done.get('evidence_policy')==3:continue
        try:
            sched=schedule(meta,day,day)
            if sched.empty:
                result={'state':'MARKET_CLOSED','signature':signature,'checked_at':clock.isoformat()}
                break
            opening,closing=sched.iloc[0]['open'],sched.iloc[0]['close']
            session_close=closing
            # A file's end timestamp is not its publication time. Keep the
            # existing conservative 15-minute allowance, including DST days.
            closing=min(closing,(clock-pd.Timedelta(minutes=15)).floor('15min'))
            if closing<=opening:continue
            required={f"posttrade.{t.strftime('%Y%m%d.%H.%M')}.{venue}.csv.gz" for t in pd.date_range(opening+pd.Timedelta(minutes=15),closing,freq='15min') for venue in ('munc','mund')}
            if closing<session_close:
                eligible_names=required;prefix_end=closing;signature=source_signature()
            if (done.get('signature')==signature and done.get('evidence_policy')==3
                and (date!=today or done.get('coverage_review_version')==1)):continue
            grid=pd.date_range(opening,closing,freq='5min',inclusive='left').tz_convert(meta['timezone']).tz_localize(None)
            if not done and previous.get('identity')==meta and previous.get('reference'):
                described={pd.Timestamp(a) for a,b in previous.get('no_trades',[])+previous.get('trade_intervals',[]) if pd.Timestamp(b)-pd.Timestamp(a)==pd.Timedelta(minutes=5)}
                if set(grid)<=described:
                    result={'state':'DONE_EXISTING_EVIDENCE','signature':signature,'checked_at':clock.isoformat()};break
            available={p.name:p for p in paths}
            summary=_cached_session_summary(store,parse,date,signature,available,required,grid,isin)
            if summary['errors'].get(isin):raise ValueError(summary['errors'][isin])
            bins=set(pd.to_datetime(summary['bins'][isin]));manifest=summary['manifest']
            executed=set(pd.to_datetime(summary['executed_bins'][isin]))
            covered=set(pd.to_datetime(summary['covered_intervals']))
            if not bins:
                with store.connect() as db:
                    established=db.execute("SELECT 1 FROM candles WHERE isin=? AND source='gettex' LIMIT 1",(isin,)).fetchone()
                if not established and previous.get('identity')!=meta:raise ValueError('GETTEX listing not established by trades; empty day is not identity evidence')
            proof=dict(previous) if previous.get('identity')==meta else {}
            def retain(pair):return pd.Timestamp(pair[0]).date()!=day.date()
            proof.update(isin=isin,identity=meta,basis='complete_published_trade_files',checked_at=clock.isoformat())
            proof['no_trades']=[p for p in proof.get('no_trades',[]) if retain(p)]+[[str(t),str(t+pd.Timedelta(minutes=5))] for t in sorted(covered-bins)]
            proof['trade_intervals']=[p for p in proof.get('trade_intervals',[]) if retain(p)]+[[str(t),str(t+pd.Timedelta(minutes=5))] for t in sorted(executed)]
            # A zero-quantity publication proves a price observation, not an
            # execution or the absence of executions. Keep its interval unknown
            # rather than inventing a missing trade or a NO_TRADES certificate.
            proof['zero_quantity_intervals']=[p for p in proof.get('zero_quantity_intervals',[]) if retain(p)]+[[str(t),str(t+pd.Timedelta(minutes=5))] for t in sorted(bins-executed)]
            # Existing 15m rows may themselves be partial. Keep that source
            # uncertainty visible even when a row exists at the expected time.
            partial=[]
            for name in summary.get('unconfirmed_files',{}):
                if not summary['unconfirmed_files'][name].startswith('SOURCE_COVERAGE_CONFLICT'):continue
                parts=name.split('.');right=pd.to_datetime(parts[1]+parts[2]+parts[3],format='%Y%m%d%H%M',utc=True).tz_convert(meta['timezone']).tz_localize(None)
                partial.append([str(right-pd.Timedelta(minutes=15)),str(right)])
            proof['incomplete_publication_intervals']=[p for p in proof.get('incomplete_publication_intervals',[]) if retain(p)]+sorted(partial)
            proof['reference']={**proof.get('reference',{}),'GETTEX:'+date:manifest}
            proof['limits']='Only explicitly covered intervals with both validated MUNC/MUND files; no assertion for incomplete intervals or future corrections.'
            result={'state':'PARTIAL' if summary['unconfirmed_files'] else 'DONE','signature':signature,'checked_at':clock.isoformat(),'real_intervals':len(executed),'observed_intervals':len(bins),'zero_quantity_only_intervals':len(bins-executed),'no_trades':len(covered-bins),'files':len(manifest),
                    'unconfirmed_intervals':len(set(grid)-covered),'unconfirmed_files':summary['unconfirmed_files'],'evidence_policy':3,
                    'coverage_review_version':1,'coverage_conflicts':summary.get('coverage_conflicts',[])}
            if prefix_end is not None:result.update(scope='PUBLISHED_SESSION_PREFIX',covered_until=closing.isoformat(),session_close=session_close.isoformat())
        except (ValueError,KeyError,UnicodeError,OSError) as error:
            result={'state':'UNCONFIRMED','reason':str(error),'signature':signature,'checked_at':clock.isoformat(),'evidence_policy':3}
        break
    else:return {'state':'IDLE','sessions':len(progress)}
    withdraw=result['state']=='UNCONFIRMED' and done.get('state') in ('DONE','PARTIAL') and previous.get('identity')==meta
    if withdraw:
        # Changed source evidence cannot leave a stale NO_TRADES certificate live.
        proof={**previous,'checked_at':clock.isoformat()}
        for field in ('no_trades','trade_intervals','zero_quantity_intervals','incomplete_publication_intervals'):
            proof[field]=[p for p in previous.get(field,[]) if pd.Timestamp(p[0]).date()!=day.date()]
        proof['reference']={k:v for k,v in previous.get('reference',{}).items() if k!='GETTEX:'+date}
        proof['withdrawn_session']={'date':date,'reason':result.get('reason'),'checked_at':clock.isoformat()}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
        # The feed attaches routing context to the captured instrument. It is
        # not persisted identity; every actual instrument field still matches.
        routing=('_cp_focus','_cp_foreground','_cp_position','_gettex_poll_only','_review_only','_eligibility_only')
        captured={k:v for k,v in info.items() if k not in routing}
        if current is None or json.loads(current[0])!=captured:raise ValueError('Evidence instrument changed')
        old=db.execute('SELECT value FROM settings WHERE key=?',(proof_key,)).fetchone()
        if (json.loads(old[0]) if old else {})!=previous:raise ValueError('Evidence changed concurrently')
        prior=db.execute('SELECT value FROM settings WHERE key=?',(progress_key,)).fetchone()
        if (json.loads(prior[0]) if prior else {})!=progress:raise ValueError('Progress changed concurrently')
        if source_signature()!=signature:raise ValueError('Evidence files changed concurrently')
        if result.get('coverage_conflicts') is not None:
            if _coverage_conflicts(store,summary,db)!=result['coverage_conflicts']:
                raise ValueError('Coverage reference changed concurrently')
        if result['state'] in ('DONE','PARTIAL') or withdraw:db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(proof_key,json.dumps(proof)))
        result['evidence_policy']=3
        progress[date]=result
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(progress_key,json.dumps(progress)))
    return {**result,'date':date}


def _cached_session_summary(store,parse,date,signature,available,required,grid,requested):
    """Shared source-file decoding inside the existing feed process/cache.

    Parse every registered ISIN through the existing parser once per changed
    file set. A newly added ISIN invalidates this small summary, not its OHLCV.
    """
    import hashlib,json,math
    identities=sorted({i['isin'] for i in store.instruments()}|{requested})
    version=hashlib.sha256(json.dumps([4,signature,identities]).encode()).hexdigest()
    path=store.directory/'gettex_files'/('interval-summary.'+date+'.json')
    if path.exists():
        try:
            saved=json.loads(path.read_text(encoding='utf-8'))
            if saved.get('version')==version:return _review_coverage(store,saved)
        except (OSError,ValueError):pass
    bins={i:set() for i in identities};executed_bins={i:set() for i in identities};errors={};manifest={};unconfirmed={};valid_files=set();tails=[]
    for name in sorted(required):
        try:
            raw=available[name].read_bytes()
            content=gzip.decompress(raw);lines=content.decode('utf-8-sig').splitlines()
            if not lines:raise ValueError('EMPTY_SOURCE_FILE')
            if any(len(line.split(','))!=5 or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d',line.split(',')[0]) for line in lines):
                raise ValueError('UNCONFIRMED_SOURCE_FORMAT')
        except (KeyError,OSError,EOFError,UnicodeError,ValueError) as error:
            unconfirmed[name]=type(error).__name__+': '+str(error);continue
        manifest[name]=hashlib.sha256(raw).hexdigest();valid_files.add(name)
        selected={}
        for line in lines:
            fields=line.split(',')
            if len(fields)!=5 or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d',fields[0]):raise ValueError('Unconfirmed source format: '+name)
            isin=fields[0]
            if isin not in bins:continue
            try:
                valid=fields[2]=='EUR' and math.isfinite(float(fields[3])) and float(fields[3])>0 and math.isfinite(float(fields[4])) and float(fields[4])>=0
            except ValueError:valid=False
            if not valid:errors[isin]='Invalid published trade: '+name
            selected.setdefault(isin,[]).append(line)
        parts=name.split('.');anchor=pd.to_datetime(parts[1]+parts[2]+parts[3],format='%Y%m%d%H%M',utc=True)
        lo=(anchor-pd.Timedelta(minutes=15)).tz_convert('Europe/Berlin').tz_localize(None)
        hi=anchor.tz_convert('Europe/Berlin').tz_localize(None)
        # An early last record is only a reason to compare saved evidence,
        # never by itself proof of a gap or of an incomplete exchange file.
        try:
            last=pd.Timestamp(anchor.strftime('%Y-%m-%d')+' '+max(line.split(',')[1] for line in lines),tz='UTC')
            if last<anchor-pd.Timedelta(minutes=5):
                tails.append(dict(file=name,after=str(last.tz_convert('Europe/Berlin').tz_localize(None).ceil('5min')),
                                  end=str(hi),isins=sorted(selected)))
        except (ValueError,TypeError):pass
        for isin,rows in selected.items():
            if isin in errors:continue
            trades=parse(('\n'.join(rows)+'\n').encode(),isin,file_time=anchor)
            if len(trades)!=len(rows) or not ((trades.index>=lo)&(trades.index<=hi)).all():
                errors[isin]='Parser excluded rows or trade outside slice: '+name;continue
            bins[isin].update(trades.index.floor('5min').intersection(grid))
            executed_bins[isin].update(trades.loc[trades.volume>0].index.floor('5min').intersection(grid))
    # An absent/empty/corrupt file invalidates only its 15-minute pair, not
    # independent verified slices of the session. Never infer empty = no trades.
    covered=[]
    for stamp in grid:
        anchor=(stamp.tz_localize('Europe/Berlin').tz_convert('UTC').floor('15min')+pd.Timedelta(minutes=15))
        pair={f"posttrade.{anchor.strftime('%Y%m%d.%H.%M')}.{mic}.csv.gz" for mic in ('munc','mund')}
        if pair<=valid_files:covered.append(str(stamp))
    result={'version':version,'manifest':manifest,'bins':{i:sorted(map(str,b)) for i,b in bins.items()},'executed_bins':{i:sorted(map(str,b)) for i,b in executed_bins.items()},'errors':errors,
            'covered_intervals':covered,'unconfirmed_files':unconfirmed,'coverage_candidates':tails}
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(result),encoding='utf-8');tmp.replace(path)
    return _review_coverage(store,result)


def _coverage_conflicts(store,summary,db=None):
    """Contradictory native evidence withdraws absence, never supplies prices.

    Only confirmed GETTEX/EUR 5m rows entirely beyond a file's early tail and
    absent from BOTH official segments can trigger review. No TV scraping,
    price tolerance or source promotion; a discrepancy remains UNCONFIRMED.
    """
    import json,math
    if not summary.get('coverage_candidates'):return []
    if db is None:
        with store.connect() as connection:return _coverage_conflicts(store,summary,connection)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='series' AND type='table'").fetchone():return []
    from gettex_chart import confirmed_state
    result=[]
    for candidate in summary['coverage_candidates']:
        for isin in candidate['isins']:
            item=db.execute('SELECT payload FROM instruments WHERE isin=?',(isin,)).fetchone()
            saved=db.execute('SELECT value FROM settings WHERE key=?',('gettex_chart:'+isin+':5m',)).fetchone()
            if not item or not saved:continue
            info=json.loads(item[0]);state=json.loads(saved[0])
            if not confirmed_state(info,state):continue
            meta=state['identity']
            rows=db.execute('SELECT ts,open,high,low,close,volume FROM series WHERE isin=? AND source=? AND venue=? AND currency=? AND symbol=? AND base_tf=? AND ts>=? AND ts<? ORDER BY ts',
                (isin,meta['source'],meta['venue'],meta['currency'],meta['symbol'],'5m',candidate['after'],candidate['end'])).fetchall()
            for stamp,o,h,l,c,v in rows:
                if stamp in summary['bins'].get(isin,[]):continue
                if not all(x is not None and math.isfinite(x) for x in (o,h,l,c,v)):continue
                if min(o,l,c)<=0 or v<=0 or h<max(o,l,c) or l>min(o,h,c):continue
                result.append(dict(file=candidate['file'],isin=isin,identity=meta,tf='5m',ts=stamp,ohlcv=[o,h,l,c,v],
                                   reason='SOURCE_COVERAGE_CONFLICT: saved positive GETTEX chart bar beyond official file tail'))
    return result


def _review_coverage(store,summary):
    result=dict(summary);conflicts=_coverage_conflicts(store,summary)
    result['coverage_conflicts']=conflicts
    result['unconfirmed_files']=dict(summary['unconfirmed_files'])
    rejected={c['file'] for c in conflicts}
    for name in rejected:result['unconfirmed_files'][name]='SOURCE_COVERAGE_CONFLICT: official publication and confirmed native chart disagree'
    if rejected:
        def unaffected(stamp):
            anchor=pd.Timestamp(stamp).tz_localize('Europe/Berlin').tz_convert('UTC').floor('15min')+pd.Timedelta(minutes=15)
            return not any(f"posttrade.{anchor.strftime('%Y%m%d.%H.%M')}.{mic}.csv.gz" in rejected for mic in ('munc','mund'))
        result['covered_intervals']=[s for s in summary['covered_intervals'] if unaffected(s)]
    return result



def acquire_missing_evidence(info,store,http,now=None,budget=8):
    """Bounded official-file acquisition in the existing serialized feed worker.

    Files are shared by all ISINs. Acquisition never certifies absence or changes
    OHLCV/DQ. Existing classifier validates complete sessions on its next turn.
    Persistent per-day leases/cooldowns also survive an interrupted application.
    """
    import json,hashlib
    from timeframes import schedule
    clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    cooldown=max(filter(None,[store.setting('candlepilot_source_cooldown',None),store.setting('cp_source_retry:gettex',None)]),default=None)
    if cooldown and pd.Timestamp(cooldown)>clock:return {'state':'COOLDOWN'}
    isin=info['isin'];meta=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin')
    dates=set()
    for base in ('5m','15m'):
        report=store.setting(f'data_quality:{isin}:{isin}:{base}',{})
        if report.get('identity')!=meta:continue
        if report.get('state') not in ('UNCONFIRMED','GAP','INVALID'):continue
        for a,b in report.get('ranges',[]):
            dates.update(d.strftime('%Y%m%d') for d in pd.date_range(pd.Timestamp(a).normalize(),pd.Timestamp(b).normalize(),freq='D'))
    progress=store.setting('history_evidence_progress:'+isin,{})
    # Existing shared acquisition queue also owns contradictory publications.
    # A healthy OHLCV envelope must not hide an incomplete source file.
    dates.update(date for date,state in progress.items() if state.get('unconfirmed_files'))
    cache=store.directory/'gettex_files';cache.mkdir(exist_ok=True)
    today=clock.tz_convert('Europe/Berlin').strftime('%Y%m%d')
    for date in sorted(dates,key=lambda d:(d!=today,d)):
        day=pd.to_datetime(date,format='%Y%m%d')
        if date>today:continue
        try:
            sessions=schedule(meta,day,day)
            if sessions.empty:continue
            r=sessions.iloc[0]
            # Today's completed publication prefix, with the existing delay
            # allowance. Never request a future or still-forming interval.
            closing=min(r.close,(clock-pd.Timedelta(minutes=15)).floor('15min'))
            if closing<=r.open:continue
            required=[f"posttrade.{t.strftime('%Y%m%d.%H.%M')}.{v}.csv.gz" for t in pd.date_range(r.open+pd.Timedelta(minutes=15),closing,freq='15min') for v in ('munc','mund')]
        except (ValueError,KeyError):continue
        evidence=store.setting('history_evidence_progress:'+isin,{}).get(date,{})
        rejected=set(evidence.get('unconfirmed_files',{})) if evidence.get('evidence_policy') in (2,3) else set()
        missing=[name for name in required if not (cache/name).exists() or name in rejected]
        if not missing:continue
        key='candlepilot_acquisition:'+date
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            old=json.loads(old[0]) if old else {}
            # One recheck after fixing the validator, still behind the shared
            # source cooldown above. Never bypass provider 429/transport waits.
            corrected_validator=(old.get('validation_policy',0)<2 and old.get('state')=='UNCONFIRMED'
                and old.get('reason','').startswith('ValueError: INVALID_REPLACEMENT_TIME_OR_VALUE:'))
            if not corrected_validator and old.get('retry_after') and pd.Timestamp(old['retry_after'])>clock:continue
            lease={'state':'RUNNING','date':date,'checked_at':clock.isoformat(),'retry_after':(clock+pd.Timedelta(minutes=30)).isoformat(),'attempts':old.get('attempts',0)+1,'validation_policy':2,'file_cursor':old.get('file_cursor')}
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(lease)))
        # A missing individual file must not permanently hide later slices.
        # Resume in the stable session order, across restarts, within the same
        # request budget and provider cooldown. 404 is never absence evidence.
        cursor=old.get('file_cursor')
        if cursor in required:
            position=required.index(cursor)
            missing=[n for n in missing if required.index(n)>position]+[n for n in missing if required.index(n)<=position]
        result={**lease,'downloaded':0,'unchanged_files':0,'missing_before':len(missing),'file_cursor':cursor,'unavailable_files':[]}
        try:
            # Only derive locations from an actually published official URL.
            html=http(PAGE).decode('utf8','ignore')
            locations=published_files(html)
            templates=[urlparse(location) for location in locations.values()]
            if not templates:raise ValueError('NO_PUBLISHED_OFFICIAL_FILE_TEMPLATE')
            u=templates[0]
            for name in missing[:max(1,min(8,budget))]:
                url=locations.get(name) or u._replace(path=u.path.rsplit('/',1)[0]+'/'+name,query='',fragment='').geturl()
                target=cache/name
                original=target.read_bytes() if target.exists() else None
                try:raw=http(url)
                except HTTPError as error:
                    if error.code!=404:raise
                    result['file_cursor']=name
                    result['unavailable_files'].append(name)
                    continue
                content=gzip.decompress(raw)
                if original is not None:
                    # Trade IDs are not published by this endpoint. Automatic
                    # replacement may add proven rows, but must not silently
                    # merge cancellations/changed trades with the old ledger.
                    from collections import Counter
                    try:
                        old_lines=gzip.decompress(original).decode('utf-8-sig').splitlines()
                        valid_old=bool(old_lines) and all(len(line.split(','))==5 and re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d',line.split(',')[0]) for line in old_lines)
                    except (OSError,EOFError,UnicodeError):valid_old=False
                    lines=_replacement_lines(content,name,old_lines if valid_old else ())
                    if valid_old and Counter(old_lines)-Counter(lines):
                        raise ValueError('REQUIRES_TRADE_CORRECTION_ID: '+name)
                with LOCK:
                    current=target.read_bytes() if target.exists() else None
                    if current!=original:raise ValueError('Evidence file changed concurrently: '+name)
                    if original is None or (name in rejected and raw!=original):
                        if original is not None:
                            backup=cache/'rejected_files';backup.mkdir(exist_ok=True)
                            preserved=backup/(name+'.'+hashlib.sha256(original).hexdigest()[:16]+'.original')
                            if not preserved.exists():preserved.write_bytes(original)
                        temporary=cache/(name+'.part');temporary.write_bytes(raw);temporary.replace(target)
                        if original is not None:result['replaced_invalid_files']=result.get('replaced_invalid_files',0)+1
                    elif raw==original:result['unchanged_files']+=1
                result['downloaded']+=1
                result['file_cursor']=name
            result.update(state='UNCHANGED_UNCONFIRMED' if result['unchanged_files']==result['downloaded'] else 'FILES_ACQUIRED',
                          retry_after=(clock+(pd.Timedelta(days=1) if result['unchanged_files']==result['downloaded'] else pd.Timedelta(minutes=15))).isoformat())
            if result['unavailable_files'] and not result['downloaded']:
                result.update(state='UNCONFIRMED',reason='HTTPError: HTTP Error 404: Not Found; bounded file rotation will resume')
        except Exception as error:
            result.update(state='UNCONFIRMED',reason=type(error).__name__+': '+str(error),retry_after=(clock+pd.Timedelta(hours=24)).isoformat())
            if isinstance(error,HTTPError) and error.code==429:
                # Shared across dates/ISINs: never walk to another day on 429.
                from email.utils import parsedate_to_datetime
                retry=error.headers.get('Retry-After') if error.headers else None
                if retry:
                    try:due=clock+pd.Timedelta(seconds=float(retry))
                    except ValueError:
                        try:due=pd.Timestamp(parsedate_to_datetime(retry))
                        except (ValueError,TypeError):due=clock
                    result['retry_after']=max(pd.Timestamp(result['retry_after']),due).isoformat()
                store.set_setting('candlepilot_source_cooldown',result['retry_after'])
        # One bounded historical batch per source interval, not per ticker.
        # A 54-stock refresh must not multiply historical requests by54.
        cooldown=store.setting('candlepilot_source_cooldown',None)
        source_due=clock+pd.Timedelta(minutes=15)
        if cooldown:source_due=max(source_due,pd.Timestamp(cooldown))
        store.set_setting('candlepilot_source_cooldown',source_due.isoformat())
        store.set_setting(key,result)
        return result
    return {'state':'IDLE'}


def maintain_evidence(info,store,http,parse,now=None):
    """One existing history turn: discover first, then its bounded acquisition.

    Previously an unknown partial file waited another full universe rotation
    because acquisition ran before its evidence had been classified. Fresh
    quote slots keep their current read-only-of-cache classification path.
    """
    # Local verified files must not advance by only one pair per slow network
    # turn. Yield a bounded batch on this same writer before provider waits.
    accumulated=accumulate_cached_batch(store,parse,now)
    classified=classify_cached_session(info,store,parse,now)
    acquired=acquire_missing_evidence(info,store,http,now)
    return {'classification':classified,'acquisition':acquired,'cached_history':accumulated}


def accumulate_cached_batch(store,parse,now=None,max_pairs=16,max_seconds=5):
    """Drain saved pairs fairly on the existing history lane, without HTTP.

    Each pair retains its original atomic checkpoint and all validation. The
    budget is checked between pairs: never cancel a SQLite writer mid-pair.
    Unchanged, rejected and complete pairs retain their existing skip policy.
    """
    started=time.monotonic();limit=max(1,min(16,int(max_pairs)))
    seconds=max(.01,min(5.,float(max_seconds)));results=[];stop='PAIR_BUDGET'
    for _ in range(limit):
        if results and time.monotonic()-started>=seconds:stop='TIME_BUDGET';break
        result=accumulate_cached_pair(store,parse,now)
        if result.get('state') in ('IDLE','NOT_ENABLED','INPUT_CHANGED'):
            stop=result['state'];break
        results.append(result)
    summary=dict(state='IDLE' if not results and stop=='IDLE' else
                 stop if stop in ('NOT_ENABLED','INPUT_CHANGED') else 'YIELDED',
                 stop_reason=stop,pairs_processed=len(results),
                 candles_added=sum(r.get('added',0) for r in results),
                 conflicts=sum(r.get('conflicts',0) for r in results),
                 blocked_pairs=sum(r.get('state')=='BLOCKED' for r in results),
                 pairs=[r.get('pair') for r in results],
                 seconds=round(time.monotonic()-started,4),pair_limit=limit,
                 seconds_budget=seconds,checked_at=pd.Timestamp(now or pd.Timestamp.now(tz='UTC')).isoformat())
    if results:
        progress=store.setting('gettex_cached_history',{})
        progress['last_batch']=summary
        store.set_setting('gettex_cached_history',progress)
    return summary


def _empty_closed_pair(files,anchor,signature):
    """Calendar proof for empty equity files outside a confirmed session only."""
    import hashlib
    from timeframes import schedule
    begin=anchor-pd.Timedelta(minutes=15);meta=dict(source='gettex',venue='GETTEX',timezone='Europe/Berlin')
    try:
        sessions=schedule(meta,begin.tz_convert('Europe/Berlin').tz_localize(None).normalize(),
                          anchor.tz_convert('Europe/Berlin').tz_localize(None).normalize())
        if ((sessions.open<anchor)&(sessions.close>begin)).any():return None
        with LOCK:
            payloads={mic:p.read_bytes() for mic,p in files.items()}
            if any(gzip.decompress(raw).strip() for raw in payloads.values()):return None
            if signature!={mic:[p.stat().st_size,p.stat().st_mtime_ns] for mic,p in files.items()}:return None
        return {mic:hashlib.sha256(raw).hexdigest() for mic,raw in payloads.items()}
    except (ValueError,KeyError,OSError,EOFError,UnicodeError):return None


def accumulate_cached_pair(store,parse,now=None):
    """One closed official file pair on the existing history lane, no HTTP.

    Shared by the registered stocks: new instruments revisit unchanged files
    without redownloading them. Only missing candles are inserted; conflicting
    existing values stay intact for normal evidence/recovery processing.
    """
    import hashlib,json,math
    from eur_listings import OWNER_HISTORY_POLICY
    from paper_account import instrument_allowed
    import data_quality as quality
    policy=store.setting(OWNER_HISTORY_POLICY,{})
    if not (policy.get('version')==1 and policy.get('approved') is True
            and policy.get('exclusive_venue')=='GETTEX' and policy.get('scope')=='registered_stocks'
            and policy.get('accumulate_cached_history') is True):return {'state':'NOT_ENABLED'}
    infos={i['isin']:i for i in store.instruments() if instrument_allowed(i)}
    universe=hashlib.sha256(json.dumps(sorted(infos)).encode()).hexdigest()
    clock=pd.Timestamp(now or pd.Timestamp.now(tz='UTC'));pairs={}
    for path in (store.directory/'gettex_files').glob('posttrade.*.csv.gz'):
        match=re.fullmatch(r'posttrade\.(\d{8}\.\d{2}\.\d{2})\.(mund|munc)\.csv\.gz',path.name)
        if match:pairs.setdefault(match[1],{})[match[2]]=path
    progress=store.setting('gettex_cached_history',{});done=progress.get('pairs',{})
    for label,files in sorted(pairs.items()):
        anchor=pd.to_datetime(label,format='%Y%m%d.%H.%M',utc=True)
        if set(files)!=set(('munc','mund')) or anchor>clock:continue
        signature={mic:[p.stat().st_size,p.stat().st_mtime_ns] for mic,p in files.items()}
        previous=done.get(label,{})
        recheck_empty=(previous.get('state')=='BLOCKED' and previous.get('reason')=='EMPTY_FILE_UNCONFIRMED'
                       and previous.get('empty_calendar_policy',0)<1)
        if previous.get('signature')==signature and previous.get('universe')==universe and not recheck_empty:continue
        hashes={};rows={};errors={};boundary_rows=0
        try:
            with LOCK:
                for mic,path in sorted(files.items()):
                    payload=path.read_bytes();hashes[mic]=hashlib.sha256(payload).hexdigest()
                    lines=gzip.decompress(payload).decode('utf-8-sig').splitlines()
                    if not lines:raise ValueError('EMPTY_FILE_UNCONFIRMED')
                    for line in lines:
                        f=line.split(',')
                        if len(f)!=5 or not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d',f[0]):raise ValueError('UNCONFIRMED_SOURCE_FORMAT')
                        isin=f[0]
                        if isin not in infos or f[2]!='EUR':continue
                        try:valid=math.isfinite(float(f[3])) and float(f[3])>0 and math.isfinite(float(f[4])) and float(f[4])>=0
                        except ValueError:valid=False
                        if not valid:errors[isin]='INVALID_TRADE_VALUE';continue
                        rows.setdefault(isin,[]).append(line)
                if signature!={mic:[p.stat().st_size,p.stat().st_mtime_ns] for mic,p in files.items()}:return {'state':'INPUT_CHANGED'}
            if previous.get('sha256') and previous['sha256']!=hashes:
                raise ValueError('CHANGED_SOURCE_FILE_REQUIRES_EXISTING_RECOVERY')
            candles=[];trades=[];lo=(anchor-pd.Timedelta(minutes=15)).tz_convert('Europe/Berlin').tz_localize(None);hi=anchor.tz_convert('Europe/Berlin').tz_localize(None)
            for isin,lines in rows.items():
                if isin in errors:continue
                frame=parse(('\n'.join(lines)+'\n').encode(),isin,file_time=anchor)
                if len(frame)!=len(lines) or not ((frame.index>=lo)&(frame.index<=hi)).all():errors[isin]='INVALID_TRADE_TIMESTAMP';continue
                # Match existing trade-table identity; never duplicate a replay.
                frame=frame.reset_index(names='at').drop_duplicates().set_index('at').sort_index()
                boundary_rows+=int((frame.index==hi).sum());frame=frame.loc[frame.index<hi]
                if frame.empty:continue
                pending=[]
                for base,freq in (('5m','5min'),('15m','15min')):
                    bars=frame.price.resample(freq).ohlc().join(frame.volume.resample(freq).sum().rename('volume')).dropna()
                    bars.attrs=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin',timeframe=base)
                    audit=quality.audit(bars,base,now=clock,check_stale=False)
                    if audit['state']=='INVALID':errors[isin]='INVALID_OHLCV_OR_SESSION';break
                    pending.extend((isin,'gettex',base,str(t),*(float(row[f]) for f in quality.FIELDS)) for t,row in bars.iterrows())
                if isin in errors:continue
                candles.extend(pending);trades.extend((isin,str(t),float(row.price),float(row.volume)) for t,row in frame.iterrows())
            added=conflicts=0
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute('SELECT value FROM settings WHERE key=?',(OWNER_HISTORY_POLICY,)).fetchone()
                stocks=[json.loads(v) for v, in db.execute('SELECT payload FROM instruments')]
                current_universe=hashlib.sha256(json.dumps(sorted(i['isin'] for i in stocks if instrument_allowed(i))).encode()).hexdigest()
                if not current or json.loads(current[0])!=policy or current_universe!=universe:return {'state':'INPUT_CHANGED'}
                # A conflicting slice must not enter the trade ledger either:
                # the ordinary feed later rebuilds candles from that ledger.
                # Preserve both layers, not just today's candle INSERT.
                rejected=set()
                for isin in {r[0] for r in trades}:
                    incoming={r[1:] for r in trades if r[0]==isin}
                    retained=set(db.execute('SELECT ts,price,volume FROM trades WHERE isin=? AND ts>=? AND ts<?',(isin,str(lo),str(hi))))
                    if retained-incoming:
                        rejected.add(isin);errors[isin]='EXISTING_TRADE_COVERAGE_DIFFERS'
                for row in candles:
                    old=db.execute('SELECT open,high,low,close,volume FROM candles WHERE isin=? AND source=? AND base_tf=? AND ts=?',row[:4]).fetchone()
                    if old is not None and tuple(old)!=row[4:]:
                        conflicts+=1;rejected.add(row[0])
                db.executemany('INSERT OR IGNORE INTO trades(isin,ts,price,volume) VALUES (?,?,?,?)',[r for r in trades if r[0] not in rejected])
                for row in candles:
                    if row[0] in rejected:continue
                    old=db.execute('SELECT open,high,low,close,volume FROM candles WHERE isin=? AND source=? AND base_tf=? AND ts=?',row[:4]).fetchone()
                    if old is None:
                        db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',row);added+=1
                receipt=dict(signature=signature,sha256=hashes,universe=universe,checked_at=clock.isoformat(),
                    added=added,existing_value_conflicts=conflicts,errors=errors,endpoint_rows_deferred=boundary_rows,
                    state='REVIEW_REQUIRED' if errors or conflicts else 'ACCUMULATED',
                    evidence='Official MUNC/MUND pair; missing rows only. Not proof of NO_TRADES or trade readiness.')
                done[label]=receipt
                progress.update(pairs=done,last_pair=label,last_result=receipt,checked_at=clock.isoformat(),
                    registered_stocks=len(infos),available_pairs=sum(len(v)==2 for v in pairs.values()),
                    current_universe_pairs=sum(v.get('universe')==universe for v in done.values()),
                    candles_added_total=progress.get('candles_added_total',0)+added)
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('gettex_cached_history',json.dumps(progress)))
            return dict(state=receipt['state'],pair=label,added=added,conflicts=conflicts,errors=errors)
        except (OSError,EOFError,UnicodeError,ValueError) as error:
            closed_hashes=_empty_closed_pair(files,anchor,signature) if str(error)=='EMPTY_FILE_UNCONFIRMED' else None
            done[label]=dict(signature=signature,sha256=previous.get('sha256') or hashes,universe=universe,
                state='MARKET_CLOSED' if closed_hashes else 'BLOCKED',reason='Confirmed GETTEX calendar; both files empty outside equity session' if closed_hashes else str(error),
                checked_at=clock.isoformat(),empty_calendar_policy=1)
            if closed_hashes:done[label]['sha256']=closed_hashes
            progress.update(pairs=done,last_pair=label,last_result=done[label],checked_at=clock.isoformat(),
                registered_stocks=len(infos),available_pairs=sum(len(v)==2 for v in pairs.values()),
                current_universe_pairs=sum(v.get('universe')==universe for v in done.values()))
            store.set_setting('gettex_cached_history',progress)
            return {'state':done[label]['state'],'pair':label,'reason':done[label]['reason']}
    return {'state':'IDLE','checked_pairs':len(done),'registered_stocks':len(infos)}
