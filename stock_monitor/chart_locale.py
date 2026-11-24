"""Localize chart artists, never the renderer's canonical series names."""
from i18n import tr

def draw_localized(app):
    for ax in app.fig.axes:
        for label in (ax.xaxis.label,ax.yaxis.label):
            label.set_text(tr(label.get_text()))
        legend=ax.get_legend()
        if legend is not None:
            for label in legend.get_texts():label.set_text(tr(label.get_text()))
        for artist in ax.texts:
            original=getattr(artist,'_sm_canonical_text',artist.get_text())
            artist._sm_canonical_text=original
            artist.set_text(tr(original))
    from reference_design import polish_chart
    polish_chart(app)
    app.canvas.draw_idle()
