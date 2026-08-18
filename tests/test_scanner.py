import json
import unittest

from scanner import (
    ScanValidationError,
    expand_target_specs,
    find_mpeg_ts_offset,
    is_rtp_payload,
    parse_ffprobe_output,
)


def ts_payload(offset=0, packets=4):
    prefix = bytearray(offset)
    if offset >= 12:
        prefix[0] = 0x80
    body = bytearray()
    for _index in range(packets):
        body.extend(bytes([0x47]) + bytes(187))
    return bytes(prefix + body)


class ScannerTest(unittest.TestCase):
    def test_expand_cidr_range_and_deduplicate(self):
        targets = expand_target_specs(
            '239.192.67.0/30\n239.192.67.2-239.192.67.5\n239.192.67.5',
            max_targets=10,
        )
        self.assertEqual(
            targets,
            [
                '239.192.67.0',
                '239.192.67.1',
                '239.192.67.2',
                '239.192.67.3',
                '239.192.67.4',
                '239.192.67.5',
            ],
        )

    def test_rejects_unicast_and_oversized_range(self):
        with self.assertRaises(ScanValidationError):
            expand_target_specs('192.168.1.0/24')
        with self.assertRaises(ScanValidationError):
            expand_target_specs('239.1.1.0/24', max_targets=10)

    def test_detects_plain_and_rtp_transport_stream(self):
        plain = ts_payload()
        rtp = ts_payload(offset=12)
        self.assertEqual(find_mpeg_ts_offset(plain), 0)
        self.assertEqual(find_mpeg_ts_offset(rtp), 12)
        self.assertFalse(is_rtp_payload(plain, 0))
        self.assertTrue(is_rtp_payload(rtp, 12))

    def test_ignores_random_payload(self):
        self.assertIsNone(find_mpeg_ts_offset(bytes(range(256)) * 2))

    def test_parses_ffprobe_metadata(self):
        raw = json.dumps({
            'programs': [{'program_id': 1, 'tags': {'service_name': 'SBS'}}],
            'streams': [
                {'codec_type': 'video', 'codec_name': 'hevc', 'width': 1920, 'height': 1080},
                {'codec_type': 'audio', 'codec_name': 'aac'},
            ],
            'format': {'format_name': 'mpegts', 'bit_rate': '10000000'},
        })
        parsed = parse_ffprobe_output(raw)
        self.assertTrue(parsed['probe_ok'])
        self.assertEqual(parsed['service_name'], 'SBS')
        self.assertEqual(parsed['video_codec'], 'hevc')
        self.assertEqual(parsed['height'], 1080)
        self.assertEqual(parsed['bit_rate'], 10000000)


if __name__ == '__main__':
    unittest.main()
