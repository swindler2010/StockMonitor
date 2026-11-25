import copy,json,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import calculation_window as cw
import data_quality as dq
from analytics import resample
from desktop014 import Desktop014
from storage import Store

class ReferenceBinding(unittest.TestCase):
 def setUp(self):
  self.now=pd.Timestamp('2026-10-26T20:00Z');self.frames={};self.calls=[];self.markets=[]
  self.identities={v:dict(source='proxy',symbol='TEST.'+suffix,venue=v,currency='EUR',timezone='Europe/Berlin') for v,suffix in [('GER','DE'),('FRA','F')]}
  self.info=dict(isin='TEST',eur_history_auto=True,eur_listings=[dict(m,isin='TEST',status='CONFIRMED') for m in self.identities.values()])
  for venue,meta in self.identities.items():
   idx=dq.expected_index(meta,'1D',pd.Timestamp('2020-09-29'),pd.Timestamp('2026-10-26'),self.now)
   prices=100+np.arange(len(idx))*.02+np.sin(np.arange(len(idx))*.11)+(20 if venue=='FRA' else 0)
   daily=pd.DataFrame(dict(open=prices-.1,high=prices+1,low=prices-1,close=prices,volume=100.,source='proxy'),index=idx)
   daily.attrs={**meta,'timeframe':'1D','data_quality':{'state':'READY'}}
   self.frames[venue]={t:resample(daily,'1D',t,self.now) for t in ('1D','1W','1M')}
  self.ctx=NS(user_state=lambda i:{},source_mode=lambda i:'proxy',frame_for=lambda i,t:self.frames['FRA' if t=='1W' else 'GER'].get(t,pd.DataFrame()))
 def pin(self,ctx,info,tf,required_identity=None):
  self.calls.append((tf,copy.deepcopy(required_identity)))
  return self.frames[required_identity['venue']][tf]
 def benchmark(self,ctx,info,state,daily,now,full_reference=False):
  self.markets.append((daily.attrs['venue'],full_reference))
  return dict(quality='READY',state='OUTPERFORMING',benchmark_id='INDEX:DAX',stock_identity={k:daily.attrs[k] for k in dq.IDENTITY},valid_until='2026-10-27T16:30Z')
 def assess(self,pin=None):
  with patch.object(Desktop014,'_frame_uncached',side_effect=pin or self.pin),patch.object(cw,'bounded_benchmark',side_effect=self.benchmark):
   return cw.assess_all(self.ctx,self.info,self.now)
 def test_each_existing_strategy_binds_all_daily_dependencies_to_entry_listing(self):
  before=copy.deepcopy(self.info);out=self.assess();json.dumps(out,allow_nan=False)
  for tf,venue in [('1W','FRA'),('1M','GER')]:
   c=next(c for c in out['candidates'] if c['timeframe']==tf)
   self.assertTrue(c['analysis_ready'],c);self.assertEqual(c['context'],'full_closed_reference_v1')
   self.assertFalse(c['signal_ready']);self.assertFalse(c['trade_ready'])
   self.assertTrue(all(v['identity']==self.identities[venue] and v['full_reference']['analysis_ready'] for v in c['reference_dependencies'].values()))
   self.assertEqual(c['reference_benchmark']['stock_identity'],self.identities[venue])
  self.assertEqual(self.info,before);self.assertEqual(out['timeframes']['1D']['identity'],self.identities['GER'])
  self.assertEqual(out['timeframes']['1W']['identity'],self.identities['FRA']);self.assertEqual(len(self.calls),3)
 def test_missing_or_corrupt_required_listing_cannot_borrow_other_venue(self):
  for failure in ('missing','corrupt','identity'):
   with self.subTest(failure=failure):
    def unavailable(ctx,info,tf,required_identity=None):
     frame=self.pin(ctx,info,tf,required_identity).copy()
     if failure=='missing':return pd.DataFrame()
     if failure=='corrupt':frame.loc[frame.index[-2],'high']=0
     else:frame.attrs['currency']='USD'
     return frame
    out=self.assess(unavailable)
    self.assertFalse(any(c['analysis_ready'] for c in out['candidates'] if c['timeframe'] in ('1W','1M')))
 def test_explicit_auto_opt_out_does_not_pin_another_listing(self):
  self.info['eur_history_auto']=False;out=self.assess()
  self.assertEqual(self.calls,[]);self.assertFalse(any(c['analysis_ready'] for c in out['candidates']))
 def test_new_source_reads_remain_in_revision_guard_and_old_mixed_receipt_is_stale(self):
  with tempfile.TemporaryDirectory() as folder:
   store=Store(folder);store.save_watchlist([{'isin':'TEST','name':'test'}]);scope='market:TEST:1D'
   with store.connect() as db:db.execute('INSERT INTO input_revisions(scope,revision) VALUES (?,1)',(scope,))
   def tracked(ctx,info,tf,required_identity=None):
    store.read_cache.depend(scope);return self.pin(ctx,info,tf,required_identity)
   with store.read_cache.track_inputs(isolated=True) as token:
    store.read_cache.depend('instrument:TEST');out=self.assess(tracked);out['_input_revisions']=dict(token)
   self.assertIn(scope,token)
   with store.connect() as db:
    self.assertEqual(cw.eligibility_state(db,out,self.now),'CURRENT')
    self.assertEqual(cw.eligibility_state(db,out,pd.Timestamp(out['expires_at'])),'EXPIRED')
    old=copy.deepcopy(out);old.pop('reference_binding_version')
    next(c for c in old['candidates'] if c['timeframe']=='1W')['blockers']=['MTF_IDENTITY_MISMATCH']
    self.assertEqual(cw.eligibility_state(db,old,self.now),'STALE')
    db.execute('UPDATE input_revisions SET revision=2 WHERE scope=?',(scope,))
    self.assertEqual(cw.eligibility_state(db,out,self.now),'STALE')
   store.read_cache.close()

if __name__=='__main__':unittest.main()
