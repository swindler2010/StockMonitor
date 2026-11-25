from i18n import tr
"""User drafts and atomic explicit save, confined to the development line."""
import copy
import json
import tkinter as tk
from tkinter import ttk, messagebox

USER_SETTINGS = frozenset(('selected','theme','global_indicators','indicators','alerts_enabled',
    'automatic_history_fallback','chart_mode','ai_config','update_repository','mcp_allow_edits'))

class DraftSettingsMixin:
    def enable_user_drafts(self):
        self.pending_user_settings = {}

    def setting(self,key,default=None):
        pending=getattr(self,'pending_user_settings',{})
        if key in pending:return copy.deepcopy(pending[key])
        return super().setting(key,default)

    def set_setting(self,key,value):
        if hasattr(self,'pending_user_settings') and key in USER_SETTINGS:
            # Validate before accepting the draft, with the same SQLite encoding.
            json.dumps(value,ensure_ascii=False,allow_nan=False)
            if value==super().setting(key,None):self.pending_user_settings.pop(key,None)
            else:self.pending_user_settings[key]=copy.deepcopy(value)
            return
        return super().set_setting(key,value)

    def save_user_state(self,watchlist):
        items=[(i['isin'],json.dumps(i,ensure_ascii=False,allow_nan=False)) for i in watchlist]
        settings=[(k,json.dumps(v,ensure_ascii=False,allow_nan=False)) for k,v in self.pending_user_settings.items()]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._write_watchlist(db,items)
            db.executemany('INSERT OR REPLACE INTO settings VALUES (?,?)',settings)
        self.pending_user_settings.clear()

def ask_save_on_exit(root):
    result={'choice':'cancel'}
    window=tk.Toplevel(root);window.title(tr('Несохранённые изменения'))
    window.transient(root);window.resizable(False,False)
    body=ttk.Frame(window,padding=20);body.pack(fill='both',expand=True)
    ttk.Label(body,text=tr('Сохранить изменения перед выходом?')).pack(pady=(0,18))
    row=ttk.Frame(body);row.pack()
    def choose(choice):
        result['choice']=choice;window.destroy()
    for text,choice in (('Сохранить','save'),('Не сохранять','discard'),('Отмена','cancel')):
        ttk.Button(row,text=tr(text),command=lambda c=choice:choose(c)).pack(side='left',padx=5)
    window.protocol('WM_DELETE_WINDOW',lambda:choose('cancel'))
    window.bind('<Escape>',lambda _:choose('cancel'))
    window.update_idletasks()
    x=root.winfo_rootx()+max(0,(root.winfo_width()-window.winfo_width())//2)
    y=root.winfo_rooty()+max(0,(root.winfo_height()-window.winfo_height())//2)
    window.geometry(f'+{x}+{y}');window.grab_set();window.wait_window()
    return result['choice']

class SaveExitMixin:
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        # Loading normalizes legacy indicators; that is not an unsaved user edit.
        self.persisted=copy.deepcopy(self.watchlist)
        self.store.enable_user_drafts()
        self._saved_selection=self.selection_state()

    def selection_state(self):
        return {'symbol':self.symbol.get(),'timeframe':self.timeframe.get(),'period':self.period.get()}

    def user_changes(self):
        return [copy.deepcopy(self.drafts.get(i['isin'],i)) for i in self.watchlist]

    def has_unsaved_changes(self):
        return bool(self.store.pending_user_settings) or self.user_changes()!=self.persisted or self.selection_state()!=self._saved_selection

    def save_user_changes(self):
        items=self.user_changes()
        self.store.set_setting('selected',{'symbol':self.symbol.get(),'timeframe':self.timeframe.get(),'period':self.period.get()})
        self.store.save_user_state(items)
        if getattr(self,'bridge',None):
            try:self.bridge.commands.confirm_saved_levels(items)
            except Exception:self._write_log('MCP receipt finalization unavailable after successful Save')
        self.watchlist=items;self.persisted=copy.deepcopy(items)
        self.drafts.clear();self.user_dirty=False;self.status.set(tr('Изменения сохранены'))
        self._saved_selection=self.selection_state()

    def confirm_close(self):
        if not self.has_unsaved_changes():return True
        choice=ask_save_on_exit(self.root)
        if choice=='cancel':return False
        if choice=='save':
            try:self.save_user_changes()
            except Exception as error:
                messagebox.showerror(tr('Ошибка сохранения'),tr('Изменения не сохранены. Программа остаётся открытой.\n{p0}',p0=error),parent=self.root)
                return False
        return True
