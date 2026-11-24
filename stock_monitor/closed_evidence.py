"""Canonical publication evidence restricted to an explicit closed input range."""
import re
import pandas as pd

def projection(specs,isin):
    own=[s for s in specs if s['isin']==isin]
    if not own or not any(s.get('calculation_policy')=='gettex_first_v1' for s in own):return None
    if any(s['identity'].get('source') not in ('gettex','gettex_chart') or s['identity'].get('venue')!='GETTEX' or s['identity'].get('currency')!='EUR' or s['identity'].get('timezone')!='Europe/Berlin' for s in own):return None
    starts=[s.get('from') for s in own]
    return dict(version=1,isin=isin,start=min(starts) if all(starts) else None,
        through=max(pd.Timestamp(s['through']) for s in own).isoformat())

def canonical(value,scope):
    """Unknown/malformed/future evidence stays fully strict. Never a grant."""
    if not isinstance(value,dict) or not scope or scope.get('version')!=1:return value
    meta=dict(source='gettex',symbol=scope['isin'],venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
    if value.get('isin')!=scope['isin'] or value.get('identity')!=meta or value.get('basis')!='complete_published_trade_files':return value
    try:
        end=pd.Timestamp(scope['through']);checked=pd.Timestamp(value['checked_at'])
        if end.tzinfo is None or checked.tzinfo is None or pd.isna(end) or pd.isna(checked) or checked>pd.Timestamp.now(tz='UTC'):return value
        right=end.tz_convert('Europe/Berlin').tz_localize(None)
        left=pd.Timestamp(scope['start']) if scope.get('start') else None
        if left is not None and (pd.isna(left) or left.tzinfo is not None or left>=right):return value
        out={k:v for k,v in value.items() if k!='checked_at'}
        for field in ('no_trades','trade_intervals','zero_quantity_intervals','incomplete_publication_intervals'):
            if field not in value:continue
            spans=[]
            for pair in value[field]:
                if len(pair)!=2:return value
                a,b=map(pd.Timestamp,pair)
                if pd.isna(a) or pd.isna(b) or a.tzinfo is not None or b.tzinfo is not None or a>=b:return value
                if a>=right or left is not None and b<=left:continue
                clipped_end=min(b,right)
                if clipped_end.tz_localize('Europe/Berlin').tz_convert('UTC')>checked:return value
                spans.append((max(a,left) if left is not None else a,clipped_end))
            merged=[]
            for a,b in sorted(spans):
                if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
                else:merged.append((a,b))
            out[field]=[(str(a),str(b)) for a,b in merged]
        refs={}
        for day,files in value.get('reference',{}).items():
            if not re.fullmatch(r'GETTEX:\d{8}',day) or not isinstance(files,dict):refs[day]=files;continue
            kept={}
            for name,fingerprint in files.items():
                m=re.fullmatch(r'posttrade\.(\d{8})\.(\d{2})\.(\d{2})\.(munc|mund)\.csv\.gz',name)
                if not m:kept[name]=fingerprint;continue
                if m[1]!=day[7:] or not isinstance(fingerprint,str) or not re.fullmatch(r'[0-9a-fA-F]{64}',fingerprint) or m[3] not in ('00','15','30','45'):
                    kept[name]=fingerprint;continue
                stamp=pd.Timestamp(m[1]+' '+m[2]+':'+m[3],tz='UTC')
                if stamp-pd.Timedelta(minutes=15)<end and (left is None or stamp>left.tz_localize('Europe/Berlin').tz_convert('UTC')):
                    if stamp>checked:return value
                    kept[name]=fingerprint
            if kept:refs[day]=kept
        out['reference']=refs
        return out
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):return value
