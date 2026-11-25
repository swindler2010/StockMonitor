import sys,tempfile,unittest,sqlite3,threading,ast
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import matplotlib
matplotlib.use('Agg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
import pandas as pd
from storage import Store
from chart_overlay import Overlay
from snapshot_worker import calculate
class Tests(unittest.TestCase):
 def test_candle_close_invalidates_analysis_without_database_change(self):
  from unittest.mock import patch
  from desktop014 import Desktop014
  with tempfile.TemporaryDirectory(dir='work') as d:
   store=Store(d);app=Desktop014();app.store=store
   frame=pd.DataFrame(dict(open=[1.],high=[2.],low=[1.],close=[1.5],volume=[10.]),index=pd.to_datetime(['2026-01-01T10:00:00Z']))
   frame.attrs['timeframe']='15m'
   before=pd.Timestamp('2026-01-01T10:14:00Z');after=pd.Timestamp('2026-01-01T10:16:00Z')
   try:
    with patch('analytics.bar_ends',return_value=pd.DatetimeIndex(['2026-01-01T10:15:00Z'])),patch('desktop014.analyse',return_value='computed') as compute,patch('desktop014.pd.Timestamp') as clock:
     clock.now.return_value=before
     app.analyse_cached({'isin':'x'},frame,{})
     app.analyse_cached({'isin':'x'},frame,{})
     self.assertEqual(compute.call_count,1)
     clock.now.return_value=after
     app.analyse_cached({'isin':'x'},frame,{})
     self.assertEqual(compute.call_count,2)
   finally:store.read_cache.close()
 def test_motion_coalesces_and_release_flushes(self):
  from desktop014 import Desktop014
  app=Desktop014();queued={};app.root=SimpleNamespace(after=lambda delay,fn:queued.setdefault('timer',fn) and 'timer',after_cancel=lambda name:queued.pop(name,None))
  seen=[];app._process_motion=lambda event:seen.append(event);app.drag=None
  for i in range(100):app.on_motion(i)
  self.assertEqual(len(queued),1);self.assertEqual(seen,[])
  app.on_release(None);self.assertEqual(seen,[99]);self.assertIsNone(app._motion_timer)

 def test_analysis_reuse_preserves_results(self):
  from desktop014 import Desktop014
  from analytics import analyse
  with tempfile.TemporaryDirectory(dir='work') as d:
   store=Store(d);app=Desktop014();app.store=store
   frame=pd.DataFrame(dict(open=[1.]*40,high=[2.]*40,low=[1.]*40,close=[1.5]*40,volume=[10.]*40),index=pd.date_range('2026-01-01',periods=40,freq='15min'))
   frame['source']='gettex';info={'isin':'x'}
   expected,signal=analyse(frame,{})
   for _ in range(2):
    actual,result=app.analyse_cached(info,frame,{})
    pd.testing.assert_frame_equal(actual,expected);self.assertEqual(result,signal)
   frame.iloc[-1,frame.columns.get_loc('close')]=1.8
   expected,signal=analyse(frame,{})
   actual,result=app.analyse_cached(info,frame,{})
   pd.testing.assert_frame_equal(actual,expected);self.assertEqual(result,signal);store.read_cache.close()

 def test_cache_external_commit_and_copy(self):
  with tempfile.TemporaryDirectory(dir='work') as d:
   store=Store(d);frame=pd.DataFrame(dict(open=[1.],high=[2.],low=[1.],close=[2.],volume=[10.]),index=pd.to_datetime(['2026-01-01']))
   store.save_frame('x','gettex','15m',frame)
   first=store.load_official('x');first.iloc[0,0]=99
   self.assertEqual(store.load_official('x').iloc[0].open,1)
   with closing(sqlite3.connect(store.path)) as db:
    db.execute("UPDATE candles SET close=1.5 WHERE isin='x'");db.commit()
   self.assertEqual(store.load_official('x').iloc[0].close,1.5)
   store.read_cache.close()
 def test_hover_no_full_draw_and_invalidation(self):
  fig=Figure();canvas=FigureCanvasAgg(fig);ax=fig.subplots();overlay=Overlay(canvas,[ax],'grey','black','white');canvas.draw()
  draws=[];canvas.mpl_connect('draw_event',lambda e:draws.append(1))
  for i in range(100):overlay.show(i/100,.5,'same OHLC')
  self.assertEqual(len(draws),0);self.assertEqual(len(ax.lines),2)
  ax.set_xlim(0,5);self.assertIsNone(overlay.background);overlay.show(1,.5,'new');self.assertEqual(len(draws),1)
  overlay.hide();self.assertTrue(all(not a.get_visible() for a in overlay.artists))
  overlay.close();self.assertEqual(len(ax.lines),0)
 def test_snapshot_worker_no_tk_and_fresh(self):
  with tempfile.TemporaryDirectory(dir='work') as d:
   store=Store(d);frame=pd.DataFrame(dict(open=[1.]*40,high=[2.]*40,low=[1.]*40,close=[1.5]*40,volume=[10.]*40),index=pd.date_range('2026-01-01',periods=40,freq='15min'))
   store.save_frame('x','gettex','15m',frame)
   args=dict(watchlist=[dict(isin='x',name='X')],drafts={},feed_errors={},symbol='X',timeframe='15m',period='5 лет',ai_config=dict(provider='local'))
   results=[];errors=[]
   def run():
    try:results.append(calculate(store,args,0))
    except Exception as e:errors.append(e)
   t=threading.Thread(target=run);t.start();t.join(10)
   self.assertFalse(t.is_alive());self.assertEqual(errors,[]);self.assertTrue(results[0]['ready']);self.assertEqual(results[0]['stocks'][0]['count'],40)
   self.assertEqual(results[0]['selected']['isin'],'x');store.read_cache.close()
 def test_syntax(self):
  for file in (Path(__file__).parent/'stock_monitor').glob('*.py'):ast.parse(file.read_text(encoding='utf-8-sig'))
if __name__=='__main__':unittest.main()
