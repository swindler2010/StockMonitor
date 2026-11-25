import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from candlepilot_health import diagnostic_scopes

class Scopes(unittest.TestCase):
    def test_partition_preserves_all_diagnoses_and_never_grants_permission(self):
        rows=[dict(isin=i,source='gettex' if v=='GETTEX' else 'proxy',venue=v,currency='EUR',symbol=i,tf='15m',state=s,issues=['original']) for i,v,s in [('A','GETTEX','INVALID'),('A','FRA','GAP'),('B','GETTEX','READY'),('X','GETTEX','GAP')]]
        rows.append(dict(isin='A',source=None,venue=None,currency=None,symbol=None,tf='5m',state='INVALID',issues=['unknown']))
        before=copy.deepcopy(rows)
        out=diagnostic_scopes(rows,{'A':{'isin':'A'},'B':{'isin':'B'}},dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX'))
        self.assertEqual([r['diagnostic_scope'] for r in rows],['current_scope','other_venue_archive','current_scope','removed_instrument','unknown_identity'])
        self.assertEqual(sum(v['series'] for v in out['groups'].values()),5)
        self.assertEqual(sum(v['problems'] for v in out['groups'].values()),4)
        for old,new in zip(before,rows):self.assertEqual(old,{k:v for k,v in new.items() if k!='diagnostic_scope'})
    def test_unapproved_policy_does_not_hide_foreign_venue(self):
        r=dict(isin='A',source='proxy',venue='FRA',currency='EUR',symbol='A.F',state='GAP')
        for policy in ({},dict(version=1,approved=False,scope='registered_stocks',exclusive_venue='GETTEX')):
            self.assertEqual(diagnostic_scopes([r],{'A':{'isin':'A'}},policy)['groups']['current_scope']['problems'],1)

if __name__=='__main__':unittest.main()
