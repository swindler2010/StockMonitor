"""A new unrelated native tail must not revoke an unchanged reviewed gap."""
import copy,json,sqlite3,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_controls as cp
import cp_reconciliation as rv

class ScopedTail(unittest.TestCase):
 def test_market_only_change_rechecks_exact_scope_without_renewing_permission(self):
  for actor in (cp.TV_ACTOR,cp.REVIEWED_ACTOR):
   with self.subTest(actor=actor),sqlite3.connect(':memory:') as db:
    db.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT); CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER); CREATE TABLE series(isin,source,venue,currency,symbol,base_tf,ts,open,high,low,close,volume);')
    isin='US67066G1040';info=dict(isin=isin,kind='stock');meta=dict(source='gettex_chart',symbol='NVDA.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
    db.execute('INSERT INTO instruments VALUES (?,?)',(isin,json.dumps(info)))
    def add(ts):db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(isin,'gettex_chart','GETTEX','EUR','NVDA.GTX','15m',ts,100.,101.,99.,100.,10.))
    add('2026-10-21 13:15:00');add('2026-10-21 13:45:00')
    p=dict(id='p',isin=isin,kind='archive',identity=meta,tf='15m',range=['2026-10-21 13:30:00']*2,report_key='data_quality:'+isin+':NVDA.GTX:15m',state='UNCONFIRMED')
    cp.write(db,p['report_key'],dict(state='UNCONFIRMED',identity=meta))
    scopes=['instrument:'+isin,'market:'+isin+':15m','setting:'+p['report_key'],'setting:gettex_chart:'+isin+':15m']
    with patch('eur_listings.stored_identity',return_value=dict(meta)):
     if actor==cp.REVIEWED_ACTOR:
      binding=cp._reviewed_binding(db,info,p)
      policy=dict(version=cp.REVIEWED_VERSION,approved=True,entries={'p':dict(problem=p,native_binding=binding)})
      cp.write(db,cp.REVIEWED_PREFIX+isin,policy);scopes+=['setting:'+cp.REVIEWED_PREFIX+isin]
      evidence=dict(native_binding=binding,policy_digest=cp.digest(policy))
     else:
      cp.write(db,cp.TV_POLICY,dict(version=cp.TV_VERSION,approved=True,enabled=True))
      job=rv._spec(isin,meta,'15m',p['range'][0],p['range'][1]);job['observation']=dict(result='ABSENT',identity_confirmed=True,tv_symbol='GETTEX:NVD')
      cp.write(db,rv.key(job),job);scopes+=['setting:'+cp.TV_POLICY,'setting:'+rv.key(job),'setting:interval_evidence:'+isin,'setting:history_identity:'+isin]
      evidence=dict(native_binding=cp._tv_native_binding(job,rv._input(db,job)),native_binding_version=2,job_ids=[job['id']])
     for s in scopes:db.execute('INSERT INTO input_revisions VALUES (?,1)',(s,))
     grant=dict(accepted={'p':dict(actor=actor,problem=p,evidence=evidence,input_revisions={s:1 for s in scopes})})
     before=copy.deepcopy(grant)
     self.assertEqual(set(cp.effective_accepted(db,isin,grant)),{'p'})
     add('2026-11-13 16:00:00');db.execute('UPDATE input_revisions SET revision=2 WHERE scope=?',('market:'+isin+':15m',))
     self.assertEqual(set(cp.effective_accepted(db,isin,grant)),{'p'})
     self.assertEqual(grant,before) # No token/expiry/grant write or new permission.
     for strict in [s for s in scopes if not s.startswith('market:')]+['setting:unknown']:
      with self.subTest(strict=strict):
       candidate=copy.deepcopy(grant)
       if strict not in scopes:candidate['accepted']['p']['input_revisions'][strict]=1
       else:db.execute('UPDATE input_revisions SET revision=2 WHERE scope=?',(strict,))
       self.assertFalse(cp.effective_accepted(db,isin,candidate))
       if strict in scopes:db.execute('UPDATE input_revisions SET revision=1 WHERE scope=?',(strict,))
     db.execute("UPDATE series SET high=102 WHERE ts='2026-10-21 13:45:00'")
     self.assertFalse(cp.effective_accepted(db,isin,grant))
     db.execute("UPDATE series SET high=101 WHERE ts='2026-10-21 13:45:00'")
     add(p['range'][0]);self.assertFalse(cp.effective_accepted(db,isin,grant))

if __name__=='__main__':unittest.main()
