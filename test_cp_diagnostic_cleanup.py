import copy,json,sqlite3,sys,unittest
from unittest.mock import patch,MagicMock
from contextlib import contextmanager
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from cp_diagnostic_cleanup import turn


class Memory:
    def __init__(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE series(isin,source,venue,currency,symbol,base_tf,ts,close); CREATE TABLE candles(isin,source,base_tf);')
    @contextmanager
    def connect(self):
        with self.db:yield self.db
    def put(self,k,v):
        with self.connect() as db:db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(k,json.dumps(v)))
    def get(self,k):
        row=self.db.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone()
        return json.loads(row[0]) if row else None


class DiagnosticCleanup(unittest.TestCase):
    def setUp(self):
        self.store=Memory();self.addCleanup(self.store.db.close);self.info={'isin':'TEST'}
        self.meta=dict(source='proxy',venue='EBS',currency='CHF',symbol='TEST.SW',timezone=None)
        self.key='data_quality:TEST:TEST.SW:15m';self.old={'state':'INVALID','issues':['unsupported calendar'],'ranges':[]}
        self.report=dict(self.old,identity=self.meta,base_tf='15m',checked_at='2026-10-01T10:00:00+00:00')
        self.unit=json.dumps(['proxy','EBS','CHF','TEST.SW','15m'])
        with self.store.connect() as db:db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?)',('TEST','proxy','EBS','CHF','TEST.SW','15m','2026-10-01 09:00',100))
        self.store.put(self.key,self.old);self.save_archive()
    def save_archive(self):self.store.put('history_archive_progress:TEST',{'series':{self.unit:{'quality':self.report,'input_revisions':{'market:TEST:15m':1}}}})
    def test_duplicate_removed_without_changing_error_time_rows_or_permission(self):
        before=self.store.db.execute('SELECT * FROM series').fetchall()
        self.assertEqual(turn(self.store,self.info)['state'],'NORMALIZED')
        after=self.store.get(self.key);self.assertEqual(after,dict(self.old,identity=self.meta,base_tf='15m'))
        self.assertEqual(self.store.get('cp_diagnostic_cleanup:TEST')[self.key]['before'],self.old)
        self.assertEqual(before,self.store.db.execute('SELECT * FROM series').fetchall())
        self.assertEqual(turn(self.store,self.info)['state'],'NO_MATCH')
    def test_different_or_resolved_error_cannot_erase_current_failure(self):
        for change in [{'state':'READY'},{'issues':['different']},{'checked_at':'2099-01-01T00:00:00+00:00'},{'base_tf':'5m'},{'identity':{**self.meta,'currency':'EUR'}}]:
            with self.subTest(change=change):
                original=copy.deepcopy(self.report);self.report.update(change);self.save_archive()
                self.assertEqual(turn(self.store,self.info)['state'],'NO_MATCH');self.assertEqual(self.store.get(self.key),self.old)
                self.report=original
    def test_missing_or_ambiguous_series_never_guesses_identity(self):
        with self.store.connect() as db:db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?)',('TEST','proxy','FRA','EUR','TEST.SW','15m','2026-10-01 09:00',101))
        self.assertEqual(turn(self.store,self.info)['state'],'NO_MATCH')
        with self.store.connect() as db:db.execute('DELETE FROM series')
        self.assertEqual(turn(self.store,self.info)['state'],'NO_MATCH')
    def test_newer_or_addressed_diagnostic_is_untouched(self):
        for changed in [dict(self.old,checked_at='2026-10-02T10:00:00+00:00'),dict(self.old,identity=self.meta)]:
            self.store.put(self.key,changed);self.assertEqual(turn(self.store,self.info)['state'],'NO_MATCH');self.assertEqual(self.store.get(self.key),changed)
    def test_only_one_diagnostic_per_existing_writer_turn(self):
        for base in ['5m','1H']:
            with self.store.connect() as db:db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?)',('TEST','proxy','EBS','CHF','TEST.SW',base,'2026-10-01 09:00',100))
            self.store.put('data_quality:TEST:TEST.SW:'+base,self.old)
        arc={'series':{json.dumps(['proxy','EBS','CHF','TEST.SW',b]):{'quality':dict(self.report,base_tf=b)} for b in ['5m','15m','1H']}}
        self.store.put('history_archive_progress:TEST',arc)
        turn(self.store,self.info)
        self.assertEqual(len(self.store.get('cp_diagnostic_cleanup:TEST')),1)

    def test_malformed_metadata_preserves_failures_and_returns_safely(self):
        for archive in ['broken', {'series':{'not json':{}}}, {'series':{self.unit:None}}, {'series':[]}]:
            with self.subTest(archive=archive):
                with self.store.connect() as db:db.execute('UPDATE settings SET value=? WHERE key=?',(archive if isinstance(archive,str) else json.dumps(archive),'history_archive_progress:TEST'))
                self.assertEqual(turn(self.store,self.info)['state'],'UNKNOWN')
                self.assertEqual(self.store.get(self.key),self.old)
                self.assertIsNone(self.store.get('cp_diagnostic_cleanup:TEST'))
        self.save_archive()
        with self.store.connect() as db:
            db.execute('DELETE FROM settings WHERE key=?',(self.key,))
            db.execute('INSERT INTO settings VALUES (?,?)',('data_quality:TEST:broken','not json'))
        self.assertEqual(turn(self.store,self.info)['state'],'NO_MATCH')
        self.store.put('data_quality:TEST:broken',self.old)
        self.assertEqual(turn(self.store,self.info)['state'],'UNKNOWN')
        self.assertIsNone(self.store.get('cp_diagnostic_cleanup:TEST'))

    def test_feed_normalizes_only_history_turn_and_survives_unknown_metadata(self):
        from contextlib import ExitStack
        from feed_worker import FeedContext
        store=MagicMock();store.setting.return_value={}
        context=FeedContext(store)
        with ExitStack() as stack:
            stack.enter_context(patch('eur_listings.owner_history_info',side_effect=lambda info,policy:info))
            stack.enter_context(patch.object(context,'_calculate',side_effect=lambda info:{'errors':[]}))
            for target in ['history_service.inspect_changed_official_archive','cp_controls.automatic_repair_turn','cp_controls.automatic_turn','cp_controls.tv_risk_turn','cp_controls.reviewed_risk_turn','cp_reconciliation.turn']:
                stack.enter_context(patch(target,return_value={}))
            normalize=stack.enter_context(patch('cp_diagnostic_cleanup.turn',return_value={'state':'UNKNOWN'}))
            normal=context.calculate({'isin':'TEST'})
            self.assertEqual(normal['cp_local_history']['diagnostic_cleanup']['state'],'UNKNOWN')
            self.assertEqual(normal['errors'],[]);normalize.assert_called_once()
            normalize.reset_mock()
            fresh=context.calculate({'isin':'TEST','_gettex_poll_only':True})
            normalize.assert_not_called();self.assertNotIn('cp_local_history',fresh)


if __name__=='__main__':unittest.main()
