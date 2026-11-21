"""Local display-only news translation. Originals and trading inputs are immutable.

Models are optional checksum-verified Argos packs, loaded from ZIP in memory.
No model download, external API, portfolio data or network call in runtime.
"""
from collections import OrderedDict, Counter
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import zipfile

ENGINE = 'ct2-argos-display-v1'
LANGUAGES = ('ru', 'de', 'en')
_models = OrderedDict()
_detector = None


def fingerprint(row):
    return hashlib.sha256(json.dumps([ENGINE, row.get('url'), row.get('title'), row.get('summary')], ensure_ascii=False).encode()).hexdigest()


def setting(key, target):
    if target not in LANGUAGES: raise ValueError('UNSUPPORTED_TARGET_LANGUAGE')
    return 'news_translation:'+target+':'+key


def detect(text):
    global _detector
    if len(re.findall(r'[^\W\d_]', text)) < 12: raise ValueError('LANGUAGE_UNCERTAIN')
    from lingua import LanguageDetectorBuilder
    if _detector is None: _detector = LanguageDetectorBuilder.from_all_languages().with_low_accuracy_mode().build()
    confidence = _detector.compute_language_confidence_values(text[:2200])
    if not confidence or confidence[0].value < .65 or (len(confidence) > 1 and confidence[0].value-confidence[1].value < .15):
        raise ValueError('LANGUAGE_UNCERTAIN')
    code = confidence[0].language.iso_code_639_1.name.lower()
    if code not in LANGUAGES: raise ValueError('SOURCE_LANGUAGE_UNSUPPORTED')
    return code


def model_directory():
    # Existing Windows runtime dependency directory; no new task folders.
    return Path(sys.prefix)/'Дополнительные пакеты'


def load_model(source, target):
    pair = source+'_'+target
    if pair in _models: _models.move_to_end(pair); return _models[pair]
    directory = model_directory()
    manifest = json.loads((directory/'sm-translation-models.json').read_text(encoding='utf-8'))
    entry = next((x for x in manifest if x.get('pair') == pair), None)
    if not entry: raise ValueError('TRANSLATION_MODEL_NOT_INSTALLED')
    path = directory/('sm-translate-'+pair+'.argosmodel')
    if path.stat().st_size != entry['size'] or path.stat().st_size > 250_000_000: raise ValueError('MODEL_SIZE_MISMATCH')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''): digest.update(chunk)
    if digest.hexdigest() != entry['sha256']: raise ValueError('MODEL_HASH_MISMATCH')
    import ctranslate2
    import sentencepiece
    # Keep at most two directional models, CPU-only and a single inference thread.
    while len(_models) >= 2: _models.popitem(last=False)
    with zipfile.ZipFile(path) as archive:
        metadata = next(n for n in archive.namelist() if n.endswith('/metadata.json'))
        prefix = metadata[:-len('metadata.json')]
        info = json.loads(archive.read(metadata))
        if (info.get('from_code'), info.get('to_code')) != (source, target): raise ValueError('MODEL_IDENTITY_MISMATCH')
        files = {n[len(prefix+'model/'):]: archive.read(n) for n in archive.namelist()
                 if n.startswith(prefix+'model/') and not n.endswith('/')}
        tokenizer = sentencepiece.SentencePieceProcessor(model_proto=archive.read(prefix+'sentencepiece.model'))
    translator = ctranslate2.Translator(pair, files=files, device='cpu', compute_type='int8', inter_threads=1, intra_threads=1)
    _models[pair] = (translator, tokenizer)
    return translator, tokenizer


def numbers(text):
    return Counter(n.replace(',', '.') for n in re.findall(r'\d+(?:[.,]\d+)*', text))


def direct(text, source, target):
    if not text: return ''
    translator, tokenizer = load_model(source, target)
    # Whole short paragraphs; bounded tokens, no implicit truncation or model download.
    pieces = re.split(r'(?<=[.!?])\s+(?=[A-ZА-ЯÄÖÜ])|\n+', text)
    tokens = [tokenizer.encode(p, out_type=str) for p in pieces if p.strip()]
    if any(len(t) > 512 for t in tokens): raise ValueError('TRANSLATION_TEXT_TOO_LONG')
    result = translator.translate_batch(tokens, beam_size=2, max_batch_size=8, max_input_length=0, max_decoding_length=768,
                                        replace_unknowns=True, length_penalty=.2)
    if any(len(r.hypotheses[0]) >= 768 for r in result): raise ValueError('TRANSLATION_OUTPUT_LIMIT')
    text_out = ' '.join(tokenizer.decode(r.hypotheses[0]).strip() for r in result)
    if not text_out or len(text_out) > 8000: raise ValueError('TRANSLATION_EMPTY_OR_TOO_LONG')
    if numbers(text) != numbers(text_out): raise ValueError('TRANSLATION_NUMBERS_CHANGED')
    return text_out


def translate(row, target):
    title, summary = str(row.get('title') or ''), str(row.get('summary') or '')
    source = detect(title+' '+summary)
    if source == target: return dict(status='ALREADY_TARGET', source=source, target=target, title=title, summary=summary)
    route = [(source, target)] if 'en' in (source, target) else [(source, 'en'), ('en', target)]
    for left, right in route:
        title, summary = direct(title, left, right), direct(summary, left, right)
    return dict(status='TRANSLATED', source=source, target=target, title=title, summary=summary, engine=ENGINE,
                pivot='en' if len(route) == 2 else None)


def update_cache(rows, target, previous, translator=translate, now=None, maximum=3):
    now = time.time() if now is None else now
    wanted = {fingerprint(row) for row in rows[:100]}
    cache = {k: v for k, v in (previous.get('items') or {}).items() if k in wanted}
    count = 0
    for row in rows[:100]:
        key = fingerprint(row); old = cache.get(key, {})
        if old.get('target') == target and (old.get('status') in ('TRANSLATED', 'ALREADY_TARGET') or now-old.get('at', 0) < 3600): continue
        if count >= maximum: break
        count += 1
        try: value = translator(row, target)
        except Exception as error: value = dict(status='UNAVAILABLE', target=target, error=str(error)[:100])
        cache[key] = {**value, 'at': now, 'fingerprint': key}
    return dict(items=cache, target=target, engine=ENGINE)


def project(rows, cache, target):
    result = []
    for row in rows[:100]:
        value = cache.get('items', {}).get(fingerprint(row), {})
        accepted = value.get('target') == target and value.get('status') in ('TRANSLATED', 'ALREADY_TARGET')
        result.append({**row, 'original_title': row.get('title', ''), 'original_summary': row.get('summary', ''),
                       'title': value['title'] if accepted else row.get('title', ''),
                       'summary': value['summary'] if accepted else row.get('summary', ''),
                       'translation': value if accepted else dict(status='UNAVAILABLE' if value and time.time()-value.get('at',0)<3600 else 'PENDING', target=target, error=value.get('error'))})
    return result


def cached_snapshot(store, key, target):
    snapshot = store.setting('news:'+key, {})
    return {**snapshot, '_translation_cache': store.setting(setting(key, target), {}), '_translation_target': target}
