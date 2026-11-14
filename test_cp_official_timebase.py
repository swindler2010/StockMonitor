"""Conserved GETTEX redistribution, isolated SQLite, no network or orders."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
import data_quality as dq
from test_cp_tv_legacy_reference import FlatStore


class OfficialTimebase(unittest.TestCase):
 def test_ordinary_turn_conservation_provenance_and_rejection(self):
  root=Path(os.environ.get('SM_TEST_ARCHIVE',str(Path(__file__).parent.parent.parent/'СМ-Архив')))
  with tempfile.NamedTemporaryFile(dir=root,suffix='.sqlite3',delete=False) as f:path=Path(f.name)
  try:
   store=FlatStore(path);isin='US30303M1027'
   info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX')
   store.save_instrument(info)
   meta=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='FB2A.GTX',timezone='Europe/Berlin')
   official={**meta,'source':'gettex','symbol':isin}
   for base in ('5m','15m'):
    store.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=meta,
     listing_proof={**meta,'isin':isin,'status':'CONFIRMED','verified_at':'2026-11-13T12:00Z'}))
   now=pd.Timestamp('2026-11-14T08:00Z');first='2026-10-21 13:30:00';end='2026-10-21 14:15:00'
   trades=[('2026-10-21 13:31:00',100.,2.),('2026-10-21 13:36:00',101.,5.),('2026-10-21 13:41:00',99.,21.),
           ('2026-10-21 14:01:00',102.,1.),('2026-10-21 14:06:00',100.,5.),('2026-10-21 14:11:00',101.,10.)]
   raw=pd.DataFrame(trades,columns=['ts','price','volume']);raw.index=pd.to_datetime(raw.pop('ts'))
   rows={}
   for base in ('5m','15m'):
    rows[base]=[(str(at),float(p.price.iloc[0]),float(p.price.max()),float(p.price.min()),float(p.price.iloc[-1]),float(p.volume.sum()))
     for at,p in raw.groupby(pd.Grouper(freq=base.replace('m','min'))) if not p.empty]
   native={'15m':[('2026-10-21 14:00:00',100.,102.,99.,101.,44.)],
           '5m':[('2026-10-21 14:00:00',100.,102.,99.,101.,44.)]}
   proof=dict(isin=isin,identity=official,basis='complete_published_trade_files',checked_at='2026-11-13T12:00Z',
    no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261021':{}})
   for at in pd.date_range(first,end,freq='5min',inclusive='left'):
    field='no_trades' if pd.Timestamp('2026-10-21 13:45')<=at<pd.Timestamp('2026-10-21 14:00') else 'trade_intervals'
    proof[field].append([str(at),str(at+pd.Timedelta(minutes=5))])
   for tm in ('11.45','12.00','12.15'):
    for board in ('munc','mund'):proof['reference']['GETTEX:20261021']['posttrade.20261021.'+tm+'.'+board+'.csv.gz']='a'*64
   store.set_setting('interval_evidence:'+isin,proof)
   policy=dict(version=rw.RETIME_VERSION,approved=True,scope='all current and future GETTEX/EUR stocks')
   store.set_setting(rw.RETIME_POLICY,policy)
   with store.connect() as db:
    db.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*r) for r in trades])
    for base,rr in rows.items():db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in rr])
    for base,rr in native.items():db.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[meta[k] for k in rw.IDENTITY[:4]],base,*r) for r in rr])
    spec=rv._spec(isin,meta,'15m',first,'2026-10-21 13:45:00')
    job,_=rv._enqueue(db,spec,{'kind':'test'},now)
    inputs=rv._input(db,job);plan=rw.retime_plan(db,job,inputs,now)
   self.assertIsNotNone(plan)
   # Real changes, incomplete pairs, conflicting visual evidence and revocation
   # cannot be waived by the standing owner permission.
   with store.connect() as db:
    db.execute('BEGIN IMMEDIATE')
    db.execute("UPDATE candles SET high=999 WHERE base_tf='15m' AND ts=?",(first,))
    with self.assertRaisesRegex(ValueError,'TRADE_MISMATCH'):rw.publish_retime(db,job,inputs,plan,now)
    db.rollback()
   bad=copy.deepcopy(proof);bad['reference']['GETTEX:20261021'].pop('posttrade.20261021.11.45.mund.csv.gz')
   store.set_setting('interval_evidence:'+isin,bad)
   with store.connect() as db:
    with self.assertRaisesRegex(ValueError,'PAIR_MISSING'):rw.retime_plan(db,job,rv._input(db,job),now)
   store.set_setting('interval_evidence:'+isin,proof)
   with store.connect() as db:
    db.execute('BEGIN IMMEDIATE');db.execute("UPDATE series SET volume=45 WHERE base_tf='15m'")
    with self.assertRaisesRegex(ValueError,'NOT_CONSERVED'):rw.retime_plan(db,job,rv._input(db,job),now)
    db.rollback()
   # The existing CP turn, not a hand-written OHLCV mutation, applies the plan.
   with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(store,info,now)
   receipt=store.setting(rw.RETIME_PREFIX+isin+':'+job['id'])
   self.assertEqual((receipt['state'],receipt['added'],receipt['changed']),('APPLIED',6,2))
   with store.connect() as db:
    finding=rv._classify(job,rv._input(db,job),now)
    self.assertEqual(finding['state'],'NATIVE_ROWS_CONFIRMED')
   for base in ('5m','15m'):
    frame=store.load_series(isin,base,**{k:meta[k] for k in ('source','symbol','venue','currency')})
    self.assertEqual(dq.audit(frame,base,meta,now,check_stale=False)['state'],'READY')
    self.assertEqual(set(frame.origin_source),{'gettex'})
    self.assertNotIn(pd.Timestamp('2026-10-21 13:45'),frame.index)
    self.assertEqual(rw._aggregate(native[base]),rw._aggregate(rows[base]))
    with store.connect() as db:
     frozen=rw.retimed_rows(db,isin,meta,base)
     self.assertIsNone(frozen['2026-10-21 13:45:00'])
     self.assertEqual(frozen['2026-10-21 14:00:00'],list(rows[base][-3 if base=='5m' else -1][1:]))
   with store.connect() as db:
    self.assertEqual(db.execute('SELECT COUNT(*) FROM candle_revision_history').fetchone()[0],8)
    self.assertTrue(rw.retime_current(db,receipt,now))
   # Restart retains provenance. Unrelated bars do not revoke it.
   store.read_cache.close();store=FlatStore(path)
   with store.connect() as db:
    db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(isin,'gettex','15m','2026-11-13 10:00:00',1.,1.,1.,1.,1.))
    self.assertTrue(rw.retime_current(db,receipt,now))
   store.set_setting(rw.RETIME_POLICY,{**policy,'approved':False})
   with store.connect() as db:revoked=rv._input(db,job)['fingerprint']
   frame=store.load_series(isin,'15m',**{k:meta[k] for k in ('source','symbol','venue','currency')})
   self.assertEqual(dq.audit(frame,'15m',meta,now,check_stale=False)['state'],'INVALID')
   self.assertFalse(frame.attrs.get('official_retiming_empty'))
   store.set_setting(rw.RETIME_POLICY,policy)
   with store.connect() as db:self.assertNotEqual(revoked,rv._input(db,job)['fingerprint'])
   with store.connect() as db:db.execute("UPDATE trades SET price=110 WHERE ts='2026-10-21 13:31:00'")
   frame=store.load_series(isin,'15m',**{k:meta[k] for k in ('source','symbol','venue','currency')})
   self.assertEqual(dq.audit(frame,'15m',meta,now,check_stale=False)['state'],'INVALID')
  finally:
   if 'store' in locals():store.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)


if __name__=='__main__':unittest.main()

