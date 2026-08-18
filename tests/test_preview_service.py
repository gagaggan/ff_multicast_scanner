import tempfile
import unittest
from pathlib import Path

from preview_service import PreviewError, build_ffmpeg_command, build_input_url, validate_preview_source


class PreviewServiceTest(unittest.TestCase):
    def test_accepts_only_multicast_udp_or_rtp(self):
        self.assertEqual(validate_preview_source('udp://239.1.2.3:49220').hostname, '239.1.2.3')
        self.assertEqual(validate_preview_source('rtp://239.1.2.3:49220').scheme, 'rtp')
        for url in ('http://example.com/live', 'udp://192.168.1.1:49220', 'udp://239.1.2.3'):
            with self.assertRaises(PreviewError):
                validate_preview_source(url)

    def test_adds_safe_multicast_input_options(self):
        url = build_input_url('udp://239.1.2.3:49220', '192.168.29.230')
        self.assertIn('localaddr=192.168.29.230', url)
        self.assertIn('overrun_nonfatal=1', url)

    def test_video_is_always_transcoded_and_audio_is_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            h264 = build_ffmpeg_command('ffmpeg', 'udp://239.1.2.3:49220', Path(directory), 'h264', 'aac')
            hevc = build_ffmpeg_command('ffmpeg', 'udp://239.1.2.3:49220', Path(directory), 'hevc', 'ac3')
        self.assertIn('libx264', h264)
        self.assertIn('libx264', hevc)
        self.assertIn('copy', h264)
        self.assertIn('aac', hevc)


if __name__ == '__main__':
    unittest.main()
