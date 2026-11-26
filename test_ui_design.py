"""Targeted viewport/volume/logo regression; no market or trading writes."""
import sys,unittest,io,tempfile,json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch,Mock
import pandas as pd
import numpy as np
from PIL import Image
from matplotlib.figure import Figure
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from chart_viewport import rebase
from chart_modes import draw_volume,fit_volume
import logos016 as logos


class DesignTests(unittest.TestCase):
    def test_quality_refresh_keeps_manual_view_and_updates_chart(self):
        from desktop014 import Desktop014
        view=((10.,20.),(100.,120.));key=('stock','1H','Max','proxy')
        app=NS(closing=False,quality_signature='old',current_quality_signature=lambda:'new',
               render_key=key,user_view=view,refresh_chart=Mock(),root=NS(after=Mock()),quality_tick=Mock())
        Desktop014.quality_tick(app)
        self.assertEqual(app.user_view,view);self.assertEqual(app.render_key,key)
        app.refresh_chart.assert_called_once();app.root.after.assert_called_once()

    def test_zoom_tracks_same_dates_when_older_bars_are_added(self):
        old=pd.date_range('2026-11-01',periods=20,freq='h');new=pd.date_range('2026-10-31',periods=50,freq='h')
        view=((4.25,12.75),(10.,20.))
        self.assertEqual(rebase(view,old,new),((28.25,36.75),view[1]))
        self.assertEqual(rebase(view,old,old.append(pd.date_range(old[-1]+pd.Timedelta(hours=1),periods=3,freq='h'))),view)
        self.assertIsNone(rebase(None,old,new))

    def test_volume_exact_values_mask_and_zoom_rescale(self):
        f=pd.DataFrame(dict(open=[2,4,3,2],close=[3,2,4,3],volume=[1000000,10,20,30],chart_segment=[1,1,-1,1]))
        ax=Figure().subplots();before=f.copy(deep=True)
        draw_volume(ax,f,f.chart_segment.to_numpy()>0)
        self.assertEqual(len(ax.patches),2)
        heights=[p.get_path().vertices.reshape(-1,5,2)[:,2,1].tolist() for p in ax.patches]
        self.assertEqual(heights,[[1000000,30],[10]])
        fit_volume(ax,f,(.5,3.5));self.assertAlmostEqual(ax.get_ylim()[1],33.6)
        ax.plot([1,2,3],[40,50,60]);fit_volume(ax,f,(.5,3.5));self.assertAlmostEqual(ax.get_ylim()[1],67.2)
        fit_volume(ax,f,(-1,3.5));self.assertAlmostEqual(ax.get_ylim()[1],1120000)
        pd.testing.assert_frame_equal(f,before)

    def test_icon_redirect_uses_bounded_public_destination(self):
        first=Mock(is_redirect=True,headers={'Location':'/assets/favicon.png'})
        second=Mock(is_redirect=False);second.iter_content.return_value=[b'png']
        first.__enter__=Mock(return_value=first);first.__exit__=Mock(return_value=False)
        second.__enter__=Mock(return_value=second);second.__exit__=Mock(return_value=False)
        with patch.object(logos,'public_url',side_effect=lambda u:u) as check,patch('requests.get',side_effect=[first,second]) as get:
            self.assertEqual(logos.download('https://company.example/favicon.ico'),(b'png','https://company.example/assets/favicon.png'))
        self.assertEqual(check.call_count,2);self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_icon_fallback_decodes_real_image_and_retains_source(self):
        image=Image.new('RGB',(48,48),'red');stream=io.BytesIO();image.save(stream,format='PNG')
        with tempfile.TemporaryDirectory(prefix='logos-') as folder:
            with patch.object(logos,'public_url',side_effect=lambda u:u),patch.object(logos,'download',side_effect=[ValueError('404'),(stream.getvalue(),'https://icons.duckduckgo.com/ip3/company.example.ico')]):
                result=logos.fetch('EXAMPLE',folder,'https://company.example')
            self.assertEqual(result.size,(24,24))
            meta=json.loads(next(Path(folder).glob('*.json')).read_text())
            self.assertIn('company.example',meta['url'])
            with patch.object(logos,'download',side_effect=AssertionError('cached icon must not download')):
                self.assertIsNotNone(logos.fetch('EXAMPLE',folder))

    def test_icon_rejects_private_redirects_and_oversized_payload(self):
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaisesRegex(ValueError,'Non-public'):logos.public_url('https://company.example')
        r=Mock(is_redirect=False);r.iter_content.return_value=[b'12345'];r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False)
        with patch.object(logos,'public_url',side_effect=lambda u:u),patch('requests.get',return_value=r):
            with self.assertRaisesRegex(ValueError,'too large'):logos.download('https://company.example/icon',limit=4)

    def test_missing_profile_uses_exact_isin_artwork_only(self):
        stream=io.BytesIO();Image.new('RGB',(64,64),'blue').save(stream,format='PNG')
        with tempfile.TemporaryDirectory(prefix='logos-') as folder:
            with patch('yfinance.Ticker') as ticker,patch.object(logos,'download',return_value=(stream.getvalue(),'https://api.elbstream.com/logos/isin/US0846707026?format=png&size=64')) as get:
                ticker.return_value.get_info.return_value={}
                self.assertIsNotNone(logos.fetch('BRK.B',folder,isin='US0846707026'))
                get.assert_called_once_with('https://api.elbstream.com/logos/isin/US0846707026?format=png&size=64')
            meta=json.loads(next(Path(folder).glob('*.json')).read_text())
            self.assertEqual(meta['isin'],'US0846707026');self.assertIn('Elbstream',meta['attribution'])


if __name__=='__main__':unittest.main()
