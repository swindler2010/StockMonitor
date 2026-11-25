import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent/'stock_monitor'))
from chart_worker import LatestRequest

class LatestChartTests(unittest.TestCase):
    def test_fullscreen_restores_late_created_benchmark_row_before_chart(self):
        import tkinter as tk
        from tkinter import ttk
        from types import SimpleNamespace
        from unittest.mock import Mock
        from extras016 import Extras016
        root=tk.Tk();root.withdraw()
        try:
            outer=ttk.Frame(root);outer.pack(fill='both',expand=True)
            toolbar=ttk.Frame(outer);toolbar.pack(fill='x')
            body=ttk.Panedwindow(outer,orient='horizontal');body.pack(fill='both',expand=True)
            body.add(ttk.Frame(body));side=ttk.Frame(body);body.add(side)
            benchmark=ttk.Frame(outer);benchmark.pack(fill='x',before=body)
            before=outer.pack_slaves()
            ui=SimpleNamespace(root=root,chart_outer=outer,chart_body=body,chart_side=side,canvas=Mock())
            Extras016.toggle_chart_fullscreen(ui)
            Extras016.toggle_chart_fullscreen(ui)
            self.assertEqual(outer.pack_slaves(),before)
            self.assertLess(outer.pack_slaves().index(benchmark),outer.pack_slaves().index(body))
            if ui._sash_restore_timer:root.after_cancel(ui._sash_restore_timer)
        finally:root.destroy()

    def test_separate_process_reads_only_and_returns_current_chart(self):
        import tempfile,sqlite3
        import pandas as pd
        from compute_process import Client
        from storage import Store
        import benchmarks as bm
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);info=bm.CATALOG['INDEX:SP500']
            frame=pd.DataFrame({'open':[100.],'high':[102.],'low':[99.],'close':[101.],'volume':[10.]},index=pd.to_datetime(['2026-10-02']))
            frame.attrs=bm.identity(info);store.save_series(info,frame,'1D')
            values={'watchlist':[info],'drafts':{},'feed_errors':{},'symbol':info['name'],'timeframe':'1D','period':'Max','ai_config':{}}
            client=Client(store.path)
            try:
                result=client.calculate('chart',values)
                self.assertEqual(result['context']['benchmark_id'],None)
                self.assertNotEqual(client.process.pid,__import__('os').getpid())
                with store.connect() as db:
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM series').fetchone()[0],1)
                # Opening/using the worker never creates a journal record or settings.
                with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM settings').fetchone()[0],0)
            finally:
                client.close()
                if client.process:
                    client.process.wait(timeout=10);client.process.stdin.close();client.process.stdout.close()
                store.read_cache.close()

    def setUp(self):
        self.jobs=[];self.results=[];self.errors=[]
        self.queue=LatestRequest(lambda *args:self.jobs.append(args),self.results.append,self.errors.append)

    def test_burst_coalesces_and_never_applies_old_instrument_tf_or_benchmark(self):
        for number in range(30):self.queue.request(lambda n=number:n)
        self.assertEqual(len(self.jobs),1)
        work,done,_=self.jobs[0];done(work())
        self.assertEqual(self.results,[]);self.assertEqual(len(self.jobs),2)
        work,done,_=self.jobs[1];done(work())
        self.assertEqual(self.results,[29]);self.assertFalse(self.queue.running)

    def test_close_discards_completion_and_pending_work(self):
        self.queue.request(lambda:'old');self.queue.request(lambda:'new')
        self.queue.close();self.jobs[0][1]('old')
        self.assertEqual(self.results,[]);self.assertEqual(len(self.jobs),1)

    def test_old_error_does_not_replace_new_chart(self):
        self.queue.request(lambda:1);self.queue.request(lambda:2)
        self.jobs[0][2]('old failure');self.assertEqual(self.errors,[])
        self.jobs[1][1](2);self.assertEqual(self.results,[2])

if __name__=='__main__':unittest.main()
