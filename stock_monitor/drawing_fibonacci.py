"""Explicit bounded manual Fibonacci fan, screen arcs and bar time zones."""
import math
import numpy as np
from matplotlib.path import Path
from matplotlib.patches import PathPatch
from matplotlib.transforms import Affine2D, IdentityTransform
from i18n import tr

KINDS = ('fib_fan', 'fib_arcs', 'fib_timezones')
RATIOS = (.236, .382, .5, .618, .786, 1.)
ZONES = (0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89)


def fan(a, b, horizon):
    """Classic price fan: A -> (Bx, Ay + ratio * (By-Ay))."""
    x, y = a; ex, ey = b
    if not all(math.isfinite(v) for v in (*a, *b, horizon)) or ex <= x or ey == y:
        raise ValueError('Фибоначчи: вторая точка должна быть позже первой и иметь другую цену')
    end = max(ex, horizon)
    return [(r, a, (end, y+(ey-y)*r*(end-x)/(ex-x))) for r in RATIOS]


class ScreenFibArc(PathPatch):
    """A semicircle centered on B, facing A; recomputed through pan/zoom/resize.

    The radius is ratio * screen distance(A,B), not price units plus bar units.
    This geometry intentionally depends on the screen aspect ratio.
    """
    def __init__(self, axes, a, b, ratio, **kwargs):
        self.axes_owner, self.a, self.b, self.ratio = axes, a, b, ratio
        super().__init__(Path.arc(-90, 90), transform=IdentityTransform(), fill=False, **kwargs)

    def get_patch_transform(self):
        a, b = self.axes_owner.transData.transform([self.a, self.b])
        delta = a-b; radius = float(np.linalg.norm(delta))*self.ratio
        angle = math.atan2(float(delta[1]), float(delta[0]))
        return Affine2D().scale(radius).rotate(angle).translate(*b)


def render(kind, xy, app, frame, style, kw, register):
    if kind not in KINDS: return False
    ax = app.ax_price
    if len(xy) != 2: return True
    a, b = xy; x, y = a; ex, ey = b
    def line(xs, ys, **overrides):
        artist, = ax.plot(xs, ys, scalex=False, scaley=False, **{**kw, **overrides}); register(artist)
    def label(x, y, text, **extra):
        artist = ax.annotate(text, (x, y), xytext=(4, 4), textcoords='offset points',
                             color=style['color'], alpha=style['opacity'], fontsize=style['fontsize'],
                             clip_on=True, parse_math=False, zorder=kw['zorder']+.0001, **extra)
        register(artist)
    if ex <= x:
        label(x, y, tr('Фибоначчи: нужны две разные отметки времени')); return True
    if ax.get_xscale() != 'linear' or (kind != 'fib_timezones' and ax.get_yscale() != 'linear'):
        label(x, y, tr('Фибоначчи: требуется линейная шкала')); return True
    if kind == 'fib_timezones':
        for number in ZONES:
            at = x+(ex-x)*number
            line([at, at], [0, 1], transform=ax.get_xaxis_transform())
            label(at, .96, str(number), xycoords=ax.get_xaxis_transform())
        label(x, y, tr('Временные зоны: интервалы баров, не календарные дни'))
        return True
    if ey == y: return True
    line([x, ex], [y, ey], alpha=style['opacity']*.5)
    if kind == 'fib_fan':
        horizon = max(len(frame)-1, float(ax.get_xlim()[1]))
        for ratio, origin, end in fan(a, b, horizon):
            line([origin[0], end[0]], [origin[1], end[1]])
            label(ex, y+(ey-y)*ratio, f'{ratio:g}')
        return True
    for ratio in RATIOS:
        arc = ScreenFibArc(ax, a, b, ratio, edgecolor=style['color'], linewidth=style['width'],
                           linestyle=style['linestyle'], alpha=style['opacity'], zorder=kw['zorder'])
        # add_artist does not update data limits or autoscale the user's chart.
        ax.add_artist(arc); arc.set_clip_path(ax.patch); register(arc)
        label(ex+(x-ex)*ratio, ey+(y-ey)*ratio, f'{ratio:g}')
    label(ex, ey, tr('Дуги: радиус A–B в экранном масштабе'))
    return True
