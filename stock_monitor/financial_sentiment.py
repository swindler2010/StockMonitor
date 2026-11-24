"""Small deterministic financial phrase model. Abstains on ambiguity.

Direction is interpreted with its subject: falling costs differ from falling
profit. No model download, network call, inferred price impact or trading use.
"""
import re
from functools import lru_cache

METHOD = 'FINANCIAL_PHRASES_V2'
GOOD = r'(?:profit\w*|earnings|revenue\w*|sales|turnover|margin\w*|demand|orders|cash flow|прибыл\w*|выручк\w*|продаж\w*|umsatz\w*|gewinn\w*)'
BAD = r'(?:cost\w*|expense\w*|loss\w*|debt|расход\w*|затрат\w*|убыт\w*|долг\w*|kosten|verlust\w*|schulden)'
UP = r'(?:increas\w*|ris(?:e|es|ing)|rose|grew|grow\w*|growth|higher|up|improv\w*|surged?|gained?|раст\w*|вырос\w*|рост\w*|повыс\w*|увелич\w*|stieg\w*|steigen\w*|gestiegen|höher\w*)'
DOWN = r'(?:decreas\w*|declin\w*|fall\w*|fell|lower\w*|down|drop\w*|reduc\w*|narrow\w*|снизи\w*|сниж\w*|сократ\w*|пада\w*|упал\w*|sank\w*|sinken\w*|gesunken|rückgang\w*|gesenkt|niedriger\w*)'
NEUTRAL = r'\b(?:will (?:publish|release|report)|annual general meeting|scheduled (?:for|on)|shareholder meeting|опубликует отч[её]т|дата отч[её]та|собрание акционеров|hauptversammlung|wird.{0,20}veröffentlichen)\b'
EXPLICIT = {
    'positive': [r'\b(?:beats?|exceeds?|exceeded)\b.{0,35}\b(?:estimates|expectations|forecast)\b',
                 r'\b(?:raises?|raised|upgraded)\b.{0,25}\b(?:outlook|guidance|forecast|rating)\b',
                 r'\b(?:record profits?|profit growth|превыс\w* ожидания|рекордн\w* прибыл\w*|рекордн\w* выручк\w*|rekordgewinn\w*)\b',
                 r'\b(?:shares?|stocks?|акции|aktien)\b.{0,20}\b(?:soars?|surges?|rall\w*|gains?|rises?|rose|растут|выросли|steigen|gestiegen)\b'],
    'negative': [r'\b(?:misses?|missed|below)\b.{0,35}\b(?:estimates|expectations|forecast)\b',
                 r'\b(?:cuts?|cut|lowered|downgraded)\b.{0,25}\b(?:outlook|guidance|forecast|rating)\b',
                 r'\b(?:profit warning|gewinnwarnung|bankrupt\w*|не оправдал\w* ожиданий|снижает прогноз)\b',
                 r'\b(?:shares?|stocks?|акции|aktien)\b.{0,20}\b(?:plunges?|tumbles?|slumps?|falls?|fell|drops?|падают|упали|fallen|gefallen)\b']}


def linked(text, subject, direction):
    # Punctuation/conjunctions and a second financial subject end a phrase;
    # "sales rose but costs fell" must not pair sales with falling costs.
    tokens=re.findall(r'[\w]+|[^\w\s]',text.lower())
    for i,token in enumerate(tokens):
        if not re.fullmatch(subject,token,re.I):continue
        for step in (1,-1):
            for distance in range(1,6):
                j=i+step*distance
                if not 0<=j<len(tokens):break
                word=tokens[j]
                if word in ('.',',',';',':','but','while','and','а','но','und','aber'):break
                if re.fullmatch(GOOD+'|'+BAD,word,re.I):break
                if re.fullmatch(direction,word,re.I):return True
    return False


@lru_cache(maxsize=2048)
def _evaluate_title(title):
    if not isinstance(title,str) or not title.strip() or '\ufffd' in title:
        return dict(label='unknown',reason='UNREADABLE_OR_EMPTY',method=METHOD)
    title=title[:1600]
    if '?' in title or re.search(r'\b(?:could|might|may|perhaps|возможно|может ли|könnte|vielleicht)\b',title,re.I):
        return dict(label='unknown',reason='QUESTION_OR_UNCERTAINTY',method=METHOD)
    if re.search(r"\b(?:not|never|no|without|didn.t|isn.t|nicht|kein\w*|не|нет|без)\b",title,re.I):
        return dict(label='unknown',reason='NEGATION',method=METHOD)
    signs=set()
    for sign,patterns in EXPLICIT.items():
        if any(re.search(pattern,title,re.I) for pattern in patterns):signs.add(sign)
    if linked(title,GOOD,UP) or linked(title,BAD,DOWN):signs.add('positive')
    if linked(title,GOOD,DOWN) or linked(title,BAD,UP):signs.add('negative')
    if len(signs)==1:return dict(label=signs.pop(),reason='FINANCIAL_PHRASE',method=METHOD)
    if len(signs)>1:return dict(label='unknown',reason='MIXED_DIRECTIONS',method=METHOD)
    if re.search(NEUTRAL,title,re.I):return dict(label='neutral',reason='SCHEDULE_OR_MEETING',method=METHOD)
    return dict(label='unknown',reason='INSUFFICIENT_EVIDENCE',method=METHOD)


def evaluate(article):
    title=article.get('original_title') or article.get('title') or ''
    if not isinstance(title,str):title=''
    return dict(_evaluate_title(title[:1600]))
