"""Localize chart artists, never the renderer's canonical series names."""
from i18n import tr

def draw_localized(app):
    for ax in app.fig.axes:
        for label in (ax.xaxis.label,ax.yaxis.label):
            label.set_text(tr(label.get_text()))
        legend=ax.get_legend()
        if legend is not None:
            for label in legend.get_texts():label.set_text(tr(label.get_text()))
    app.canvas.draw_idle()
