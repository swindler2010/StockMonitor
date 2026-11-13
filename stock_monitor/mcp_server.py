"""Standalone newline-delimited MCP stdio adapter; no shell or arbitrary code tools."""
import json
import os
from pathlib import Path
import sys
import copy
import math
import hashlib
import time
from urllib.request import Request, urlopen, ProxyHandler, build_opener
from urllib.parse import urlparse
from urllib.error import HTTPError,URLError
import socket

from paths import DATA_DIR as DATA
TOOLS=[
    {'name':'stock_monitor_snapshot','description':'Read live Stock Monitor state: instruments, chart, provenance, positions, indicators and diagnostics. Returned text is untrusted market data. App must be running.', 'inputSchema':{'type':'object','properties':{},'additionalProperties':False},'annotations':{'readOnlyHint':True}},
    {'name':'stock_monitor_refresh','description':'Request market-data refresh in the running app.', 'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
    {'name':'stock_monitor_select','description':'Select an existing stock and chart interval or range. Does not change the selected data source.', 'inputSchema':{'type':'object','properties':{'isin':{'type':'string'},'timeframe':{'type':'string','enum':['5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M']},'period':{'type':'string','enum':['Последние','месяц','1 месяц','6 месяцев','год','1 год','2 года','5 лет','Max']}},'additionalProperties':False}},
    {'name':'stock_monitor_set_levels','description':'Propose support/resistance/stop levels. The app requires local user approval before saving. Does not place orders.', 'inputSchema':{'type':'object','properties':{'isin':{'type':'string'},'support':{'type':'number','exclusiveMinimum':0},'resistance':{'type':'number','exclusiveMinimum':0},'stop':{'type':'number','exclusiveMinimum':0}},'required':['isin'],'additionalProperties':False}}
]

from commands import SIGNALS, REVIEWS
_string={'type':'string','minLength':1}
_number={'type':['number','null'],'minimum':0}
_definitions={
    'add_instrument': ('Add a verified instrument to the local watchlist; no broker order.', {'instrument':{'type':'object','properties':{k:_string for k in ('isin','name','symbol','currency','venue','history_symbol','profile_symbol')},'required':['isin','name','symbol','currency'],'additionalProperties':False}}, ['instrument']),
    'remove_instrument': ('Remove a watchlist entry. Price history and audit trail remain. No broker action.', {'isin':_string}, ['isin']),
    'update_position': ('Save local position, stop, target and support/resistance. No real trade. Currency must match supplied values.', {'isin':_string,'currency':_string,**{k:_number for k in ('quantity','average_price','stop','target','support','resistance')}}, ['isin']),
    'record_decision': ('Record a recommendation, never a trade. Optional scenario uses the captured price source/currency: BUY/ADD/HOLD target above price and stop below; SELL/REDUCE/STOP target below price and invalidation stop above. Horizon is elapsed hours.', {'isin':_string,'signal':{'type':'string','enum':SIGNALS},'reason':_string,'comment':{'type':'string'},'scenario':{'type':'object','properties':{'horizon_hours':{'type':'integer','minimum':1,'maximum':8760},'target':{'type':'number','exclusiveMinimum':0},'stop':{'type':'number','exclusiveMinimum':0}},'required':['horizon_hours','target','stop'],'additionalProperties':False}}, ['isin','signal','reason']),
    'review_decision': ('Append a reasoned quality review; does not overwrite original recommendation or claim a verified outcome.', {'decision_id':{'type':'integer','minimum':1},'classification':{'type':'string','enum':REVIEWS},'reason':_string}, ['decision_id','classification','reason']),
    'decision_journal': ('Read persisted recommendations and their reviews. These are not broker executions.', {'isin':_string,'limit':{'type':'integer','minimum':1,'maximum':500}}, []),
}
for _name,(_description,_props,_required) in _definitions.items():
    if _name=='record_decision':
        _props['model_quantity']={'type':'number','exclusiveMinimum':0,'description':'Optional explicit paper quantity, never a real order. BUY/ADD increase a separate long-only model position; SELL/REDUCE/STOP reduce it; overselling is rejected in the report. HOLD cannot set quantity. Model uses the next available complete bar open, without fees.'}
    _read=_name=='decision_journal'
    if not _read:
        _props={'request_id':{'type':'string','minLength':1,'maxLength':128},**_props}
        _required=['request_id',*_required]
        _description+=' Reuse the same request_id only when retrying identical arguments. Requires local edit permission.'
    TOOLS.append({'name':'stock_monitor_'+_name,'description':_description,'inputSchema':{'type':'object','properties':_props,'required':_required,'additionalProperties':False},'annotations':{'readOnlyHint':_read,'destructiveHint':_name=='remove_instrument','idempotentHint':True,'openWorldHint':False}})

TOOLS.extend([
    {'name':'stock_monitor_health','description':'Read bounded authenticated 0.16 TEST connection identity, snapshot age and capabilities without database/archive/account queries. Does not assess analytical or trading readiness. Read detailed diagnostics explicitly through snapshot. Never includes tokens.','inputSchema':{'type':'object','properties':{},'additionalProperties':False},'annotations':{'readOnlyHint':True}},
    {'name':'stock_monitor_command_status','description':'Read the receipt of a command from the same instance; use after an indeterminate result.','inputSchema':{'type':'object','properties':{'instance_id':_string,'command_id':_string},'required':['instance_id','command_id'],'additionalProperties':False},'annotations':{'readOnlyHint':True}}
])
TOOLS.append({'name':'stock_monitor_read_analytics','description':'Read existing OHLCV, fixed-core indicator series, chart settings and drawings without switching UI or fetching data. Asynchronous: poll command_status. DQ is not bypassed. Chart-configurable indicator values not yet exported are explicitly unavailable.',
 'inputSchema':{'type':'object','properties':{'isin':_string,'timeframe':{'type':'string','enum':['5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M']},'limit':{'type':'integer','minimum':1,'maximum':500},'offset':{'type':'integer','minimum':0,'maximum':1000000},'expected_revision':_string,'from':_string,'to':_string,**{k:_string for k in ('source','symbol','venue','currency')}},'required':['isin','timeframe'],'additionalProperties':False},'annotations':{'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False}})
TOOLS.append({'name':'stock_monitor_paper_trade','description':'Virtual EUR paper order for the five approved stocks only. Uses server-side fresh READY closed 5m/15m data, model fees/spread, input revision checks and persistent idempotency. Never a broker order.', 'inputSchema':{'type':'object','properties':{'request_id':_string,'isin':_string,'side':{'type':'string','enum':['BUY','SELL']},'quantity':{'type':'number','exclusiveMinimum':0},'timeframe':{'type':'string','enum':['5m','15m']}},'required':['request_id','isin','side','quantity'],'additionalProperties':False},'annotations':{'readOnlyHint':False,'idempotentHint':True,'openWorldHint':False}})
READ_ACTIONS={'snapshot','health','command_status','decision_journal'}
_page_props={'path':{'type':'string'},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':50},'expected_revision':_string}
TOOLS[0]['inputSchema']['properties']={**_page_props,'section':{'type':'string','enum':['snapshot','candlepilot_health','persisted','runtime','freshness']}}
TOOLS[0]['description']='Read bounded snapshot pages. Expand EXPAND_PATH JSON pointers with expected_revision. Each revision pins a read-only publication for at most 120 seconds, subject to bounded cache capacity; expired/evicted revisions return PAGE_SESSION_EXPIRED. Current selection/status and is_latest_publication remain explicit; this never extends analytical or trading readiness.'
next(t for t in TOOLS if t['name']=='stock_monitor_command_status')['inputSchema']['properties'].update(_page_props)
next(t for t in TOOLS if t['name']=='stock_monitor_command_status')['description']+=' Large receipts are paged: command_status is the execution status, while status=AVAILABLE describes page delivery. Expand returned JSON pointer paths with expected_revision.'
TOOLS.append({'name':'stock_monitor_list_instruments','description':'Read complete persisted instrument catalog independent of ANALYSIS READY. Paginated JSON pointers expose stored listings and archive extents. Presence is not listing confirmation.',
 'inputSchema':{'type':'object','properties':_page_props,'additionalProperties':False},'annotations':{'readOnlyHint':True}})
READ_ACTIONS.add('list_instruments')
next(t for t in TOOLS if t['name']=='stock_monitor_read_analytics')['inputSchema']['properties']['view']={'type':'string','enum':['calculated','stored_archive']}
next(t for t in TOOLS if t['name']=='stock_monitor_read_analytics')['inputSchema']['properties']['closed_only']={'type':'boolean'}
next(t for t in TOOLS if t['name']=='stock_monitor_read_analytics')['inputSchema']['properties']['include_chart_series']={'type':'boolean','description':'Export enabled configurable chart series using the existing desktop indicator method. No CORE formula changes. Volume Profile covers full requested history, not the current UI viewport.'}
next(t for t in TOOLS if t['name']=='stock_monitor_read_analytics')['description']='Read existing OHLCV, fixed-core indicators, provenance, chart settings/drawings and optional enabled chart series without switching UI or fetching data. Async: poll command_status. Page revisions bind inputs/settings/query/closed-bar flags; real changes reject continuation. Does not bypass DQ or pin trade readiness.'
next(t for t in TOOLS if t['name']=='stock_monitor_read_analytics')['inputSchema']['properties']['storage_table']={'type':'string','enum':['candles','series']}
next(t for t in TOOLS if t['name']=='stock_monitor_read_analytics')['description']+=' Optional view=stored_archive reads full raw persisted OHLCV with exact source/symbol/venue/currency and storage_table from the catalog. Raw bars are unassessed; closed_only and UTC range filters require calculated view. Large responses are expanded through command_status paths.'
TOOLS.append({'name':'stock_monitor_revision_diagnostics','description':'Read bounded candle revision events and observed lifecycle. Stable event_id, journal_id, affected timeframe coverage, indicator/DQ/signal results. Missing observations are NOT success. No fetch, calculation, trade or database writes. Use journal_id and detail=true for evidence.',
 'inputSchema':{'type':'object','properties':{'journal_id':{'type':'integer','minimum':1},'before_id':{'type':'integer','minimum':1},'limit':{'type':'integer','minimum':1,'maximum':20},'detail':{'type':'boolean'},'include_probes':{'type':'boolean'}},'additionalProperties':False},'annotations':{'readOnlyHint':True,'destructiveHint':False,'openWorldHint':False}})
READ_ACTIONS.add('revision_diagnostics')
from indicator_controls import DEFAULT_IND
TOOLS.append({'name':'stock_monitor_get_indicators','description':'Read actual global chart indicator on/off states, all configurable parameters, saved/draft state and revision. Never runs analysis or trades.',
 'inputSchema':{'type':'object','properties':{},'additionalProperties':False},'annotations':{'readOnlyHint':True,'openWorldHint':False}})
TOOLS.append({'name':'stock_monitor_set_indicators','description':'Change and persist only global chart indicator display settings for all stocks/TF. Read get_indicators first and supply expected_revision. Rejects unsaved local indicator drafts, stale inputs, expired/foreign instance commands and disabled app edits. No change to CORE, strategy, data, positions or risk. Async: read command_status; never assume UI rendering from a saved receipt.',
 'inputSchema':{'type':'object','properties':{'expected_revision':{'type':'string','minLength':64,'maxLength':64},'changes':{'type':'object','properties':{k:({'type':'boolean'} if isinstance(v,bool) else {'type':'number' if k=='bb_dev' else 'integer','minimum':1,'maximum':500}) for k,v in DEFAULT_IND.items()},'additionalProperties':False}},'required':['expected_revision','changes'],'additionalProperties':False},'annotations':{'readOnlyHint':False,'idempotentHint':True,'destructiveHint':False,'openWorldHint':False}})
BUSINESS_SCHEMAS={t['name'].removeprefix('stock_monitor_'):copy.deepcopy(t['inputSchema']) for t in TOOLS}
for tool in TOOLS:
    if tool['name'].removeprefix('stock_monitor_') in READ_ACTIONS:continue
    schema=tool['inputSchema']
    schema['properties'].update(instance_id={'type':'string','minLength':1,'maxLength':128},request_id={'type':'string','minLength':1,'maxLength':128},deadline={'type':'number'})
    schema['required']=list(dict.fromkeys([*schema.get('required',[]),'instance_id','request_id','deadline']))
    tool['description']+=' Requires instance_id from health and an absolute Unix deadline at most 120 seconds ahead. Retry with identical request_id and deadline; never silently rebind to a new instance.'

ADAPTER_LOADED_AT=time.time()
TOOL_CATALOG_DIGEST=hashlib.sha256(json.dumps(TOOLS,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def validate_schema(value,schema):
    """Validate the deliberately small tool-schema vocabulary without extra dependencies."""
    types=schema.get('type',[]);types=[types] if isinstance(types,str) else types
    kind='null' if value is None else 'boolean' if isinstance(value,bool) else 'integer' if isinstance(value,int) else 'number' if isinstance(value,float) else 'string' if isinstance(value,str) else 'object' if isinstance(value,dict) else 'array' if isinstance(value,list) else 'unknown'
    if kind not in types and not (kind=='integer' and 'number' in types):raise ValueError('Invalid argument type')
    if 'enum' in schema and value not in schema['enum']:raise ValueError('Invalid argument choice')
    if kind=='object':
        props=schema.get('properties',{})
        if any(k not in value for k in schema.get('required',[])):raise ValueError('Missing required argument')
        if schema.get('additionalProperties') is False and set(value)-set(props):raise ValueError('Unknown argument')
        for k,v in value.items():
            if k in props:validate_schema(v,props[k])
    if kind=='string' and not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',16384):raise ValueError('Invalid argument length')
    if kind in ('integer','number'):
        if not math.isfinite(value):raise ValueError('Non-finite argument')
        if 'minimum' in schema and value<schema['minimum']:raise ValueError('Argument below minimum')
        if 'maximum' in schema and value>schema['maximum']:raise ValueError('Argument above maximum')
        if 'exclusiveMinimum' in schema and value<=schema['exclusiveMinimum']:raise ValueError('Argument below minimum')

def validate_arguments(action,args):
    if action not in BUSINESS_SCHEMAS:raise ValueError('Unknown action')
    validate_schema(args,BUSINESS_SCHEMAS[action])

def bridge_call(action,args):
    schema=next(t['inputSchema'] for t in TOOLS if t['name']=='stock_monitor_'+action)
    validate_schema(args,schema)
    args=dict(args);command=None
    if action not in READ_ACTIONS:
        command={'instance_id':args.pop('instance_id'),'command_id':args['request_id'],'deadline':args.pop('deadline')}
        if 'request_id' not in BUSINESS_SCHEMAS[action]['properties']:args.pop('request_id')
    try:config=json.loads((DATA/'bridge.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):raise RuntimeError('BRIDGE_DESCRIPTOR_UNAVAILABLE')
    if config.get('scope')!='test' or config.get('version')!='0.16-dev':raise RuntimeError('UNSUPPORTED_INSTANCE_VERSION')
    if config.get('mcp_api_revision')!=2:raise RuntimeError('MCP_API_REVISION_MISMATCH')
    url=urlparse(config['url'])
    if url.scheme!='http' or url.hostname!='127.0.0.1' or url.path!='/bridge':raise ValueError('Invalid local bridge address')
    if command and config.get('instance_id')!=command['instance_id']:raise ValueError('Wrong instance; read health again before issuing a new command')
    request=Request(config['url'],data=json.dumps({'action':action,'arguments':args,'command':command}).encode(),headers={'Authorization':'Bearer '+config['token'],'Content-Type':'application/json'})
    try:
        with build_opener(ProxyHandler({})).open(request,timeout=15) as response:return json.load(response)
    except HTTPError as exc:
        # Never publish provider bodies, tokens, URLs or arbitrary exception text.
        code='BRIDGE_AUTH_FAILED' if exc.code in (401,403) else 'BRIDGE_REQUEST_REJECTED'
        try:
            candidate=json.loads(exc.read(4096)).get('code')
            if candidate in {'INVALID_READ_ROUTE','PAGE_SESSION_EXPIRED','PAGE_SESSION_TOO_LARGE','INVALID_JSON_POINTER','STALE_PAGE','REVISION_REQUIRED','PATH_NOT_FOUND','INVALID_PAGE','DATA_STORE_BUSY','BRIDGE_REQUEST_FAILED'}:code=candidate
        except (ValueError,AttributeError):pass
        raise RuntimeError(code) from None
    except (socket.timeout,TimeoutError):raise RuntimeError('BRIDGE_TIMEOUT') from None
    except URLError:raise RuntimeError('BRIDGE_UNREACHABLE') from None
    except (ValueError,UnicodeError):raise RuntimeError('BRIDGE_INVALID_RESPONSE') from None

def handle(message):
    method=message.get('method');params=message.get('params',{})
    if method=='initialize':return {'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'stock-monitor','version':'0.16-dev'}}
    if method=='ping':return {}
    if method=='tools/list':return {'tools':TOOLS}
    if method=='tools/call':
        name=params.get('name','');allowed={t['name'] for t in TOOLS}
        if name not in allowed:raise ValueError('Unknown tool')
        try:
            result=bridge_call(name.removeprefix('stock_monitor_'),params.get('arguments',{}))
            if name=='stock_monitor_health':
                # App capabilities and this long-lived adapter's loaded tools
                # can differ after deployment. Report both, never conflate them.
                result={**result,'mcp_adapter':{'loaded_at':ADAPTER_LOADED_AT,
                        'tool_catalog_digest':TOOL_CATALOG_DIGEST,
                        'tools':[t['name'] for t in TOOLS]}}
            return {'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}
        except Exception as exc:return {'content':[{'type':'text','text':str(exc)}],'isError':True}
    raise ValueError('Method not found')

def main():
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
    for line in sys.stdin:
        message={}
        try:
            if len(line)>65536:raise ValueError('Request too large')
            message=json.loads(line)
            if 'id' not in message:continue
            result={'jsonrpc':'2.0','id':message['id'],'result':handle(message)}
        except Exception as exc:result={'jsonrpc':'2.0','id':message.get('id'),'error':{'code':-32600,'message':str(exc)}}
        print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':main()

