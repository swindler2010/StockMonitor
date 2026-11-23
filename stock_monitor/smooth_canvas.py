"""Keep the last chart bitmap visible while Tk settles its layout.

Only the UI canvas changes. Figures, artists, data, viewport and publication
guards stay with their existing owners; no worker or market cache is added.
"""
import tkinter as tk
import time
from matplotlib.backend_bases import ResizeEvent
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg as BaseCanvas
from matplotlib.colors import to_hex


class FigureCanvasTkAgg(BaseCanvas):
    frame_delay_ms = 16
    resize_quiet_ms = 80
    resize_budget_ms = 250

    def __init__(self,*args,**kwargs):
        self._pending_bitmap_size=None
        self._draw_requested_at=None
        self._last_resize_at=0
        super().__init__(*args,**kwargs)
        self._tkcanvas.configure(background=to_hex(self.figure.get_facecolor()))
        self._tkcanvas.bind('<Destroy>',self._destroy_pending_draw,add='+')

    def _destroy_pending_draw(self,event):
        if event.widget is self._tkcanvas and self._idle_draw_id:
            try:self._tkcanvas.after_cancel(self._idle_draw_id)
            except tk.TclError:pass
            self._idle_draw_id=None

    def resize(self,event):
        self._resize_figure_for_canvas_size(event.width,event.height)

    def _resize_figure_for_canvas_size(self,width,height):
        # TkAgg normally clears its visible PhotoImage immediately on every
        # Configure. Header/side-panel layout may emit several in one switch,
        # showing a blank or intermediate chart before the final draw.
        size=(int(width),int(height))
        if min(size)<=0:return
        if self._pending_bitmap_size==size:return
        if (self._pending_bitmap_size is None
                and (self._tkphoto.width(),self._tkphoto.height())==size
                and tuple(round(n) for n in self.figure.bbox.size)==size):return
        self._pending_bitmap_size=size
        self._last_resize_at=time.monotonic()
        self.figure.set_size_inches(width/self.figure.dpi,height/self.figure.dpi,forward=False)
        ResizeEvent('resize_event',self)._process()
        self.draw_idle()

    def draw_idle(self):
        # A fixed, bounded frame delay coalesces layout bursts. Further events
        # do not postpone the deadline, so continuous interaction still draws.
        if self._idle_draw_id:return
        self._draw_requested_at=time.monotonic()
        def idle_draw():
            # Native maximize/restore emits Configure in several waves. Wait
            # briefly for geometry to settle, while bounding postponement so
            # a continuously dragged window still receives updated frames.
            now=time.monotonic()
            quiet=self.resize_quiet_ms-(now-self._last_resize_at)*1000
            budget=self.resize_budget_ms-(now-self._draw_requested_at)*1000
            if self._pending_bitmap_size is not None and quiet>0 and budget>0:
                self._idle_draw_id=self._tkcanvas.after(max(1,int(min(quiet,budget))),idle_draw)
                return
            try:self.draw()
            finally:self._idle_draw_id=None;self._draw_requested_at=None
        self._idle_draw_id=self._tkcanvas.after(self.frame_delay_ms,idle_draw)

    def draw(self):
        self._tkcanvas.configure(background=to_hex(self.figure.get_facecolor()))
        size=self._pending_bitmap_size
        if size is None or (self._tkphoto.width(),self._tkphoto.height())==size:
            super().draw();self._pending_bitmap_size=None;return
        previous=self._tkphoto
        self._tkphoto=tk.PhotoImage(master=self._tkcanvas,width=size[0],height=size[1])
        try:
            # Agg renders and blits to an as-yet undisplayed image. Only the
            # completed bitmap is attached to the existing canvas item.
            super().draw()
        except Exception:
            self._tkphoto=previous
            raise
        self._tkcanvas.itemconfigure(self._tkcanvas_image_region,image=self._tkphoto)
        self._tkcanvas.coords(self._tkcanvas_image_region,size[0]/2,size[1]/2)
        self._pending_bitmap_size=None
