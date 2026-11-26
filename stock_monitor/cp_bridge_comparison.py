"""Compare two coherent read views, never CP/DQ cumulative counters.

A physical candle key is (ISIN, source, venue, currency, symbol, TF, stored ts).
Legacy candles other than GETTEX have incomplete listing identity and are kept
in a separate storage namespace. Counts are storage observations, not repairs
attributed to CP or Work, and are never added across TF as economic candles.
"""
import sqlite3
import time


def revision_coverage(db):
    """Recognize the existing complete market triggers without installing them."""
    for table in ('series', 'candles'):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        columns = [r[1] for r in db.execute('PRAGMA table_info('+table+')')]
        same = ' AND '.join('NEW.'+c+' IS '+c for c in columns)
        equal = ' AND '.join('NEW.'+c+' IS OLD.'+c for c in columns)
        def bump(row):
            return "INSERT INTO input_revisions VALUES('market:'||"+row+".isin||':'||"+row+".base_tf,1) ON CONFLICT(scope) DO UPDATE SET revision=revision+1;"
        expected = {
            'insert': f'CREATE TRIGGER input_{table}_insert BEFORE INSERT ON {table} WHEN NOT EXISTS(SELECT 1 FROM {table} WHERE {same}) BEGIN {bump("NEW")} END',
            'delete': f'CREATE TRIGGER input_{table}_delete AFTER DELETE ON {table} BEGIN {bump("OLD")} END',
            'update': f'CREATE TRIGGER input_{table}_update AFTER UPDATE ON {table} WHEN NOT ({equal}) BEGIN {bump("OLD")} {bump("NEW")} END',
        }
        for action, sql in expected.items():
            actual = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (f'input_{table}_{action}',)).fetchone()
            if not actual or actual[0] != sql:
                return False
    return True


def rows(db, tables, isin, tf=None):
    queries=[];params=[]
    if 'series' in tables:
        queries.append("SELECT source,venue,currency,symbol,base_tf,ts,open,high,low,close,volume FROM series WHERE isin=?"+(" AND base_tf=?" if tf is not None else ""))
        params.extend((isin,tf) if tf is not None else (isin,))
    if 'candles' in tables:
        # The EUR GETTEX mapping is the existing Store.load_official contract.
        # Do not invent venue/currency for an old ambiguous CSV/provider row.
        queries.append("SELECT source,CASE WHEN source='gettex' THEN 'GETTEX' ELSE '<legacy>' END,CASE WHEN source='gettex' THEN 'EUR' ELSE '<unknown>' END,CASE WHEN source='gettex' THEN isin ELSE '<legacy>' END,base_tf,ts,open,high,low,close,volume FROM candles WHERE isin=?"+(" AND base_tf=?" if tf is not None else ""))
        params.extend((isin,tf) if tf is not None else (isin,))
    if not queries:return iter(())
    # Both consumers already deduplicate exact keys/values and retain conflicting
    # copies as ambiguous. SQL DISTINCT sorted every OHLCV column unnecessarily.
    return iter(db.execute(' UNION ALL '.join(queries)+' ORDER BY 1,2,3,4,5,6',params))


def unique(stream):
    """Deduplicate identical storage copies; conflicting copies stay ambiguous."""
    last=None;values=None;ambiguous=False
    for row in stream:
        key=tuple(row[:6]);payload=tuple(row[6:])
        if key!=last:
            if last is not None:yield last,values,ambiguous
            last=key;values=payload;ambiguous=False
        elif payload!=values:ambiguous=True
    if last is not None:yield last,values,ambiguous


