import sys,unittest
from pathlib import Path
import numpy as np,pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from chart_indicators import volume_profile
class VolumeTests(unittest.TestCase):
    def test_batched_parity_with_original_loop(self):
        rng=np.random.default_rng(41);low=rng.uniform(1,200,12000);high=low+rng.uniform(0,15,len(low));high[::11]=low[::11];volume=rng.uniform(0,1e6,len(low))
        f=pd.DataFrame({'low':low,'high':high,'volume':volume})
        for bins in (2,24,100):
            price,actual,width=volume_profile(f,bins);edges=np.linspace(low.min(),high.max(),bins+1);expected=np.zeros(bins)
            for bottom,top,v in f.to_numpy():
                if top==bottom:expected[min(bins-1,max(0,np.searchsorted(edges,bottom,side='right')-1))]+=v
                else:expected+=v*np.maximum(0,np.minimum(edges[1:],top)-np.maximum(edges[:-1],bottom))/(top-bottom)
            np.testing.assert_allclose(actual,expected,rtol=1e-12,atol=1e-6)
            self.assertAlmostEqual(actual.sum()/volume.sum(),1,places=12)
    def test_flat_empty_and_invalid_unchanged(self):
        f=pd.DataFrame({'low':[10.,10.],'high':[10.,10.],'volume':[2.,3.]})
        self.assertEqual(volume_profile(f)[1].sum(),5)
        self.assertEqual(len(volume_profile(f.iloc[:0])[0]),0)
        for col,val in [('volume',-1),('low',float('nan')),('high',9.)]:
            bad=f.copy();bad.loc[0,col]=val
            with self.assertRaises(ValueError):volume_profile(bad)
if __name__=='__main__':unittest.main()
