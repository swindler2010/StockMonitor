import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from tunnel_runtime import health_ready
class HealthFreshnessTests(unittest.TestCase):
    def payload(self,value):return {'result':'ok','readyz':{'ok':True},'control_plane_poll':{'ok':True,'value':value}}
    def test_recent_cloud_poll(self):
        for value in (1000,970,820):self.assertTrue(health_ready(self.payload(value),now=1000))
    def test_stale_local_ready_not_connected(self):
        self.assertFalse(health_ready(self.payload(600),now=1000))
    def test_invalid_and_future(self):
        for value in (None,True,'invalid',float('nan'),float('inf'),1006):
            self.assertFalse(health_ready(self.payload(value),now=1000))
    def test_failed_or_missing_poll(self):
        self.assertFalse(health_ready({'result':'ok','readyz':{'ok':True}},now=1000))
        p=self.payload(1000);p['control_plane_poll']['ok']=False;self.assertFalse(health_ready(p,now=1000))
if __name__=='__main__':unittest.main()
