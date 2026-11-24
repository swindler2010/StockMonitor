import copy
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from premarket_runtime import parse,fetch,present,download,PREFIX,COOLDOWN
from news_runtime import NewsRuntime

INFO=dict(isin='fixture',name='Apple',profile_symbol='AAPL',currency='EUR')
NOW=1795520000


def response():
    return {'chart':{'result':[{'meta':dict(symbol='AAPL',instrumentType='EQUITY',currency='USD',exchangeName='NMS',hasPrePostMarketData=True,
                 regularMarketPrice=100,regularMarketTime=NOW-9000,currentTradingPeriod={'pre':dict(start=NOW-3600,end=NOW+3600)}),
                 'timestamp':[NOW-120,NOW-60], 'indicators':{'quote':[{'close':[101,102]}]}}]}}


class ParseTests(unittest.TestCase):
    def test_only_premarket_same_symbol_same_array_and_source_currency(self):
        raw=response();packet=parse(INFO,raw,NOW)
        self.assertEqual(packet['status'],'AVAILABLE');self.assertAlmostEqual(packet['quote']['change'],2)
        self.assertEqual(packet['currency'],'USD');self.assertEqual(packet['quote']['at'],NOW-60)
        raw['chart']['result'][0]['meta']['symbol']='OTHER'
        with self.assertRaisesRegex(ValueError,'LISTING_MISMATCH'):parse(INFO,raw,NOW)
        raw=response();raw['chart']['result'][0]['indicators']['quote'][0]['close'].pop()
        with self.assertRaisesRegex(ValueError,'INCOMPLETE_PUBLICATION'):parse(INFO,raw,NOW)

    def test_future_post_and_invalid_prices_never_become_a_premarket_quote(self):
        for timestamps,prices in [([NOW+1,NOW+2],[1,2]),([NOW+3700,NOW+3800],[1,2]),([NOW-2,NOW-1],[None,float('nan')])]:
            raw=response();raw['chart']['result'][0].update(timestamp=timestamps,indicators={'quote':[{'close':prices}]})
            packet=parse(INFO,raw,NOW);self.assertIsNone(packet['quote']);self.assertEqual(packet['status'],'NO_PREMARKET_BARS')
        raw=response();raw['chart']['result'][0]['meta']['hasPrePostMarketData']=False
        self.assertEqual(parse(INFO,raw,NOW)['status'],'NOT_PROVIDED')

    def test_age_changes_display_without_renewing_source_timestamp_or_using_fx(self):
        packet=parse(INFO,response(),NOW);original=copy.deepcopy(packet)
        title,detail,color=present(INFO,packet,NOW+1000)
        self.assertIn('USD',title);self.assertIn('Архив',title);self.assertIsNone(color)
        self.assertEqual(packet,original)
        self.assertIn('нет данных',present({**INFO,'profile_symbol':'OTHER'},packet,NOW)[0])

    def test_errors_keep_original_quote_and_retry_after_is_respected(self):
        old=parse(INFO,response(),NOW);old['next_retry_at']=NOW-1
        transport=Mock(side_effect=HTTPError('https://query1.finance.yahoo.com',429,'limited',{'Retry-After':'7200'},io.BytesIO()))
        packet,cooldown=fetch(INFO,old,{},now=NOW,transport=transport)
        self.assertEqual(packet['quote'],old['quote']);self.assertEqual(cooldown['retry_at'],NOW+7200)
        fetch(INFO,packet,cooldown,now=NOW+60,transport=transport);self.assertEqual(transport.call_count,1)
        packet,_=fetch(INFO,old,{},now=NOW,transport=Mock(side_effect=RuntimeError('network')))
        self.assertEqual(packet['status'],'SOURCE_ERROR');self.assertEqual(packet['quote']['at'],NOW-60)

    def test_transport_has_verified_ssl_default_limits_and_rejects_redirect(self):
        r=Mock(status=200);r.geturl.return_value='https://query1.finance.yahoo.com/v8/finance/chart/AAPL'
        r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False);r.read.side_effect=[b'{"chart":{}}',b'']
        opener=SimpleNamespace(open=Mock(return_value=r));self.assertEqual(download('AAPL',opener=opener),{'chart':{}})
        request=opener.open.call_args.args[0];self.assertIn('includePrePost=true',request.full_url)
        self.assertEqual(opener.open.call_args.kwargs['timeout'],12)
        r.geturl.return_value='https://other.example/a'
        with self.assertRaisesRegex(ValueError,'SOURCE_REDIRECT'):download('AAPL',opener=opener)


