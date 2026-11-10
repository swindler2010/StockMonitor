"""Translated enum labels with canonical values retained in application state."""
import tkinter as tk
from tkinter import ttk
from i18n import tr

class CanonicalCombobox(ttk.Combobox):
    def __init__(self,master=None,**kwargs):
        self._canonical_values=tuple(kwargs.pop('values',()))
        self._model=kwargs.pop('textvariable',None)
        self._display=tk.StringVar(master)
        self._syncing=False
        self._model_trace=None
        super().__init__(master,values=self._labels(),textvariable=self._display,**kwargs)
        self._display_trace=self._display.trace_add('write',self._from_display)
        if self._model is not None:
            self._model_trace=self._model.trace_add('write',self._from_model)
            self._from_model()

    def _from_model(self,*unused):
        if self._syncing:return
        self._syncing=True
        try:
            value=self._model.get()
            self._display.set(self._labels()[self._canonical_values.index(value)] if value in self._canonical_values else value)
        finally:self._syncing=False

    def _from_display(self,*unused):
        if self._syncing or self._model is None:return
        self._syncing=True
        try:self._model.set(self.get())
        finally:self._syncing=False

    def get(self):
        visible=super().get()
        # Index preserves identity even if two labels have the same translation.
        index=super().current()
        return self._canonical_values[index] if 0<=index<len(self._canonical_values) else visible

    def _labels(self):
        labels=[tr(v) for v in self._canonical_values]
        return [str(label)+' ['+str(value)+']' if labels.count(label)>1 else label for value,label in zip(self._canonical_values,labels)]

    def set(self,value):
        if value in self._canonical_values:self.current(self._canonical_values.index(value))
        else:super().set(value)

    def configure(self,cnf=None,**kwargs):
        if isinstance(cnf,dict):kwargs={**cnf,**kwargs};cnf=None
        if 'values' in kwargs:
            value=self._model.get() if self._model is not None else self.get()
            previous=self._canonical_values
            self._canonical_values=tuple(kwargs['values']);kwargs['values']=self._labels()
            try:result=super().configure(cnf,**kwargs)
            except Exception:
                self._canonical_values=previous
                raise
            # Re-render from the canonical value: the old visible label may
            # have changed or disappeared when the option list was refreshed.
            self.set(value)
            return result
        return super().configure(cnf,**kwargs)
    config=configure

    def cget(self,key):
        if key=='values':return self._canonical_values
        if key=='textvariable' and self._model is not None:return str(self._model)
        return super().cget(key)

    def destroy(self):
        if self._model_trace is not None:
            self._model.trace_remove('write',self._model_trace);self._model_trace=None
        self._display.trace_remove('write',self._display_trace)
        super().destroy()
