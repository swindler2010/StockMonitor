"""Reference-screen presentation helpers. Never change analytical inputs or actions."""
import re
import tkinter as tk
from tkinter import ttk
from i18n import tr


STATES = {'OUTPERFORMING':'Сильнее индекса', 'UNDERPERFORMING':'Слабее индекса',
          'NEUTRAL':'На уровне индекса', 'UNAVAILABLE':'Нет данных',
          'BULLISH':'Рост', 'BEARISH':'Снижение', 'TRANSITION':'Переход', 'RANGE':'Боковик'}


def state_label(value):
    return tr(STATES.get(value, value))


def signal_reason(value):
    """Known human explanations only; unknown provider text remains verbatim."""
    from diagnostic_display import diagnostic_text
    parts = []
    for part in str(value or '').split('; '):
        match = re.fullmatch(r'([^;\n]{1,80}): (OUTPERFORMING|UNDERPERFORMING|NEUTRAL|UNAVAILABLE)', part)
        parts.append(match[1]+': '+state_label(match[2]) if match else diagnostic_text(part))
    return '; '.join(parts)


def concise_reason(engine):
    """One whole deciding reason; the complete explanation stays in Analysis."""
    if not isinstance(engine,dict):return tr('Ожидается расчёт')
    key=('blockers' if engine.get('status')=='DATA BLOCKED' else
         'entry_blockers' if engine.get('signal')=='WAIT' else
         'bearish_reasons' if engine.get('signal')=='SELL' else 'bullish_reasons')
    reasons=engine.get(key) or []
    if not isinstance(reasons,(list,tuple)):reasons=[reasons]
    for reason in reasons:
        text=reason.get('text') if isinstance(reason,dict) else reason
        if text:return signal_reason(text)
    parts=str(engine.get('reason') or '').split('; ')
    for part in parts:
        if part and not re.fullmatch(r'[^;\n]{1,80}: (OUTPERFORMING|UNDERPERFORMING|NEUTRAL|UNAVAILABLE)',part):return signal_reason(part)
    return signal_reason(engine.get('reason')) or tr('Ожидается расчёт')


def wrap_label(label, padding=0):
    """Natural requested height, measured wrapping, no character truncation."""
    if getattr(label,'_reference_wrap_installed',False):return label
    label._reference_wrap_installed=True
    def inset(widget,value,box=True):
        parts=value if isinstance(value,(tuple,list)) else widget.tk.splitlist(str(value))
        values=[widget.winfo_pixels(str(v)) for v in parts]
        return (2*values[0] if box else sum(values)) if len(values)==2 else 2*values[0] if len(values)==1 else values[0]+values[2] if values else 0
    def resized(event):
        if not label.winfo_exists():return
        if label.winfo_manager()=='pack':
            # A naturally sized packed label is not its allocated width.
            # Feeding its own wrapped request back into wraplength shrinks
            # it word by word and makes the whole card grow vertically.
            parent=label.master
            try:inside=inset(parent,parent.cget('padding'))
            except tk.TclError:inside=0
            gap=inset(label,label.pack_info().get('padx',0),box=False)
            width=max(60,parent.winfo_width()-inside-gap-padding-4)
        else:width=max(60,label.winfo_width()-padding)
        if int(float(label.cget('wraplength') or 0)) != width:
            label.configure(wraplength=width)
    label.bind('<Configure>', resized, add='+')
    if label.winfo_manager()=='pack':
        parent=label.master
        binding=parent.bind('<Configure>',resized,add='+')
        label.bind('<Destroy>',lambda e:parent.unbind('<Configure>',binding) if e.widget is label and parent.winfo_exists() else None,add='+')
    return label


def flow_toolbar(frame, widgets):
    """Reflow the same controls; keep commands, variables, order and disabled state."""
    from main_screen import flow_widgets
    for widget in widgets:widget.pack_forget()
    def reflow(event=None):
        if frame.winfo_exists():flow_widgets(widgets, max(120, frame.winfo_width()))
    frame.bind('<Configure>', reflow, add='+')
    frame.after_idle(reflow)


