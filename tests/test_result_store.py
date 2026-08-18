import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from result_store import (
    ResultStore,
    build_iproxy_channels,
    build_m3u,
    endpoint_key,
    existing_endpoint_keys,
    load_iproxy_channels,
)


class ResultStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'results.json'
        self.store = ResultStore(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_merge_and_custom_name_are_persistent(self):
        hit = {
            'address': '239.192.67.98',
            'port': 49220,
            'scheme': 'udp',
            'url': 'udp://239.192.67.98:49220',
            'detected_at': '2026-08-18T00:00:00+00:00',
            'service_name': 'SBS',
        }
        stored = self.store.merge(hit)
        self.store.update(stored['id'], name='SBS FHD', enabled=False)
        hit['service_name'] = 'Changed by probe'
        self.store.merge(hit)
        loaded = self.store.load()[0]
        self.assertEqual(loaded['name'], 'SBS FHD')
        self.assertFalse(loaded['enabled'])
        self.assertEqual(loaded['seen_count'], 2)

    def test_endpoint_key_ignores_transport_scheme(self):
        self.assertEqual(
            endpoint_key('udp://239.192.67.98:49220'),
            endpoint_key('rtp://239.192.67.98:49220'),
        )

    def test_export_excludes_existing_and_builds_m3u(self):
        results = [
            {'id': 'one', 'name': 'Existing', 'url': 'udp://239.1.1.1:49220', 'enabled': True},
            {'id': 'two', 'name': 'New', 'url': 'rtp://239.1.1.2:49220', 'enabled': True},
            {'id': 'three', 'name': 'Encrypted', 'url': 'rtp://239.1.1.3:49220', 'enabled': True, 'scrambled': True},
        ]
        excluded = {'239.1.1.1:49220'}
        channels = build_iproxy_channels(results, excluded_endpoints=excluded)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]['name'], 'New')
        self.assertTrue(channels[0]['rtp'])
        playlist = build_m3u(channels)
        self.assertIn('#EXTM3U', playlist)
        self.assertIn('rtp://239.1.1.2:49220', playlist)
        self.assertNotIn('rtp://239.1.1.3:49220', playlist)

    def test_scrambled_result_is_disabled(self):
        stored = self.store.merge({
            'address': '239.1.1.3',
            'port': 49220,
            'url': 'rtp://239.1.1.3:49220',
            'detected_at': '2026-08-18T00:00:00+00:00',
            'scrambled': True,
            'scrambled_ts_packets': 10,
            'sample_ts_packets': 100,
        })
        self.assertFalse(stored['enabled'])

    def test_loads_iproxy_database_read_only(self):
        database = Path(self.temporary.name) / 'ff_iproxy.db'
        connection = sqlite3.connect(database)
        connection.execute('CREATE TABLE ff_iproxy_setting (id INTEGER, key TEXT, value TEXT)')
        channels = [{'name': 'SBS', 'url': 'udp://239.192.67.98:49220'}]
        connection.execute(
            'INSERT INTO ff_iproxy_setting VALUES (?, ?, ?)',
            (1, 'manual_channels', json.dumps(channels)),
        )
        connection.commit()
        connection.close()
        loaded = load_iproxy_channels(database)
        self.assertEqual(loaded, channels)
        self.assertEqual(existing_endpoint_keys(loaded), {'239.192.67.98:49220'})

    def test_updates_probe_without_incrementing_seen_count(self):
        stored = self.store.merge({
            'address': '239.192.67.67',
            'port': 49220,
            'url': 'udp://239.192.67.67:49220',
            'detected_at': '2026-08-18T00:00:00+00:00',
        })
        updated = self.store.update_probe(stored['id'], {
            'probe_ok': True,
            'service_name': 'SBS',
            'width': 1920,
            'height': 1080,
            'bit_rate': 8000000,
            'bit_rate_source': 'measured',
        })
        self.assertEqual(updated['seen_count'], 1)
        self.assertEqual(updated['name'], 'SBS')
        self.assertEqual(self.store.get(stored['id'])['width'], 1920)


if __name__ == '__main__':
    unittest.main()
