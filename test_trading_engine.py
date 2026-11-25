import copy
import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from trading_engine import evaluate, facts, profile
from analytics import analyse, analyse_multiframe, indicators


class EngineTests(unittest.TestCase):
    def contexts(self,regime='BULLISH',trigger=True):
        f=dict(regime=regime,histogram=2.,histogram_delta=1.,rsi=62.,breakout=trigger,
            retest=False,reclaim=False,breakdown=False,bearish_retest=False,
            near_resistance=False,volume_reliable=True,relative_volume=1.5,candle_up=True,
            extended=False,room=True,two_below_slow=False,closed_below_stop=False)
        return {tf:dict(facts=copy.deepcopy(f),sufficient_history=True,ohlcv_valid=True,
            data_quality={'state':'READY'},signal_at='2026-10-06',closed_bar_count=100) for tf in ('1H','4H','1D')}
    def run_engine(self,c=None,qty=0,benchmark=None):
        return evaluate(c or self.contexts(),'1H',{'quantity':qty},benchmark)
    def test_confirmed_entry_buy(self):self.assertEqual(self.run_engine()['signal'],'BUY')
    def test_without_trigger_wait(self):self.assertEqual(self.run_engine(self.contexts(trigger=False))['signal'],'WAIT')
    def test_position_changes_only_hold_wait(self):
        c=self.contexts(trigger=False)
        self.assertEqual(self.run_engine(c,5)['signal'],'HOLD')
        self.assertEqual(self.run_engine(c,0)['signal'],'WAIT')
        self.assertEqual(self.run_engine(c,None)['position_state'],'UNKNOWN')
    def test_bearish_sell_and_oversold_no_buy(self):
        c=self.contexts('BEARISH',False)
        for v in c.values():v['facts'].update(histogram=-2,histogram_delta=-1,rsi=20,two_below_slow=True,candle_up=False)
        self.assertEqual(self.run_engine(c,5)['signal'],'SELL')
        self.assertEqual(self.run_engine(c)['signal'],'SELL')
    def test_higher_bearish_veto(self):
        c=self.contexts();c['1D']['facts']['regime']='BEARISH'
        self.assertEqual(self.run_engine(c)['signal'],'WAIT')
    def test_medium_bearish_veto(self):
        c=self.contexts();c['4H']['facts']['regime']='BEARISH'
        self.assertEqual(self.run_engine(c)['signal'],'WAIT')
    def test_unconfirmed_volume_veto(self):
        for rv in (0.,.8,1.19):
            c=self.contexts();c['1H']['facts']['relative_volume']=rv
            self.assertEqual(self.run_engine(c)['signal'],'WAIT')
    def test_unreliable_volume_explicit(self):
        c=self.contexts();c['1H']['facts'].update(volume_reliable=False,relative_volume=None)
        r=self.run_engine(c);self.assertEqual(r['signal'],'WAIT');self.assertTrue(any('Объём' in s for s in r['warnings']))
    def test_momentum_deterioration(self):
        c=self.contexts();c['1H']['facts']['histogram_delta']=-1
        self.assertEqual(self.run_engine(c)['signal'],'WAIT')
    def test_overbought_trend_not_sell(self):
        c=self.contexts(trigger=False);c['1H']['facts']['rsi']=90
        self.assertEqual(self.run_engine(c,5)['signal'],'HOLD')
    def test_oversold_recovery_requires_reclaim_and_mtf(self):
        c=self.contexts(trigger=False);c['1H']['facts'].update(rsi=29,reclaim=True)
        self.assertEqual(self.run_engine(c)['signal'],'WAIT')
        c['1H']['facts'].update(rsi=52)
        self.assertEqual(self.run_engine(c)['signal'],'BUY')
    def test_retest_entry(self):
        c=self.contexts(trigger=False);c['1H']['facts']['retest']=True
        self.assertEqual(self.run_engine(c)['signal'],'BUY')
    def test_support_break_hold_to_sell(self):
        c=self.contexts(trigger=False);self.assertEqual(self.run_engine(c,5)['signal'],'HOLD')
        c['1H']['facts'].update(breakdown=True,histogram=-1,histogram_delta=-1,rsi=35,candle_up=False)
        self.assertEqual(self.run_engine(c,5)['signal'],'SELL')
    def test_range_position_layer(self):
        c=self.contexts('RANGE',False)
        for v in c.values():v['facts'].update(histogram=0,histogram_delta=0,rsi=48,relative_volume=1.)
        self.assertEqual(self.run_engine(c)['signal'],'WAIT')
        self.assertEqual(self.run_engine(c,1)['signal'],'HOLD')
    def test_quality_gate_each_tf_and_state(self):
        for tf in ('1H','4H','1D'):
            for state in ('INVALID','GAP','STALE','SOURCE_UNAVAILABLE','RECOVERING'):
                c=self.contexts();c[tf]['data_quality']['state']=state
                r=self.run_engine(c);self.assertEqual(r['signal'],'WAIT');self.assertEqual(r['status'],'DATA BLOCKED');self.assertEqual(r['confidence'],0)
    def test_missing_tf_blocked(self):
        c=self.contexts();del c['4H'];self.assertEqual(self.run_engine(c)['status'],'DATA BLOCKED')
    def test_benchmark_score_not_order(self):
        up=self.run_engine(benchmark={'state':'OUTPERFORMING','quality':'READY'})
        down=self.run_engine(benchmark={'state':'UNDERPERFORMING','quality':'READY'})
        self.assertEqual(up['score']-down['score'],2)
        c=self.contexts(trigger=False);self.assertEqual(self.run_engine(c,benchmark={'state':'OUTPERFORMING','quality':'READY'})['signal'],'WAIT')
    def test_invalid_benchmark_excluded(self):
        r=self.run_engine(benchmark={'state':'OUTPERFORMING','quality':'INVALID'})
        self.assertEqual(r['score'],self.run_engine()['score']);self.assertTrue(r['warnings'])
    def test_extended_no_buy(self):
        c=self.contexts();c['1H']['facts']['extended']=True;self.assertEqual(self.run_engine(c)['signal'],'WAIT')
    def test_deterministic_input_unchanged(self):
        c=self.contexts();before=copy.deepcopy(c);self.assertEqual(self.run_engine(c),self.run_engine(c));self.assertEqual(c,before)
    def frame(self):
        n=90;close=100+np.arange(n)*.1+np.sin(np.arange(n))*.2
        f=pd.DataFrame(dict(open=close-.1,high=close+.3,low=close-.3,close=close,volume=np.full(n,100.),source='proxy'),index=pd.date_range('2026-01-01',periods=n,freq='h',tz='UTC'))
        f.attrs.update(timeframe='1H',data_quality={'state':'READY'})
        return f
    def test_forming_extreme_does_not_change_facts(self):
        f=self.frame();now=f.index[-1]+pd.Timedelta(minutes=30)
        a=analyse(f,now=now)[1];f.iloc[-1,f.columns.get_loc('close')]=10000;f.iloc[-1,f.columns.get_loc('high')]=11000
        b=analyse(f,now=now)[1];self.assertEqual(a['facts'],b['facts']);self.assertEqual(a['signal'],b['signal'])
    def test_internal_partial_excluded_before_indicators(self):
        f=self.frame();f['bar_complete']=True;f.iloc[40,f.columns.get_loc('bar_complete')]=False
        now=f.index[-1]+pd.Timedelta(hours=2);a=analyse(f,now=now)[1]
        f.iloc[40,f.columns.get_loc('close')]=10000;f.iloc[40,f.columns.get_loc('high')]=11000
        b=analyse(f,now=now)[1];self.assertEqual(a['facts'],b['facts'])
    def test_volume_average_excludes_current(self):
        f=self.frame();f.iloc[-1,f.columns.get_loc('volume')]=10000
        self.assertEqual(indicators(f).iloc[-1].volume_mean20,100)
    def test_features_bullish_bearish_range(self):
        f=self.frame();x=indicators(f);a=facts(x,90,120,85);self.assertEqual(a['regime'],'BULLISH')
        for col in ('open','high','low','close'):f[col]=200-f[col]
        f[['high','low']]=f[['low','high']].to_numpy();x=indicators(f)
        self.assertEqual(facts(x,50,120,45)['regime'],'BEARISH')
        f[['open','high','low','close']]=100.;self.assertEqual(facts(indicators(f),90,110,85)['regime'],'RANGE')
    def test_profile_has_three_distinct_tf(self):
        for tf in ('5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M'):self.assertEqual(len(set(profile(tf))),3)
    def market_frames(self):
        frames={}
        for tf in ('1H','4H','1D'):
            f=self.frame()
            f.index=pd.bdate_range(end='2026-01-30',periods=len(f))+pd.Timedelta(hours=9,minutes=30)
            f.attrs.update(timeframe=tf,source='proxy',symbol='TEST',currency='USD',venue='NMS',timezone='America/New_York')
            f.iloc[-1,f.columns.get_loc('close')]+=1
            f.iloc[-1,f.columns.get_loc('high')]+=1
            f.iloc[-1,f.columns.get_loc('volume')]=200
            frames[tf]=f
        return frames
    def test_full_ohlcv_pipeline_buy(self):
        r=analyse_multiframe(self.market_frames(),'1H',now='2026-02-01T00:00Z')
        self.assertEqual(r['signal'],'BUY')
        self.assertEqual(set(r['timeframe_context']),{'1H','4H','1D'})
        self.assertTrue(r['bullish_reasons'])
    def test_full_pipeline_identity_blocks(self):
        f=self.market_frames();f['4H'].attrs['currency']='EUR'
        r=analyse_multiframe(f,'1H',now='2026-02-01T00:00Z')
        self.assertEqual(r['signal'],'WAIT');self.assertEqual(r['status'],'DATA BLOCKED')
    def test_full_pipeline_bad_quality_blocks(self):
        f=self.market_frames();f['1D'].attrs['data_quality']={'state':'INVALID'}
        r=analyse_multiframe(f,'1H',now='2026-02-01T00:00Z')
        self.assertEqual(r['signal'],'WAIT');self.assertTrue(r['blockers'])
    def test_full_pipeline_breakout_needs_volume_and_closed_bar(self):
        frames=self.market_frames()
        frames['1H'].iloc[-1,frames['1H'].columns.get_loc('volume')]=100
        self.assertEqual(analyse_multiframe(frames,'1H',now='2026-02-01T00:00Z')['signal'],'WAIT')
        frames=self.market_frames();frames['1H']['bar_complete']=True
        frames['1H'].iloc[-1,frames['1H'].columns.get_loc('bar_complete')]=False
        self.assertEqual(analyse_multiframe(frames,'1H',now='2026-02-01T00:00Z')['signal'],'WAIT')
    def test_duplicate_input_returns_structured_wait(self):
        f=self.frame();f.index=pd.DatetimeIndex([f.index[0]]*len(f))
        _,r=analyse(f,now='2026-02-01T00:00Z')
        self.assertEqual(r['signal'],'WAIT');self.assertTrue(r['blockers'])
    def test_standalone_does_not_invent_other_timeframes(self):
        _,r=analyse(self.market_frames()['1H'],now='2026-02-01T00:00Z')
        self.assertEqual(r['signal'],'WAIT');self.assertEqual(r['status'],'DATA BLOCKED')

if __name__=='__main__':unittest.main()
