"""Read-only aliases using the existing snapshot schema for older MCP clients."""
def escape(value):
    return str(value).replace('~','~0').replace('/','~1')

def route(action,args):
    if action!='snapshot':return action,args,None
    path=args.get('path','')
    if not path.startswith('/@'):return action,args,None
    # Reserved aliases are explicit; no arbitrary settings or actions are routed.
    if args.get('section','snapshot')!='snapshot':raise ValueError('INVALID_READ_ROUTE')
    parts=path.split('/')
    kind=parts[1]
    if kind=='@receipt':
        if len(parts)<4 or not parts[2] or not parts[3]:raise ValueError('INVALID_READ_ROUTE')
        prefix='/'.join(parts[:4])
        instance=parts[2].replace('~1','/').replace('~0','~')
        command=parts[3].replace('~1','/').replace('~0','~')
        extra=dict(instance_id=instance,command_id=command);action='command_status'
    elif kind=='@catalog':prefix='/@catalog';extra={};action='list_instruments'
    elif kind=='@archives':prefix='/@archives';extra={};action='archive_catalog'
    elif kind=='@summary':prefix='/@summary';extra={};action='read_summary'
    elif kind=='@archive':
        table=parts[2] if len(parts)>2 else ''
        keys=('isin','source','venue','currency','symbol','timeframe') if table=='series' else ('isin','source','timeframe')
        if table not in ('series','candles') or len(parts)!=3+len(keys):raise ValueError('INVALID_ARCHIVE_ROUTE')
        values=[p.replace('~1','/').replace('~0','~') for p in parts[3:]]
        if any(not v or len(v)>256 or any(ord(c)<32 for c in v) for v in values):raise ValueError('INVALID_ARCHIVE_IDENTITY')
        prefix=path;extra={'archive_identity':dict(zip(keys,values),storage_table=table)};action='raw_archive'
    elif kind=='@persisted':prefix='/@persisted';extra={'section':'persisted'};action='snapshot'
    elif kind=='@freshness':prefix='/@freshness';extra={'section':'freshness'};action='snapshot'
    elif kind=='@dq-evidence':
        import re
        if len(parts)<4 or not re.fullmatch('[A-Z0-9]{12}',parts[2]) or not re.fullmatch('[0-9]{1,6}',parts[3]):raise ValueError('INVALID_EVIDENCE_ROUTE')
        prefix='/'.join(parts[:4]);extra={'section':'quality_evidence','evidence_isin':parts[2],'evidence_page':int(parts[3])};action='snapshot'
    else:raise ValueError('INVALID_READ_ROUTE')
    args={k:v for k,v in args.items() if k in ('offset','limit','expected_revision')}
    args.update(extra,path=path[len(prefix):])
    return action,args,prefix

def decorate(result,prefix,instance):
    if not isinstance(result,dict):return result
    # Copy only transport envelopes, never transform original financial values.
    result=dict(result)
    if prefix:
        if 'path' in result:result['path']=prefix+result['path']
        if 'items' in result:
            result['items']=[{**r,'path':prefix+r['path']} if isinstance(r,dict) and 'path' in r else r for r in result['items']]
        result['read_route']={'tool':'stock_monitor_snapshot','section':'snapshot','root':prefix}
    elif result.get('command_id'):
        result['read_route']={'tool':'stock_monitor_snapshot','section':'snapshot',
            'root':'/@receipt/'+escape(instance)+'/'+escape(result['command_id']),
            'hint':'Append returned JSON pointer path; preserve expected_revision. Read only, never resubmits.'}
    return result

def routes():
    return {'tool':'stock_monitor_snapshot','section':'snapshot','catalog':'/@catalog','summary':'/@summary',
        'candlepilot_diagnostics':{'section':'candlepilot_health','start_path':'','instruments_path':'/by_stock','completion_path':'/completion','page_limit':2,'semantics':'Fresh coherent read session; preserve expected_revision; then /completion. Saved evidence dates are not changed. Idle120s/absolute900s, no execution grant.'},
        'short_report':'/@summary/report','by_stock':'/@summary/by_stock',
        'persisted':'/@persisted','freshness':'/@freshness','dq_evidence':'/@dq-evidence/{isin}/{page}',
        'receipt':'/@receipt/{instance_id}/{command_id}',
        'archive_catalog':'/@archives',
        'raw_archive_series':'/@archive/series/{isin}/{source}/{venue}/{currency}/{symbol}/{timeframe}',
        'raw_archive_candles':'/@archive/candles/{isin}/{source}/{timeframe}',
        'raw_archive_semantics':'All saved TF including auxiliary 1m; exact stored OHLCV, no readiness/session gate; JSON-pointer escape each identity component; offset/limit/expected_revision page rows.',
        'semantics':'Read-only aliases with the same paging, allowlists, instance and stale guards.'}