def window_captions(app,palette):
    """Theme native captions of this Tk application, including legacy service windows."""
    root=app.root;root._reference_caption_palette=dict(palette)
    def apply(win):
        from main_screen import native_titlebar
        if win.winfo_exists():native_titlebar(win,root._reference_caption_palette)
    if not getattr(root,'_reference_caption_binding',None):
        def mapped(event):
            if isinstance(event.widget,tk.Toplevel):root.after_idle(lambda:apply(event.widget))
        root._reference_caption_binding=root.bind_class('Toplevel','<Map>',mapped,add='+')
    for widget in root.winfo_children():
        if isinstance(widget,tk.Toplevel):root.after_idle(lambda win=widget:apply(win))


class Card(ttk.Frame):
    """Rounded reference card with the LabelFrame text interface used by old views."""
    def __init__(self, parent, text, prefix, **kwargs):
        super().__init__(parent, style=prefix+'.Info.TFrame', padding=(12,9), **kwargs)
        self.heading=ttk.Label(self,text=tr(text),style=prefix+'.Drawing.TLabel',font=('Segoe UI',11,'bold'))
        self.heading.pack(anchor='w',pady=(0,7))

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf,dict):kwargs={**cnf,**kwargs};cnf=None
        if 'text' in kwargs:
            text=kwargs.pop('text')
            prefix='Поручение владельца • '
            self.heading.configure(text=tr('Поручение владельца • {account}',account=text[len(prefix):]) if text.startswith(prefix) else tr(text))
        return super().configure(cnf,**kwargs)

    config=configure


def chart_note_lines(notes, width, measure):
    """Wrap a bounded set of chart annotations in a dedicated strip, never over a legend."""
    lines=[]
    for note in notes:
        line=''
        for word in note.split():
            candidate=(line+' '+word).strip()
            if line and measure(candidate)>width:
                lines.append(line);line=word
            else:line=candidate
        if line:lines.append(line)
    return lines


def polish_chart(app):
    if not hasattr(app,'main_screen') or not hasattr(app,'ax_price'):return
    axis=app.ax_price
    notes=[]
    for artist in axis.texts:
        if artist.get_gid() in ('sm-level-key','sm-chart-scope','sm-chart-live','sm-chart-through'):
            notes.append(artist.get_text());artist.set_visible(False)
    figure=app.fig
    strip=next((t for t in figure.texts if t.get_gid()=='sm-chart-notes'),None)
    if not notes:
        if strip is not None:strip.set_visible(False)
        return
    from matplotlib.font_manager import FontProperties
    renderer=app.canvas.get_renderer()
    font=FontProperties(family='DejaVu Sans',size=8)
    spec=axis.get_subplotspec().get_gridspec()
    width=max(100,figure.bbox.width*(spec.right-spec.left))
    measure=lambda s:renderer.get_text_width_height_descent(s,font,False)[0]
    lines=chart_note_lines(notes,width,measure)
    line_height=renderer.points_to_pixels(11)
    reserve=line_height*len(lines)+renderer.points_to_pixels(19)
    top=max(.60,min(.93,1-reserve/max(1,figure.bbox.height)))
    if abs(spec.top-top)>.001:
        spec.update(top=top)
        # Embedded Figure objects are not registered in pyplot's manager list.
        for panel in figure.axes:
            sub=panel.get_subplotspec() if hasattr(panel,'get_subplotspec') else None
            if sub is not None and sub.get_gridspec() is spec:
                panel.set_position(sub.get_position(figure))
    if strip is None:
        strip=figure.text(spec.left,.985,'',ha='left',va='top',fontproperties=font,
                          color=axis.yaxis.label.get_color(),linespacing=1.2,gid='sm-chart-notes')
    strip.set_text('\n'.join(lines));strip.set_visible(True)

