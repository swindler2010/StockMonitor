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
    elif kind=='@persisted':prefix='/@persisted';extra={'section':'persisted'};action='snapshot'
    elif kind=='@freshness':prefix='/@freshness';extra={'section':'freshness'};action='snapshot'
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
    return {'tool':'stock_monitor_snapshot','section':'snapshot','catalog':'/@catalog',
        'persisted':'/@persisted','freshness':'/@freshness','receipt':'/@receipt/{instance_id}/{command_id}',
        'semantics':'Read-only aliases with the same paging, allowlists, instance and stale guards.'}
