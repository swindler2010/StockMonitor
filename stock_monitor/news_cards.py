"""Selected news-card presentation; saved source texts remain UI-only."""
import hashlib,json,time
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from collections import OrderedDict
import tkinter as tk
from tkinter import ttk
from i18n import tr,language
from ui_icons import photo
from ui_collections import shared
from news_engine import safe_url,plain
from news_media import image_url


def clean_saved(value):
    if not isinstance(value,dict) or not isinstance(value.get('article'),dict):return None
    row=value['article'];url=safe_url(row.get('url'));title=plain(row.get('original_title',row.get('title')),500)
    target=value.get('target')
    if not url or not title or not isinstance(target,str) or len(target)>100:return None
    article={k:row.get(k) for k in ('provider','feed_provider','published','language','scope','publisher_status','publisher_url','relevance','link_kind') if isinstance(row.get(k),(str,type(None)))}
    article={k:(v[:500] if isinstance(v,str) else v) for k,v in article.items()}
    article.update(title=title,url=url,summary=plain(row.get('original_summary',row.get('summary')),1600),image_url=image_url(row.get('image_url')))
    return {'target':target,'article':article}


def identity(target,row):return hashlib.sha256(json.dumps([target,safe_url(row.get('url'))],ensure_ascii=False).encode()).hexdigest()


def published(row,now=None):
    try:
        dt=datetime.fromisoformat(row['published']).astimezone(ZoneInfo('Europe/Berlin'))
        today=(now or datetime.now(ZoneInfo('Europe/Berlin'))).date()
        day=tr('Сегодня') if dt.date()==today else tr('Вчера') if dt.date()==today-timedelta(days=1) else dt.strftime('%d.%m.%Y')
        return day+', '+dt.strftime('%H:%M')
    except (ValueError,TypeError,KeyError):return tr('Дата неизвестна')


