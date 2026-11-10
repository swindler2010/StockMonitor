"""Transient artists share one background; OHLCV/indicators are never redrawn on hover."""
class Overlay:
    def __init__(self,canvas,axes,color,text_color,bg):
        self.canvas=canvas;self.axes=axes;self.background=None
        self.vertical=[ax.axvline(0,color=color,ls='--',lw=.7,animated=True,visible=False) for ax in axes]
        price=axes[0]
        self.horizontal=price.axhline(0,color=color,ls='--',lw=.7,animated=True,visible=False)
        options=dict(color=text_color,fontsize=8,bbox=dict(facecolor=bg,alpha=.95),zorder=30,animated=True,visible=False)
        self.caption=price.text(.01,.99,'',transform=price.transAxes,va='top',**options)
        self.price=price.text(1,0,'',transform=price.get_yaxis_transform(),va='center',**options)
        self.artists=self.vertical+[self.horizontal,self.caption,self.price]
        self.callbacks=[canvas.mpl_connect('draw_event',self.on_draw),canvas.mpl_connect('resize_event',self.invalidate)]
        self.limits=[(ax,ax.callbacks.connect(event,self.invalidate)) for ax in axes for event in ('xlim_changed','ylim_changed')]

    def invalidate(self,*args):self.background=None

    def on_draw(self,event):
        self.background=self.canvas.copy_from_bbox(self.canvas.figure.bbox)
        self.paint()

    def paint(self):
        if self.background is None:self.canvas.draw_idle();return
        self.canvas.restore_region(self.background)
        for artist in self.artists:
            if artist.get_visible():artist.axes.draw_artist(artist)
        self.canvas.blit(self.canvas.figure.bbox)

    def show(self,index,y,text):
        for line in self.vertical:line.set_xdata([index,index])
        self.horizontal.set_ydata([y,y]);self.caption.set_text(text)
        self.price.set_position((1,y));self.price.set_text(f'{y:.2f}')
        for artist in self.artists:artist.set_visible(True)
        self.paint()

    def hide(self):
        for artist in self.artists:artist.set_visible(False)
        self.paint()

    def close(self):
        for cid in self.callbacks:self.canvas.mpl_disconnect(cid)
        for ax,cid in self.limits:ax.callbacks.disconnect(cid)
        for artist in self.artists:artist.remove()
        self.background=None
