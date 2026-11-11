import os
from pathlib import Path
import sqlite3
import sys
import json

RESOURCE_DIR=Path(__file__).resolve().parent
APP_DIR=Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else RESOURCE_DIR
distribution_file=RESOURCE_DIR/'distribution.json'
distribution_config=json.loads(distribution_file.read_text(encoding='utf-8')) if distribution_file.exists() else {}
DISTRIBUTION=distribution_config.get('channel','exe')
if DISTRIBUTION not in ('exe','msix-preview','msix-store'):raise ValueError('Unknown distribution channel')
DATA_FOLDER={'exe':'StockMonitor','msix-preview':'StockMonitor-MSIX-preview','msix-store':'StockMonitor-Store'}[DISTRIBUTION]
if DISTRIBUTION=='exe':
    profile=distribution_config.get('data_profile','development')
    if profile not in ('development','review'):raise ValueError('Unknown exe data profile')
    if profile=='review':
        if not getattr(sys,'frozen',False):raise ValueError('Review data profile requires a frozen build')
        local=os.environ.get('LOCALAPPDATA')
        if not local or not Path(local).is_absolute():raise ValueError('Absolute LOCALAPPDATA required for review data')
        DATA_DIR=Path(local)/'StockMonitor-Review'/'data'
    else:DATA_DIR=RESOURCE_DIR.parent/'test-data'
    os.environ['STOCK_MONITOR_DATA_DIR']=str(DATA_DIR)
else:
    DATA_DIR=Path(os.environ.get('STOCK_MONITOR_DATA_DIR',str(Path(os.environ.get('LOCALAPPDATA',str(APP_DIR)))/DATA_FOLDER/'data')))

def migrate_legacy():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    if DISTRIBUTION=='exe':return  # Development 0.16 never imports installed data.
    if DISTRIBUTION=='msix-preview':return
    if DISTRIBUTION=='msix-store':
        from store_release import migrate_data
        source=Path(os.environ.get('LOCALAPPDATA',str(APP_DIR)))/'StockMonitor'/'data'
        migrate_data(source,DATA_DIR,allow_import=not bool(os.environ.get('STOCK_MONITOR_DATA_DIR')))
        return
    destination=DATA_DIR/'stock_monitor.sqlite3'
    legacy=APP_DIR/'user_data'/'stock_monitor.sqlite3'
    if not destination.exists() and legacy.exists() and legacy!=destination:
        with sqlite3.connect(legacy) as src, sqlite3.connect(destination) as dst:
            cols=[r[1] for r in src.execute('PRAGMA table_info(instruments)')]
            if 'payload' not in cols:raise RuntimeError('Неподдерживаемая старая база: требуется импорт v0.10.')
            src.backup(dst)