class Cards:
    def __init__(self,dialog):
        self.d=dialog;self.saved=shared(dialog.app,'ui_news_collection_v1',clean_saved,('favorite','saved'))
        self.unsubscribe=self.saved.subscribe(self.changed);self.projected=[];self.project_key=None;self.sequence=0
        self.media=OrderedDict();self.photos={};self.labels={};self.buttons=[];self.retry={};self.media_busy=False;self.media_sequence=0;self.had_error=False
    def start(self):self.saved.load()
    def close(self):self.unsubscribe();self.labels.clear();self.photos.clear();self.media.clear()
    def changed(self):
        if self.d.closed:return
        if self.saved.error:self.d.status.set(tr('Достигнут лимит сохранённых элементов' if 'UI_COLLECTION_LIMIT' in self.saved.error else 'Не удалось сохранить отметку'))
        elif self.had_error:self.d.status.set(tr('Отметки сохранены'))
        self.had_error=bool(self.saved.error)
        self.project()
        if self.d.view_mode.get()==tr('Все новости'):self.refresh_buttons()
        else:self.d.render()
    def refresh_buttons(self):
        for button,item_id,flag,icon in self.buttons:
            if button.winfo_exists():
                button.configure(image=photo(self.d.app.root,icon+'_filled' if self.saved.has(item_id,flag) else icon,18),
                                 state='normal' if self.saved.loaded and not self.saved.busy else 'disabled')
    def project(self):
        d=self.d
        if d.closed or not self.saved.loaded:return
        key=d.target()[1];target=language();self.sequence+=1;sequence=self.sequence
        entries=[v['item']['article'] for v in self.saved.items.values() if v['item']['target']==key]
        if not entries:self.projected=[];self.project_key=(key,target);return
        def work():
            from news_translation import cached_snapshot
            from news_dashboard import prepare
            return prepare({**cached_snapshot(d.app.store,key,target),'articles':entries})['articles']
        def done(rows):
            if not d.closed and sequence==self.sequence and key==d.target()[1] and target==language():
                self.projected=rows;self.project_key=(key,target)
                if d.view_mode.get()!=tr('Все новости'):d.render()
        d.app.job(work,done,lambda _:None,lane='read',key=('saved-news-view',id(d.win),sequence))
    def rows(self,current):
        mode=self.d.view_mode.get();key=self.d.target()[1]
        if mode==tr('Все новости'):return current
        flag='favorite' if mode==tr('Избранные новости') else 'saved'
        rows={r['url']:r for r in self.projected} if self.project_key==(key,language()) else {}
        rows.update((r['url'],r) for r in current)
        return [r for r in rows.values() if self.saved.has(identity(key,r),flag)]
    def toggle(self,row,flag):
        key=self.d.target()[1]
        self.saved.toggle(identity(key,row),{'target':key,'article':row},flag)
    def render(self,rows):
        from news_dashboard import EVENTS,translation_note
        d=self.d;self.labels={};self.buttons=[]
        for row in rows:
            card=ttk.Frame(d.cards,padding=(10,10),style='DeskCard.TFrame');card.pack(fill='x',pady=(0,7))
            body=ttk.Frame(card,style='Desk.TFrame');body.pack(fill='both',expand=True)
            if not d.compact.get():
                thumb=tk.Frame(body,width=160,height=104,background='#0b2031');thumb.pack(side='left',anchor='n',padx=(0,12));thumb.pack_propagate(False)
                label=ttk.Label(thumb,style='DeskMuted.TLabel',anchor='center',text=tr('Нет превью'),image=photo(d.app.root,'news',38),compound='top')
                label.pack(fill='both',expand=True);url=image_url(row.get('image_url'))
                if url:
                    self.labels.setdefault(url,[]).append(label)
                    if url in self.media and self.media[url] is not None:self.paint(url)
                d.app.tooltip(label,'Превью предоставлено источником' if url else 'Изображение не предоставлено источником')
            content=ttk.Frame(body,style='Desk.TFrame');content.pack(fill='both',expand=True)
            top=ttk.Frame(content,style='Desk.TFrame');top.pack(fill='x',pady=(0,5))
            event=next((e for e in row.get('events',[]) if e in EVENTS),None)
            ttk.Label(top,text=tr(EVENTS[event]) if event else tr('Новости'),style='DeskMuted.TLabel',foreground='#31c5ff',font=('Segoe UI',9,'bold')).pack(side='left')
            item_id=identity(d.target()[1],row)
            for flag,icon,tip in (('saved','bookmark','Сохранить материал'),('favorite','star','Отметить важное')):
                button=ttk.Button(top,image=photo(d.app.root,icon+'_filled' if self.saved.has(item_id,flag) else icon,18),
                    command=lambda r=row,f=flag:self.toggle(r,f),width=3,state='normal' if self.saved.loaded and not self.saved.busy else 'disabled')
                button.pack(side='right',padx=(4,0));d.app.tooltip(button,tip)
                self.buttons.append((button,item_id,flag,icon))
            title=ttk.Label(content,text=row['title'],style='Desk.TLabel',font=('Segoe UI',12,'bold'),wraplength=540,justify='left',cursor='hand2');title.pack(anchor='w',fill='x')
            title.bind('<Button-1>',lambda _,r=row:d.article(r))
            note=ttk.Label(content,text=str(row.get('provider') or '—')+'  ·  '+published(row)+'  ·  '+translation_note(row),style='DeskMuted.TLabel',wraplength=540,font=('Segoe UI',9));note.pack(anchor='w',fill='x',pady=(4,3))
            text_labels=[title,note]
            if not d.compact.get():
                summary=row.get('summary') or tr('Краткое содержание не предоставлено источником')
                text=ttk.Label(content,text=summary[:220]+('…' if len(summary)>220 else ''),style='Desk.TLabel',font=('Segoe UI',10),wraplength=540,justify='left');text.pack(anchor='w',fill='x',pady=3);text_labels.append(text)
            content.bind('<Configure>',lambda event,labels=text_labels:[label.configure(wraplength=max(160,event.width-4)) for label in labels])
            actions=ttk.Frame(content,style='Desk.TFrame');actions.pack(fill='x',pady=(5,0))
            ttk.Button(actions,text=tr('Подробнее / источник'),command=lambda r=row:d.article(r)).pack(side='left')
            ttk.Button(actions,text=tr('Оригинал ↗'),command=lambda r=row:d.original(r)).pack(side='right')
        self.schedule_media()
    def paint(self,url):
        from PIL import ImageTk
        active=[label for label in self.labels.get(url,[]) if label.winfo_exists()]
        if not active or self.media.get(url) is None:return
        if url not in self.photos:self.photos[url]=ImageTk.PhotoImage(self.media[url],master=self.d.app.root)
        for label in active:label.configure(image=self.photos[url],text='',compound='none')
    def schedule_media(self):
        d=self.d
        if d.closed or self.media_busy or not hasattr(d.app.store,'directory'):return
        urls=[url for url in self.labels if url not in self.media and time.time()>=self.retry.get(url,0)][:6]
        if not urls:return
        self.media_busy=True;self.media_sequence+=1
        def work():
            from news_media import load
            result={};started=time.monotonic()
            for url in urls:
                if time.monotonic()-started>20:break
                try:result[url]=load(url,d.app.store.directory/'provider_cache')
                except Exception:result[url]=None
            return result
        def done(result):
            self.media_busy=False
            if d.closed:return
            for url,value in result.items():
                if value is None:self.retry[url]=time.time()+3600
                else:self.media[url]=value;self.media.move_to_end(url);self.paint(url)
            while len(self.media)>48:
                url,_=self.media.popitem(last=False);self.photos.pop(url,None)
            d.win.after_idle(self.schedule_media)
        def failed(_):self.media_busy=False
        if d.app.job(work,done,failed,lane='misc',key='news-preview-batch') is False:
            failed(None);d.win.after(2000,self.schedule_media)