def compare_instrument(current, previous, isin):
    if getattr(current,'checkpoint',None) is not None:
        from cp_diagnostic_baseline import compare
        return compare(current,previous,isin)
    unavailable=dict(status='UNKNOWN',new_unique_physical_ohlcv=None,corrected_unique_physical_ohlcv=None,removed_unique_physical_ohlcv=None)
    if not previous:return dict(unavailable,reason='NO_PREVIOUS_COMPLETE_SNAPSHOT')
    old=previous.get('view')
    if old is None or old.closed:return dict(unavailable,reason='PREVIOUS_READ_VIEW_EXPIRED')
    if old.instance!=current.instance:return dict(unavailable,reason='INSTANCE_CHANGED')
    if sorted(previous['scope'])!=sorted((i['isin'],i.get('kind','stock')) for i in current.infos):return dict(unavailable,reason='CATALOG_SCOPE_CHANGED')
    if not old.revision_coverage or not current.revision_coverage:return dict(unavailable,reason='MARKET_REVISION_COVERAGE_UNPROVEN')
    # Exact trigger SQL is unchanged in both pinned views. Unchanged market tokens
    # prove that all storage OHLCV, keys and row membership remained unchanged.
    prefix='market:'+isin+':'
    scopes=set(k for k in old.tokens.keys()|current.tokens.keys() if k.startswith(prefix))
    changed=[s[len(prefix):] for s in scopes if old.tokens.get(s)!=current.tokens.get(s)]
    result=dict(status='EXACT',from_revision=old.revision,to_revision=current.revision,new_unique_physical_ohlcv=0,corrected_unique_physical_ohlcv=0,removed_unique_physical_ohlcv=0,ambiguous_physical_keys=0,by_tf={},basis='Existing exact market triggers plus streaming comparison of changed TF between pinned read views; no DQ/event counters.')
    from cp_bridge_diagnostics import budget
    try:
        with old.lock, budget(old.db,2), budget(current.db,2):
            deadline=time.monotonic()+2
            for tf in sorted(changed):
                a=unique(rows(old.db,old.tables,isin,tf));b=unique(rows(current.db,current.tables,isin,tf))
                x=next(a,None);y=next(b,None);counts=dict(new=0,corrected=0,removed=0,ambiguous=0)
                while x is not None or y is not None:
                    if time.monotonic()>deadline:raise TimeoutError()
                    if y is None or (x is not None and x[0]<y[0]):counts['removed']+=not x[2];counts['ambiguous']+=x[2];x=next(a,None)
                    elif x is None or y[0]<x[0]:counts['new']+=not y[2];counts['ambiguous']+=y[2];y=next(b,None)
                    else:
                        if x[2] or y[2]:counts['ambiguous']+=1
                        else:counts['corrected']+=x[1]!=y[1]
                        x=next(a,None);y=next(b,None)
                result['by_tf'][tf]=counts
                for name in ('new','corrected','removed'):result[name+'_unique_physical_ohlcv']+=counts[name]
                result['ambiguous_physical_keys']+=counts['ambiguous']
    except (sqlite3.OperationalError,TimeoutError):return dict(unavailable,reason='COMPARISON_READ_BUDGET',from_revision=old.revision,to_revision=current.revision)
    if result['ambiguous_physical_keys']:result['status']='EXACT_WITH_AMBIGUOUS_KEYS'
    return result


def totals(rows,previous):
    comparisons=[r.get('ohlcv_comparison',{}) for r in rows]
    complete=bool(previous) and all(c.get('status')=='EXACT' for c in comparisons)
    return dict(status='EXACT' if complete else 'UNKNOWN',
        new_unique_physical_ohlcv=sum(c['new_unique_physical_ohlcv'] for c in comparisons) if complete else None,
        corrected_unique_physical_ohlcv=sum(c['corrected_unique_physical_ohlcv'] for c in comparisons) if complete else None,
        removed_unique_physical_ohlcv=sum(c['removed_unique_physical_ohlcv'] for c in comparisons) if complete else None,
        unknown_instruments=sum(c.get('status')!='EXACT' for c in comparisons),
        attribution='Observed storage delta only; not an attribution to CP/Work or a sum of unique economic candles across feeds/TF. Per-instrument by_tf is authoritative.')
