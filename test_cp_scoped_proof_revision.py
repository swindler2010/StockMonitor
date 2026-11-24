"""Global source proof revisions must not revoke an unchanged exact TV scope."""
import copy,json,sqlite3,sys,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_controls as cp,cp_reconciliation as rv

class ScopedProof(unittest.TestCase):
 def test_scoped_proof_refresh_is_readonly_and_material_changes_remain_strict(self):
  for tf in ('5m','15m'):
   with self.subTest(tf=tf),sqlite3.connect(':memory:') as db,patch('eur_listings.stored_identity',return_value=dict(source='gettex_chart',symbol='FIX.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')):
    db.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT); CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER); CREATE TABLE series(isin,source,venue,currency,symbol,base_tf,ts,open,high,low,close,volume);')
    isin='US67066G1040';meta=dict(source='gettex_chart',symbol='FIX.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin');now=pd.Timestamp.now(tz='UTC');first=pd.Timestamp('2026-11-13 10:00:00');step=pd.Timedelta(minutes=5 if tf=='5m' else 15)
    info=dict(isin=isin,kind='stock');db.execute('INSERT INTO instruments VALUES (?,?)',(isin,json.dumps(info)))
    def add(at):db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(isin,'gettex_chart','GETTEX','EUR','FIX.GTX',tf,str(at),100.,101.,99.,100.,10.))
    add(first-step);add(first+step)
    spec=rv._spec(isin,meta,tf,str(first),str(first));proof=dict(isin=isin,identity={**meta,'source':'gettex','symbol':isin},basis='complete_published_trade_files',checked_at=(now-pd.Timedelta(minutes=2)).isoformat(),reference={'GETTEX:20261113':{'fixture':'a'*64}},no_trades=[[str(first),str(first+step)]],trade_intervals=[],zero_quantity_intervals=[])
    proof_key='interval_evidence:'+isin;cp.write(db,proof_key,proof)
    report_key='data_quality:'+isin+':FIX.GTX:'+tf;cp.write(db,report_key,dict(state='UNCONFIRMED',identity=meta,base_tf=tf,ranges=[[str(first),str(first)]]))
    cp.write(db,cp.TV_POLICY,dict(version=cp.TV_VERSION,approved=True,enabled=True))
    inputs=rv._input(db,spec);job={**spec,'observation':dict(result='ABSENT',identity_confirmed=True,tv_symbol='GETTEX:FIX',at=(now-pd.Timedelta(minutes=1)).isoformat(),input_fingerprint=inputs['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION)};cp.write(db,rv.key(job),job)
    p=dict(id='scope',isin=isin,kind='archive',identity=meta,tf=tf,range=[str(first),str(first)],report_key=report_key,state='UNCONFIRMED')
    scopes=['instrument:'+isin,'market:'+isin+':'+tf,'setting:'+report_key,'setting:gettex_chart:'+isin+':'+tf,'setting:'+cp.TV_POLICY,'setting:'+rv.key(job),'setting:'+proof_key,'setting:history_identity:'+isin]
    for scope in scopes:db.execute('INSERT INTO input_revisions VALUES (?,1)',(scope,))
    entry=dict(actor=cp.TV_ACTOR,problem=p,accepted_at='original',valid_until='preserved',input_revisions={s:1 for s in scopes},evidence=dict(native_binding=cp._tv_native_binding(job,rv._input(db,job)),native_binding_version=2,job_ids=[job['id']]))
    grant=dict(accepted={'scope':entry});original=copy.deepcopy(grant)
    self.assertEqual(set(cp.effective_accepted(db,isin,grant)),{'scope'})
    refreshed={**proof,'checked_at':now.isoformat(),'no_trades':proof['no_trades']+[['2026-11-13 13:00:00','2026-11-13 13:05:00']]};cp.write(db,proof_key,refreshed);db.execute('UPDATE input_revisions SET revision=2 WHERE scope=?',('setting:'+proof_key,));add(pd.Timestamp('2026-11-16 10:00:00'));db.execute('UPDATE input_revisions SET revision=2 WHERE scope=?',('market:'+isin+':'+tf,));db.commit()
    changes=db.total_changes;db.execute('PRAGMA query_only=ON');self.assertEqual(set(cp.effective_accepted(db,isin,grant)),{'scope'});self.assertEqual(db.total_changes,changes);self.assertEqual(grant,original);db.execute('PRAGMA query_only=OFF')
    for change in ('trade','file','closure','unknown_proof','future_proof','unbound_observation','rebound_observation','bar','neighbour','policy','dq','listing','unknown_scope'):
     with self.subTest(reject=change):
      db.execute('SAVEPOINT negative');candidate=copy.deepcopy(grant);bad=copy.deepcopy(refreshed)
      if change=='trade':bad['trade_intervals']=[[str(first),str(first+step)]]
      elif change=='file':bad['reference']['GETTEX:20261113']['fixture']='b'*64
      elif change=='closure':bad['exchange_closures']=[dict(start=str(first),end=str(first+step),id='changed')]
      elif change=='unknown_proof':bad['basis']='unknown'
      elif change=='future_proof':bad['checked_at']=(now+pd.Timedelta(days=1)).isoformat()
      elif change in ('unbound_observation','rebound_observation'):
       altered=copy.deepcopy(job);altered['observation']['input_fingerprint']='unproved'
       if change=='rebound_observation':altered['observation']['at']=now.isoformat()
       cp.write(db,rv.key(job),altered)
      elif change=='bar':add(first)
      elif change=='neighbour':db.execute('UPDATE series SET high=102 WHERE ts=?',(str(first+step),))
      elif change=='unknown_scope':candidate['accepted']['scope']['input_revisions']['setting:unknown']=1
      else:
       key={'policy':'setting:'+cp.TV_POLICY,'dq':'setting:'+report_key,'listing':'setting:gettex_chart:'+isin+':'+tf}[change];db.execute('UPDATE input_revisions SET revision=2 WHERE scope=?',(key,))
      if change in ('trade','file','closure','unknown_proof','future_proof'):cp.write(db,proof_key,bad)
      self.assertFalse(cp.effective_accepted(db,isin,candidate));db.execute('ROLLBACK TO negative');db.execute('RELEASE negative')
    self.assertEqual(set(cp.effective_accepted(db,isin,grant)),{'scope'});self.assertEqual(grant,original)

if __name__=='__main__':unittest.main()
