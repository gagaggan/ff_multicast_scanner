import tempfile
import unittest
from pathlib import Path

from preview_service import PreviewError, PreviewManager, build_ffmpeg_command, build_input_url, validate_preview_source


class FakeProcess:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.terminate()


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
        self.assertIn('buffer_size=16777216', url)

    def test_video_and_audio_are_always_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            h264 = build_ffmpeg_command('ffmpeg', 'udp://239.1.2.3:49220', Path(directory), 'h264', 'aac')
            hevc = build_ffmpeg_command('ffmpeg', 'udp://239.1.2.3:49220', Path(directory), 'hevc', 'ac3')
        self.assertIn('libx264', h264)
        self.assertIn('libx264', hevc)
        self.assertNotIn('copy', h264)
        self.assertIn('aac', h264)
        self.assertIn('aac', hevc)
        self.assertEqual(h264[h264.index('-c:a') + 1], 'aac')
        self.assertEqual(hevc[hevc.index('-c:a') + 1], 'aac')

    def test_uses_iproxy_vaapi_encoder_and_downscales_uhd(self):
        with tempfile.TemporaryDirectory() as directory:
            command = build_ffmpeg_command(
                'ffmpeg',
                'rtp://239.1.2.3:49220',
                Path(directory),
                'hevc',
                'aac',
                video_encoder='h264_vaapi',
                vaapi_device='/dev/dri/renderD128',
                width=3840,
                height=2160,
            )
        self.assertEqual(command[command.index('-c:v') + 1], 'h264_vaapi')
        self.assertEqual(command[command.index('-vaapi_device') + 1], '/dev/dri/renderD128')
        self.assertEqual(command[command.index('-vf') + 1], 'scale=1280:720,format=nv12,hwupload')
        self.assertIn('8192', command)

    def test_stale_cleanup_cannot_remove_replacement_session(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = PreviewManager(directory)
            item_id = 'channel'
            stale_session = {'process': FakeProcess(), 'last_access': 0, 'stderr': []}
            replacement = {'process': FakeProcess(), 'last_access': 100, 'stderr': []}
            manager._sessions[item_id] = replacement
            session_dir = manager._session_dir(item_id)
            session_dir.mkdir(parents=True)
            marker = session_dir / 'live.m3u8'
            marker.write_text('#EXTM3U\n', encoding='utf-8')

            stopped = manager.stop(item_id, expected_session=stale_session)

            self.assertFalse(stopped)
            self.assertIs(manager._sessions[item_id], replacement)
            self.assertTrue(marker.exists())
            self.assertFalse(replacement['process'].terminated)

    def test_cleanup_rechecks_recent_access_before_stopping(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = PreviewManager(directory, idle_seconds=30)
            item_id = 'channel'
            session = {'process': FakeProcess(), 'last_access': 95, 'stderr': []}
            manager._sessions[item_id] = session
            manager._session_dir(item_id).mkdir(parents=True)

            stopped = manager.stop(item_id, expected_session=session, expired_before=100)

            self.assertFalse(stopped)
            self.assertIs(manager._sessions[item_id], session)
            self.assertTrue(manager._session_dir(item_id).exists())

    def test_fallback_session_keeps_requested_encoder_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = PreviewManager(directory)
            manager._sessions['channel'] = {
                'process': FakeProcess(),
                'last_access': 100,
                'stderr': [],
                'encoder_profile': ('h264_vaapi', '/dev/dri/renderD128'),
            }
            manager._session_dir('channel').mkdir(parents=True)
            manager.playlist_path('channel').write_text('#EXTM3U\nsegment_00001.ts\n', encoding='utf-8')

            result = manager.start(
                {'id': 'channel', 'url': 'udp://239.1.2.3:49220'},
                video_encoder='h264_vaapi',
                vaapi_device='/dev/dri/renderD128',
            )

            self.assertEqual(result, 'channel')
            self.assertFalse(manager._sessions['channel']['process'].terminated)


if __name__ == '__main__':
    unittest.main()
