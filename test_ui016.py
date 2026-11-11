"""Isolated UI regression: no feeds, model calls, updater or tunnel startup."""
import sys
from pathlib import Path
from unittest.mock import patch
import tkinter as tk
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'stock_monitor'))
import app
root=tk.Tk();root.withdraw()
with patch.object(app.StockMonitor,'refresh_all_async'),patch.object(app.StockMonitor,'ai_tick'),patch.object(app.StockMonitor,'connection_tick'):
    monitor=app.StockMonitor(root)
    assert str(monitor.timeframe_box.cget('state'))=='readonly'
    monitor.timeframe.set('1H');monitor.refresh_chart()
    monitor.symbol.set(monitor.watchlist[1]['name']);monitor.refresh_chart()
    assert monitor.timeframe.get()=='1H'
    assert monitor.store.setting('selected')['timeframe']=='1H'
    monitor.show_indicators();root.update_idletasks()
    assert any(isinstance(w,tk.Toplevel) and w.title()=='Индикаторы • все акции' for w in root.winfo_children())
    assert root.bind_class('Text','<Button-3>')
    assert root.bind_class('TEntry','<Button-3>')
    assert root.bind_class('TCombobox','<Button-3>')
    monitor.closing=True;monitor._chart_jobs.close()
    for client in (monitor.compute_chart,monitor.compute_snapshot):
        client.close()
        if client.process:
            client.process.wait(timeout=10);client.process.stdin.close();client.process.stdout.close()
    monitor.bridge.close();monitor.store.read_cache.close();root.destroy()
print('PASS: isolated UI construction, dropdown, global timeframe persistence, indicator window, text menu bindings')
