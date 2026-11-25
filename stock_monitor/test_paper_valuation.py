"""Targeted valuation checks using the saved, owner-authorized Apple fill."""
import copy,json,sqlite3,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
import data_quality as dq
import paper_account as paper
from paper_view import _auto_refresh

E=Path(__file__).resolve().parents[1]/'history64-evidence/apple-valuation30'
ISIN='US0378331005'

class ValuationTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.clock=json.loads((E/'before.json').read_text(encoding='utf8'))['at']
  cls.fixture={}
  with sqlite3.connect((E/'before.sqlite3').as_uri()+'?mode=ro',uri=True) as source:
   for table in ('instruments','settings','series','paper_accounts','paper_fills'):
    schema=source.execute('select sql from sqlite_master where type=? and name=?',('table',table)).fetchone()[0]
    if table=='instruments':rows=source.execute('select * from instruments where isin=?',(ISIN,)).fetchall()
    elif table=='settings':rows=source.execute('select * from settings where key in (?,?,?,?)',tuple(prefix+ISIN+':'+suffix for prefix,suffix in [('gettex_chart:','5m'),('gettex_chart:','15m'),('data_quality:','AAPL.GTX:5m'),('data_quality:','AAPL.GTX:15m')])).fetchall()
    elif table=='series':
     rows=[]
     for tf in ('5m','15m'):rows+=source.execute('select * from series where isin=? and source=? and venue=? and currency=? and symbol=? and base_tf=? order by ts desc limit 4',(ISIN,'gettex_chart','GETTEX','EUR','AAPL.GTX',tf)).fetchall()
    else:rows=source.execute('select * from '+table).fetchall()
    cls.fixture[table]=(schema,rows)
 def setUp(self):
  self.db=sqlite3.connect(':memory:');self.now=self.clock
  for table,(schema,rows) in self.fixture.items():
   self.db.execute(schema)
   if rows:self.db.executemany('insert into '+table+' values ('+','.join('?' for _ in rows[0])+')',rows)
  audit=dq.audit
  def frozen_audit(*args,**kwargs):
   result=audit(*args,**kwargs)
   # Only the harness clock is frozen; real DQ numeric/calendar logic is used.
   result['checked_at']=pd.Timestamp(self.now,unit='s',tz='UTC').isoformat()
   return result
  self.audit=patch.object(dq,'audit',side_effect=frozen_audit);self.audit.start()
 def tearDown(self):self.audit.stop();self.db.close()
 def mark(self,state=None):return paper.mark_positions(self.db,paper.ledger(self.db) if state is None else state,self.now)
 def test_real_quote_ready_despite_unconfirmed_archive_and_no_trade_grant(self):
  state=self.mark();position=state['positions'][0]
  self.assertEqual(position['mark_status'],'READY',position)
  self.assertEqual(position['archive_quality'],'UNCONFIRMED')
  self.assertEqual(position['mark_timeframe'],'5m')
  self.assertEqual(position['market_value'],'312.10')
  self.assertEqual(state['unrealized'],'-1.51');self.assertEqual(state['equity'],'9998.49')
  self.assertNotIn('execution_context',state)
 def test_no_writes_or_new_fills(self):
  before=self.db.total_changes;self.db.execute('pragma query_only=ON')
  self.assertEqual(self.mark()['positions'][0]['mark_status'],'READY')
  self.assertEqual(self.db.total_changes,before);self.assertEqual(self.db.execute('select count(*) from paper_fills').fetchone()[0],1)
 def test_new_closed_quote_updates_value_without_changing_cost_or_quantity(self):
  first=self.mark();row=self.db.execute("select ts,open,high,low,close,volume from series where base_tf='5m' order by ts desc limit 1").fetchone()
  stamp=str(pd.Timestamp(row[0])+pd.Timedelta(minutes=5))
  self.db.execute('insert into series(isin,source,venue,currency,symbol,base_tf,ts,open,high,low,close,volume) values (?,?,?,?,?,?,?,?,?,?,?,?)',(ISIN,'gettex_chart','GETTEX','EUR','AAPL.GTX','5m',stamp,*[v+.25 for v in row[1:5]],row[5]))
  self.now+=300;second=self.mark();p=second['positions'][0]
  self.assertEqual(p['market_value'],'312.35');self.assertEqual(p['unrealized'],'-1.26')
  self.assertEqual(p['cost'],first['positions'][0]['cost']);self.assertEqual(p['quantity'],'1')
 def test_forming_quote_is_not_used(self):
  first=self.mark();stamp=pd.Timestamp(self.now,unit='s',tz='UTC').tz_convert('Europe/Berlin').floor('5min').tz_localize(None)
  self.db.execute('insert into series(isin,source,venue,currency,symbol,base_tf,ts,open,high,low,close,volume) values (?,?,?,?,?,?,?,?,?,?,?,?)',(ISIN,'gettex_chart','GETTEX','EUR','AAPL.GTX','5m',str(stamp),400,401,399,400,2))
  self.assertEqual(self.mark()['positions'][0]['mark'],first['positions'][0]['mark'])
 def test_stale_quote_clears_old_valuation(self):
  state=self.mark();self.now+=901
  out=self.mark(state);p=out['positions'][0]
  self.assertEqual(p['mark_reason'],'PAPER_STALE_QUOTE');self.assertIsNone(out['unrealized']);self.assertIsNone(out['equity'])
  self.assertNotIn('mark',p);self.assertNotIn('market_value',p)
 def test_bad_latest_quote_does_not_fall_back_to_older_good_price(self):
  self.db.execute("update series set high=0 where base_tf='5m' and ts=(select max(ts) from series where base_tf='5m')")
  p=self.mark()['positions'][0];self.assertEqual(p['mark_reason'],'PAPER_MARK_INVALID');self.assertNotIn('mark',p)
 def test_listing_proof_change_blocks(self):
  key='gettex_chart:'+ISIN+':5m';meta=json.loads(self.db.execute('select value from settings where key=?',(key,)).fetchone()[0])
  meta['identity']['venue']='FRA';self.db.execute('update settings set value=? where key=?',(json.dumps(meta),key))
  self.assertEqual(self.mark()['positions'][0]['mark_reason'],'PAPER_IDENTITY_BLOCKED')
 def test_wrong_currency_or_provider_never_uses_gettex_price(self):
  for key,value in [('currency','USD'),('source','unconfirmed_source'),('venue','FRA')]:
   state=paper.ledger(self.db);state['positions'][0]['identity'][key]=value
   p=self.mark(state)['positions'][0];self.assertEqual(p['mark_status'],'DATA_BLOCKED');self.assertNotIn('mark',p)
 def test_execution_archive_guard_is_unchanged(self):
  info=json.loads(self.db.execute('select payload from instruments').fetchone()[0]);ident=paper.ledger(self.db)['positions'][0]['identity']
  result=dict(isin=ISIN,identity=ident,timeframe='15m',data_quality={'state':'UNCONFIRMED'},bars=[])
  from eur_listings import stored_identity
  with self.assertRaisesRegex(ValueError,'PAPER_DATA_BLOCKED'):paper.quote(info,result,self.now,stored_listing=stored_identity(self.db,info,ident,'15m'))

