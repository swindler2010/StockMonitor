"""Rebase a manual viewport by candle time when background history grows."""
import numpy as np


def rebase(view, old_index, new_index):
    if view is None or old_index is None or len(old_index)<2 or len(new_index)<2:
        return view
    if old_index.equals(new_index):return view
    # Work relative to a nearby timestamp, avoiding nanosecond float precision.
    origin=old_index.asi8[0]
    old=(old_index.asi8-origin)/1e9; new=(new_index.asi8-origin)/1e9
    def interpolate(value, x, y):
        i=max(0,min(len(x)-2,int(np.searchsorted(x,value)-1)))
        return float(y[i]+(value-x[i])*(y[i+1]-y[i])/(x[i+1]-x[i]))
    xlim=tuple(interpolate(interpolate(x,np.arange(len(old)),old),new,np.arange(len(new))) for x in view[0])
    return xlim,view[1]

