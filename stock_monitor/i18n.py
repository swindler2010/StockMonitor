"""Explicit presentation translation. Never translate stored identifiers/data."""
import json
from pathlib import Path
from string import Formatter

LANGUAGES=('ru','de','en')
LANGUAGE_NAMES={'ru':'Русский','de':'Deutsch','en':'English'}
_language='ru'
_catalog=None
_missing=set()

def language():return _language

def configure(store):
    global _language
    saved=store.setting('ui_language','ru')
    _language=saved if saved in LANGUAGES else 'ru'
    return _language

def save_language(store,code):
    if code not in LANGUAGES:raise ValueError('Unsupported UI language')
    store.set_setting('ui_language',code)
    return code

def fields(text):
    return [(field,spec,conversion) for _,field,spec,conversion in Formatter().parse(text) if field is not None]

def catalog():
    global _catalog
    if _catalog is None:
        _catalog=json.loads(Path(__file__).with_name('ui_messages.json').read_text(encoding='utf-8'))
        for source,translations in _catalog.items():
            for code,target in translations.items():
                if code not in LANGUAGES or sorted(fields(source))!=sorted(fields(target)):
                    raise ValueError('Invalid UI message translation')
    return _catalog

def tr(source,**values):
    if not isinstance(source,str):return source
    if _language=='ru':target=catalog().get(source,{}).get('ru',source)
    else:
        entry=catalog().get(source,{})
        target=entry.get(_language,source)
        if _language not in entry and len(_missing)<1000:_missing.add(source)
    return target.format(**values) if values else target

def missing_count():return len(_missing)

def language_settings(parent,store):
    import tkinter as tk
    from tkinter import ttk
    box=ttk.LabelFrame(parent,text=tr('Язык интерфейса'),padding=8)
    box.pack(fill='x',pady=8)
    saved=store.setting('ui_language','ru')
    choice=tk.StringVar(value=saved if saved in LANGUAGES else 'ru')
    note=tk.StringVar(value=tr('Выбор языка сохраняется сразу и применяется при следующем запуске.'))
    def changed():
        try:save_language(store,choice.get())
        except Exception:
            choice.set(store.setting('ui_language','ru'))
            note.set(tr('Не удалось сохранить язык. Повторите попытку.'))
        else:note.set(tr('Язык сохранён. Изменение вступит в силу при следующем запуске.'))
    for code in LANGUAGES:
        ttk.Radiobutton(box,text=LANGUAGE_NAMES[code],variable=choice,value=code,command=changed).pack(side='top',anchor='w')
    ttk.Label(box,textvariable=note,wraplength=430).pack(fill='x',pady=4)
    return box


def rendered_message(source):
    """Translate a known rendered UI message without translating captured values.

    Deterministic literal scanning avoids unbounded regex backtracking. Unknown
    strings, provider text, identifiers and numbers remain untouched.
    """
    if not isinstance(source,str) or _language=='ru':return source
    translated=tr(source)
    if translated!=source or len(source)>32768:return translated
    from ui_rendered_messages import TEMPLATES
    for template in TEMPLATES:
        parts=list(Formatter().parse(template));position=0;values={};valid=True
        for index,(literal,field,spec,conversion) in enumerate(parts):
            if not source.startswith(literal,position):valid=False;break
            position+=len(literal)
            if field is None:continue
            suffix=parts[index+1][0] if index+1<len(parts) else ''
            if not suffix and index+1<len(parts):valid=False;break
            end=source.find(suffix,position) if suffix else len(source)
            if end<position:valid=False;break
            values[field]=source[position:end];position=end
        if valid and position==len(source):return tr(template,**values)
    return source