class RefreshTests(unittest.TestCase):
 def parent(self):
  class Parent:
   def __init__(self):self.jobs={};self.n=0;self.exists=True
   def winfo_exists(self):return self.exists
   def after(self,ms,fn):self.n+=1;self.jobs[self.n]=(ms,fn);return self.n
   def after_cancel(self,key):self.jobs.pop(key)
   def bind(self,event,fn,add):self.destroy=fn
   def fire(self):_,fn=self.jobs.pop(next(iter(self.jobs)));fn()
  return Parent()
 def test_repeated_background_schedule_and_application_close(self):
  parent=self.parent();app=SimpleNamespace(closing=False);calls=[]
  _auto_refresh(parent,app,lambda:calls.append(1));self.assertEqual(len(calls),1)
  self.assertEqual(next(iter(parent.jobs.values()))[0],5000)
  parent.fire();self.assertEqual(len(calls),2);self.assertEqual(len(parent.jobs),1)
  app.closing=True;parent.fire();self.assertEqual(len(calls),2);self.assertFalse(parent.jobs)
 def test_destroy_cancels_timer_but_child_destroy_does_not(self):
  parent=self.parent();_auto_refresh(parent,SimpleNamespace(closing=False),lambda:None)
  parent.destroy(SimpleNamespace(widget=object()));self.assertEqual(len(parent.jobs),1)
  parent.destroy(SimpleNamespace(widget=parent));self.assertFalse(parent.jobs)

if __name__=='__main__':unittest.main()
