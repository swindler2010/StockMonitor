import sys,unittest
from pathlib import Path
import numpy as np,pandas as pd
from matplotlib.figure import Figure
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from chart_modes import draw_prices,draw_segments,MODES
class RenderTests(unittest.TestCase):
    def test_all_modes_exact_ohlc_geometry_and_colors(self):
        f=pd.DataFrame({'open':[10.,14.,8.],'high':[13.,15.,9.],'low':[9.,10.,7.],'close':[12.,11.,8.],'volume':[2.,3.,0.]})
        for mode in MODES:
            ax=Figure().subplots();draw_prices(ax,f,mode)
            if mode!='Line':
                for line,up in zip(ax.lines[:2],(True,False)):
                    actual=np.column_stack((line.get_xdata(),line.get_ydata())).reshape(-1,3,2)
                    expected=[]
                    for i,r in enumerate(f.itertuples()):
                        if (r.close>=r.open)!=up:continue
                        expected.append([(i,r.low),(i,r.high)])
                        if mode=='Bars':expected.extend([[(i-.3,r.open),(i,r.open)],[(i,r.close),(i+.3,r.close)]])
                    np.testing.assert_array_equal(actual[:,:2],expected);self.assertTrue(np.isnan(actual[:,2]).all())
            if mode in ('Candles','Candles + Close line'):
                from matplotlib.colors import to_rgba
                for patch,up,color in zip(ax.patches,(True,False),('#22c55e','#ef4444')):
                    self.assertEqual(patch.get_facecolor(),to_rgba(color))
                    path=patch.get_path();self.assertFalse(path.should_simplify)
                    verts=path.vertices.reshape(-1,5,2);codes=path.codes.reshape(-1,5)
                    expected=[]
                    for i,r in enumerate(f.itertuples()):
                        if (r.close>=r.open)!=up:continue
                        lo=min(r.open,r.close);hi=max(r.open,r.close,lo+max(abs(lo)*.00005,.00001))
                        expected.append([(i-.325,lo),(i+.325,lo),(i+.325,hi),(i-.325,hi),(i-.325,lo)])
                    np.testing.assert_array_equal(verts,expected)
                    np.testing.assert_array_equal(codes,np.tile([1,2,2,2,79],(len(expected),1)))
                self.assertLessEqual(ax.dataLim.x0,-.325);self.assertGreaterEqual(ax.dataLim.x1,2.325)
            if mode in ('Line','Candles + Close line'):np.testing.assert_array_equal(ax.lines[-1].get_ydata(),f.close)
    def test_segment_gaps_and_volume_values_retained(self):
        segments=np.array([[(0,0),(0,5)],[(1,0),(1,0)],[(2,0),(2,-3)]])
        line=draw_segments(Figure().subplots(),segments,color='blue',lw=1)
        actual=np.column_stack((line.get_xdata(),line.get_ydata())).reshape(-1,3,2)
        np.testing.assert_array_equal(actual[:,:2],segments);self.assertTrue(np.isnan(actual[:,2]).all())
    def test_empty_segments_no_artist(self):
        ax=Figure().subplots();self.assertIsNone(draw_segments(ax,[]));self.assertEqual(len(ax.lines),0)
if __name__=='__main__':unittest.main()
