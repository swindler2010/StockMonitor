"""Seven-day news storage, including translations and UI bookmarks only."""
import hashlib,json,time
from datetime import datetime,timezone

WEEK=7*86400

def stamp(value):
    from news_engine import timestamp
    value=timestamp(value)
    return datetime.fromisoformat(value).timestamp() if value else None

def iso(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()

def article(row,now,*,fallback=None,first=None):
    if not isinstance(row,dict):return None
    first=stamp(first if first is not None else row.get('first_seen_at'))
    if first is None:first=stamp(fallback)
    if first is None or first>now+300:first=now
    published=stamp(row.get('published'))
    basis=published if published is not None and published<=now+300 else first
    if round(now-basis,6)>=WEEK:return None
    return {**row,'first_seen_at':iso(first)}

def digest(rows):
    from news_translation import fingerprint
    return hashlib.sha256(json.dumps(sorted(fingerprint(r) for r in rows)).encode()).hexdigest()

def merge(rows,previous=None,now=None):
    """Keep a week, not just the last RSS page; repeated receipts never renew age."""
    from news_engine import normalize,safe_url
    now=time.time() if now is None else now;previous=previous if isinstance(previous,dict) else {}
    incoming=[r for r in rows if isinstance(r,dict)]
    old=previous.get('articles') or [];old=old if isinstance(old,list) else []
    seen=previous.get('first_seen') or {};seen=seen if isinstance(seen,dict) else {}
    seen=dict(seen);retained={};observed=set()
    fallback=previous.get('fetched_at') or previous.get('attempted_at')
    for row,is_new in [(r,False) for r in old]+[(r,True) for r in incoming]:
        if not isinstance(row,dict) or not (url:=safe_url(row.get('url'))):continue
        key=hashlib.sha256(url.encode()).hexdigest();observed.add(key)
        first=seen.get(key) or row.get('first_seen_at') or (iso(now) if is_new else fallback) or iso(now)
        if stamp(first) is None or stamp(first)>now+300:first=iso(now)
        seen[key]=first
        kept=article(row,now,first=first)
        if kept:retained[url]=kept
        elif is_new:retained.pop(url,None)
    # A compact hash/date receipt is not an article. Keep the first observation
    # even through an empty/failed feed, so an undated item cannot rejuvenate.
    articles=normalize(list(retained.values()),now,maximum=None)
    return articles,seen

def project(packet,now=None):
    if not isinstance(packet,dict):return {}
    now=time.time() if now is None else now
    rows=packet.get('articles') or [];rows=rows if isinstance(rows,list) else []
    fallback=packet.get('fetched_at') or packet.get('attempted_at')
    kept=[v for row in rows if (v:=article(row,now,fallback=fallback))]
    return {**packet,'articles':kept,'retention_days':7,'content_digest':digest(kept)}

def prune_store(store,now=None):
    """One worker transaction; never scan/update candle, account or event tables."""
    from news_translation import fingerprint
    now=time.time() if now is None else now;changed=removed=translations=marks=0
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        raw=db.execute("SELECT key,value FROM settings WHERE key GLOB 'news:*' OR key GLOB 'news_translation:*' OR key='ui_news_collection_v1'").fetchall()
        values={}
        for key,value in raw:
            try:
                parsed=json.loads(value)
                if isinstance(parsed,dict):values[key]=parsed
            except (ValueError,TypeError):pass
        output={};valid={}
        for key,value in values.items():
            if not key.startswith('news:'):continue
            packet=project(value,now);rows=packet['articles'];removed+=len(value.get('articles') or [])-len(rows)
            valid[key[5:]]={fingerprint(r) for r in rows};output[key]=packet
        for key,value in values.items():
            if not key.startswith('news_translation:'):continue
            topic=key.split(':',2)[-1];items=value.get('items') or {}
            if not isinstance(items,dict):continue
            keep={k:v for k,v in items.items() if k in valid.get(topic,set())}
            translations+=len(items)-len(keep);output[key]={**value,'items':keep}
        key='ui_news_collection_v1';collection=values.get(key)
        if collection and isinstance(collection.get('items'),dict):
            keep={}
            for item_id,entry in collection['items'].items():
                item=entry.get('item',{}) if isinstance(entry,dict) else {};row=item.get('article')
                kept=article(row,now)
                if kept:keep[item_id]={**entry,'item':{**item,'article':kept}}
            marks=len(collection['items'])-len(keep);output[key]={**collection,'items':keep}
        for key,value in output.items():
            if value!=values.get(key):
                db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(value,ensure_ascii=False,allow_nan=False),key));changed+=1
    # Technical RSS bodies use their original cache timestamp. Leave host
    # cooldowns and unrelated cache/evidence files untouched.
    directory=getattr(store,'directory',None)
    if directory is not None:
        folder=directory/'provider_cache'/'news-rss'
        for path in folder.glob('*.json'):
            if len(path.stem)!=64 or any(c not in '0123456789abcdef' for c in path.stem):continue
            try:
                value=json.loads(path.read_text(encoding='utf-8'));at=value.get('created_at',value.get('at'))
                if isinstance(at,(int,float)) and now-at>=WEEK and path.resolve().parent==folder.resolve():path.unlink()
            except (OSError,ValueError,TypeError):pass
        folder=directory/'provider_cache'
        for path in folder.glob('news-preview-*.json'):
            suffix=path.stem.removeprefix('news-preview-')
            if len(suffix)!=64 or any(c not in '0123456789abcdef' for c in suffix):continue
            try:
                at=json.loads(path.read_text(encoding='utf-8')).get('at')
                if isinstance(at,(int,float)) and now-at>=WEEK and path.resolve().parent==folder.resolve():
                    path.with_suffix('.png').unlink(missing_ok=True);path.unlink()
            except (OSError,ValueError,TypeError):pass
    return dict(changed=changed,removed=removed,translations=translations,marks=marks,collection=output.get('ui_news_collection_v1'))
