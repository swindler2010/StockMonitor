import tkinter as tk

def install_text_menus(root):
    def popup(event):
        widget=event.widget
        widget.focus_set()
        menu=tk.Menu(widget,tearoff=False)
        try:
            state=str(widget.cget('state'))
        except tk.TclError:
            state='normal'
        editable=state not in ('readonly','disabled')
        for label,action in [('Cut','<<Cut>>'),('Copy','<<Copy>>'),('Paste','<<Paste>>'),('Delete','<<Clear>>'),('Select All','<<SelectAll>>')]:
            menu.add_command(label=label,state='normal' if editable or label in ('Copy','Select All') else 'disabled',command=lambda action=action:widget.event_generate(action))
        try:menu.tk_popup(event.x_root,event.y_root)
        finally:menu.grab_release()
        return 'break'
    for klass in ('Entry','TEntry','Text','Spinbox','TSpinbox','TCombobox'):
        root.bind_class(klass,'<Button-3>',popup,add='+')
        root.bind_class(klass,'<Shift-F10>',popup,add='+')
