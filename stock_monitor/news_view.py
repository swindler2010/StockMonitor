"""Normalize provider news without claiming an unverified issuer association."""
from urllib.parse import urlsplit
from datetime import datetime,timezone


def articles(card, symbol):
    output=[];seen=set()
    if card.get('symbol') and card['symbol']!=symbol:return output
    for entry in card.get('news') or []:
        if not isinstance(entry,dict):continue
        c=entry.get('content',entry)
        if not isinstance(c,dict):continue
        tags=c.get('relatedTickers',entry.get('relatedTickers'))
        explicit=isinstance(tags,list) and all(isinstance(t,str) for t in tags) and bool(tags)
        if explicit and symbol not in tags:continue
        raw=c.get('canonicalUrl')
        url=raw.get('url','') if isinstance(raw,dict) else c.get('link','')
        try:
            parsed=urlsplit(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username:url=''
        except (TypeError,ValueError):url=''
        title=str(c.get('title') or 'Без заголовка')
        key=url or title
        if key in seen:continue
        seen.add(key)
        published=c.get('pubDate') or c.get('providerPublishTime') or ''
        if isinstance(published,(int,float)):
            try:published=datetime.fromtimestamp(published,timezone.utc).isoformat()
            except (OverflowError,OSError,ValueError):published=''
        provider=c.get('provider')
        provider=provider.get('displayName','') if isinstance(provider,dict) else c.get('publisher','')
        output.append({'title':title,'url':url,'published':str(published) or 'Время не указано','provider':provider or 'Издатель не указан',
                       'relevance':'Тикер указан источником' if explicit else 'Выдано источником по тикеру; связь с эмитентом не подтверждена'})
    return output