class RuntimeTests(unittest.TestCase):
    def make(self):
        self.jobs=[];self.settings={}
        self.app=SimpleNamespace(closing=False,watchlist=[copy.deepcopy(INFO)],symbol=SimpleNamespace(get=lambda:'Apple'),
            root=SimpleNamespace(after=lambda *a:1,after_cancel=Mock()),main_screen=SimpleNamespace(load_premarket=Mock()),
            store=SimpleNamespace(setting=lambda key,default=None:self.settings.get(key,default),set_setting=Mock()),
            job=lambda *args,**kw:self.jobs.append((args,kw)) or True)
        return NewsRuntime(self.app)

    def test_existing_news_lane_same_mapping_and_no_identical_cache_writes(self):
        runtime=self.make();packet=parse(INFO,response(),NOW);self.settings[PREFIX+INFO['isin']]=packet
        with patch('premarket_runtime.fetch',return_value=(packet,{})):
            self.assertTrue(runtime.refresh_premarket());args,kw=self.jobs.pop();self.assertEqual(kw['lane'],'news')
            self.assertTrue(runtime.busy);self.assertFalse(runtime.refresh_premarket())
            args[1](args[0]());self.assertFalse(runtime.busy);self.app.store.set_setting.assert_not_called()
        self.app.main_screen.load_premarket.assert_called_once_with(self.app.watchlist[0],force=True)

    def test_new_selected_stock_gets_one_turn_before_background_news(self):
        runtime=self.make();runtime.maintenance_next=float('inf')
        runtime.refresh_premarket=Mock(return_value=True)
        with patch('news_runtime.due_targets',side_effect=AssertionError('selected stock should be first')):runtime.tick()
        runtime.refresh_premarket.assert_called_once();self.assertEqual(runtime.premarket_selection,'Apple')

    def test_53_and_200_old_mapping_replies_never_publish(self):
        for count in (53,200):
            runtime=self.make()
            for i in range(count):
                self.app.watchlist=[copy.deepcopy(INFO)];runtime.refresh_premarket();args,_=self.jobs.pop()
                self.app.watchlist[0]['profile_symbol']='OTHER'+str(i)
                packet=parse(INFO,response(),NOW);args[1]((packet,{},None,{}))
            self.app.store.set_setting.assert_not_called();self.app.main_screen.load_premarket.assert_not_called()

    def test_failed_job_unblocks_sequential_worker_and_keeps_paper_untouched(self):
        runtime=self.make();runtime.refresh_premarket();args,_=self.jobs.pop();args[2]('network')
        self.assertFalse(runtime.busy);self.assertGreater(runtime.premarket_next,0)
        self.app.store.set_setting.assert_not_called()

    def test_53_and_200_old_display_reads_and_force_refresh_are_rejected(self):
        from main_screen import Screen
        for count in (53,200):
            jobs=[];screen=Screen.__new__(Screen)
            screen._closed=False;screen._premarket_key=None
            screen.premarket=Mock();screen.premarket_label=Mock()
            screen.app=SimpleNamespace(job=lambda *args,**kw:jobs.append(args) or True)
            for i in range(count):
                screen.load_premarket(INFO,force=True);old=jobs.pop()
                screen.load_premarket({**INFO,'isin':'other','profile_symbol':'OTHER'},force=True);new=jobs.pop()
                screen.premarket.reset_mock();old[1]({'packet':{}})
                screen.premarket.set.assert_not_called()
                with patch('premarket_runtime.present',return_value=('CURRENT','current',None)):
                    new[1]({'packet':{}})
                screen.premarket.set.assert_called_once_with('CURRENT')

    def test_display_queue_delay_rechecks_price_age_at_publication(self):
        from main_screen import Screen
        import time
        jobs=[];screen=Screen.__new__(Screen);screen._closed=False;screen._premarket_key=None
        screen.premarket=Mock();screen.premarket_label=Mock()
        screen.app=SimpleNamespace(job=lambda *args,**kw:jobs.append(args) or True)
        screen.load_premarket(INFO,force=True)
        packet=parse(INFO,response(),NOW)
        with patch('premarket_runtime.time.time',return_value=NOW+1000):jobs[-1][1]({'packet':packet})
        self.assertIn('Архив',screen.premarket.set.call_args.args[0])
        self.assertEqual(screen.premarket_label.configure.call_args.kwargs['foreground'],'#94a3b8')


if __name__=='__main__':unittest.main()
