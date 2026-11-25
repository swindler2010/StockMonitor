import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parent/'stock_monitor'))
from store_release import migrate_data, bridge_active

class Tests(unittest.TestCase):
    def test_reused_destination_port_does_not_block_start(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d, socket.socket() as listener:
            source=Path(d)/'source';destination=Path(d)/'destination'
            destination.mkdir()
            listener.bind(('127.0.0.1',0));listener.listen()
            (destination/'bridge.json').write_text(json.dumps({'url':f'http://127.0.0.1:{listener.getsockname()[1]}'}))
            self.assertTrue(bridge_active(destination))  # Reproduces the old false positive.
            with patch('store_release.claim_instance') as claim:
                self.assertFalse(migrate_data(source,destination,allow_import=False))
                claim.assert_called_once_with(destination)
            self.assertEqual(list(destination.iterdir()),[destination/'bridge.json'])

    def test_real_duplicate_still_stops_before_writes(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            target=Path(d)/'target'
            from store_release import AlreadyRunning
            with patch('store_release.claim_instance',side_effect=AlreadyRunning('duplicate')):
                with self.assertRaises(AlreadyRunning):migrate_data(Path(d)/'source',target)
            self.assertFalse(target.exists())

if __name__=='__main__':unittest.main()
