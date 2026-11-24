"""Small UI-only collections. One existing Dispatcher; no trading authority."""
import json


def decode(value,clean,flags,limit):
    if not isinstance(value,dict) or value.get('version')!=1:return {}
    result={};items=value.get('items')
    if not isinstance(items,dict):return result
    for key,row in list(items.items())[:limit]:
        if not isinstance(key,str) or len(key)>200 or not isinstance(row,dict):continue
        item=clean(row.get('item'))
        if item is None:continue
        states={flag:row.get(flag) is True for flag in flags}
        if any(states.values()):result[key]={'item':item,**states}
    return result


def toggle_saved(store,key,item_id,item,flag,flags,clean,limit=200):
    """Apply one intent to the latest value; never overwrite another UI intent."""
    if flag not in flags or not isinstance(item_id,str) or len(item_id)>200:raise ValueError('INVALID_UI_COLLECTION')
    item=clean(item)
    if item is None:raise ValueError('INVALID_UI_COLLECTION_ITEM')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        items=decode(json.loads(row[0]) if row else {},clean,flags,limit)
        if item_id not in items and len(items)>=limit:raise ValueError('UI_COLLECTION_LIMIT')
        entry=items.get(item_id,{'item':item,**dict.fromkeys(flags,False)})
        entry={**entry,'item':item,flag:not entry.get(flag,False)}
        if any(entry.get(f) for f in flags):items[item_id]=entry
        else:items.pop(item_id,None)
        db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)',
                   (key,json.dumps({'version':1,'items':items},ensure_ascii=False,allow_nan=False)))
    return items


class Collection:
    def __init__(self,app,key,clean,flags=('favorite',),limit=200):
        self.app,self.key,self.clean,self.flags,self.limit=app,key,clean,flags,limit
        self.items={};self.loaded=False;self.busy=False;self.error=None;self.listeners={};self.sequence=0
    def subscribe(self,callback):
        token=object();self.listeners[token]=callback
        return lambda:self.listeners.pop(token,None)
    def notify(self):
        for callback in list(self.listeners.values()):callback()
    def load(self):
        if self.loaded or self.busy:return
        self.busy=True
        def done(value):
            self.items=decode(value,self.clean,self.flags,self.limit);self.loaded=True;self.busy=False;self.error=None;self.notify()
        def failed(error):self.busy=False;self.error=str(error);self.notify()
        if self.app.job(lambda:self.app.store.setting(self.key,{}),done,failed,
                        lane='read',key=('ui-collection-load',self.key)) is False:failed('QUEUE_BUSY')
    def has(self,item_id,flag='favorite'):return self.items.get(item_id,{}).get(flag) is True
    def toggle(self,item_id,item,flag='favorite'):
        if not self.loaded or self.busy or getattr(self.app,'closing',False):return False
        self.busy=True;self.error=None;self.sequence+=1;self.notify()
        def done(value):self.items=value;self.busy=False;self.notify()
        def failed(error):self.busy=False;self.error=str(error);self.notify()
        if self.app.job(lambda:toggle_saved(self.app.store,self.key,item_id,item,flag,self.flags,self.clean,self.limit),
                        done,failed,lane='read',key=('ui-collection-save',self.key,self.sequence)) is False:failed('QUEUE_BUSY')
        return True


def shared(app,key,clean,flags=('favorite',),limit=200):
    if not hasattr(app,'_ui_collections'):app._ui_collections={}
    if key not in app._ui_collections:app._ui_collections[key]=Collection(app,key,clean,flags,limit)
    return app._ui_collections[key]
