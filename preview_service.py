import ipaddress
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path


class PreviewError(RuntimeError):
    pass


def validate_preview_source(url):
    parsed = urllib.parse.urlparse(str(url or '').strip())
    if parsed.scheme.lower() not in ('udp', 'rtp'):
        raise PreviewError('UDP/RTP 멀티캐스트 주소만 재생할 수 있습니다.')
    try:
        address = ipaddress.ip_address(parsed.hostname or '')
    except ValueError as exception:
        raise PreviewError('재생 주소가 올바르지 않습니다.') from exception
    if address.version != 4 or not address.is_multicast:
        raise PreviewError('IPv4 멀티캐스트 주소만 재생할 수 있습니다.')
    try:
        port = parsed.port
    except ValueError as exception:
        raise PreviewError('재생 포트가 올바르지 않습니다.') from exception
    if not port or not 1 <= port <= 65535:
        raise PreviewError('재생 포트가 올바르지 않습니다.')
    return parsed


def build_input_url(url, interface_address='0.0.0.0'):
    parsed = validate_preview_source(url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query['localaddr'] = [str(interface_address or '0.0.0.0')]
    query['overrun_nonfatal'] = ['1']
    query['fifo_size'] = ['5000000']
    # Multicast bursts can overflow the small OS UDP receive buffer before
    # FFmpeg's FIFO gets a chance to absorb them.
    query['buffer_size'] = ['16777216']
    query['timeout'] = ['10000000']
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def build_ffmpeg_command(ffmpeg_path, source_url, output_dir, video_codec='', audio_codec=''):
    output_dir = Path(output_dir)
    video_codec = str(video_codec or '').strip().lower()
    audio_codec = str(audio_codec or '').strip().lower()
    command = [
        str(ffmpeg_path or 'ffmpeg'),
        '-hide_banner',
        '-nostdin',
        '-loglevel',
        'warning',
        '-fflags',
        '+genpts+discardcorrupt',
        '-analyzeduration',
        '5000000',
        '-probesize',
        '5000000',
        '-i',
        source_url,
        '-map',
        '0:v:0?',
        '-map',
        '0:a:0?',
        '-sn',
        '-dn',
    ]
    # Re-encode every preview video. Live multicast sources can contain
    # damaged references/SPS data; copying H.264 would pass that corruption
    # to the browser and make the HLS MediaSource abort.
    command.extend([
        '-c:v',
        'libx264',
        '-preset',
        'veryfast',
        '-crf',
        '23',
        '-pix_fmt',
        'yuv420p',
        '-g',
        '60',
        '-keyint_min',
        '60',
        '-sc_threshold',
        '0',
    ])
    # AAC frames from multicast sources can also be malformed. Normalize the
    # audio so the browser never receives corrupt ADTS data or an unsupported
    # source profile.
    command.extend(['-c:a', 'aac', '-b:a', '128k', '-ar', '48000', '-ac', '2'])
    command.extend([
        '-max_muxing_queue_size',
        '1024',
        '-f',
        'hls',
        '-hls_time',
        '2',
        '-hls_list_size',
        '5',
        '-hls_flags',
        'delete_segments+append_list+omit_endlist+temp_file+independent_segments',
        '-hls_segment_filename',
        str(output_dir / 'segment_%05d.ts'),
        str(output_dir / 'live.m3u8'),
    ])
    return command


class PreviewManager:
    def __init__(self, root, logger=None, idle_seconds=30, max_sessions=2):
        self.root = Path(root)
        self.logger = logger
        self.idle_seconds = max(10, int(idle_seconds))
        self.max_sessions = max(1, int(max_sessions))
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._sessions = {}
        self._cleanup_thread = None

    @staticmethod
    def _safe_id(item_id):
        value = str(item_id or '').strip()
        if not value or not all(character.isalnum() or character in ('-', '_') for character in value):
            raise PreviewError('검색 결과 ID가 올바르지 않습니다.')
        return value

    def _session_dir(self, item_id):
        return self.root / self._safe_id(item_id)

    def _ensure_cleanup_thread(self):
        with self._lock:
            if self._cleanup_thread and self._cleanup_thread.is_alive():
                return
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name='ff-multicast-preview-cleanup',
                daemon=True,
            )
            self._cleanup_thread.start()

    @staticmethod
    def _consume_stderr(process, session):
        if process.stderr is None:
            return
        try:
            for line in process.stderr:
                clean = line.strip()
                if clean:
                    session['stderr'].append(clean)
                    del session['stderr'][:-20]
        except Exception:
            pass

    def start(self, item, ffmpeg_path='ffmpeg', interface_address='0.0.0.0', ready_timeout=12):
        item_id = self._safe_id(item.get('id'))
        source_url = build_input_url(item.get('url'), interface_address)
        self._ensure_cleanup_thread()
        with self._start_lock:
            with self._lock:
                existing = self._sessions.get(item_id)
                if existing and existing['process'].poll() is None:
                    existing['last_access'] = time.time()
                    if self.playlist_path(item_id).exists():
                        return item_id
                active = [
                    (key, session)
                    for key, session in self._sessions.items()
                    if session['process'].poll() is None and key != item_id
                ]
            if existing:
                self.stop(item_id)
            if len(active) >= self.max_sessions:
                oldest_id, _session = min(active, key=lambda pair: pair[1]['last_access'])
                self.stop(oldest_id)

            session_dir = self._session_dir(item_id)
            shutil.rmtree(session_dir, ignore_errors=True)
            session_dir.mkdir(parents=True, exist_ok=True)
            command = build_ffmpeg_command(
                ffmpeg_path,
                source_url,
                session_dir,
                video_codec=item.get('video_codec'),
                audio_codec=item.get('audio_codec'),
            )
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                )
            except OSError as exception:
                raise PreviewError(f'FFmpeg를 실행하지 못했습니다: {exception}') from exception
            session = {
                'process': process,
                'last_access': time.time(),
                'stderr': [],
            }
            with self._lock:
                self._sessions[item_id] = session
            threading.Thread(
                target=self._consume_stderr,
                args=(process, session),
                name=f'ff-multicast-preview-log-{item_id}',
                daemon=True,
            ).start()

            deadline = time.monotonic() + max(2, float(ready_timeout))
            playlist = self.playlist_path(item_id)
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    error = '\n'.join(session['stderr'][-4:]) or f'FFmpeg 종료 코드 {process.returncode}'
                    self.stop(item_id)
                    raise PreviewError(f'미리보기를 시작하지 못했습니다: {error[-600:]}')
                if playlist.exists() and '.ts' in playlist.read_text(encoding='utf-8', errors='replace'):
                    self.touch(item_id)
                    return item_id
                time.sleep(0.2)

            error = '\n'.join(session['stderr'][-4:])
            self.stop(item_id)
            suffix = f': {error[-500:]}' if error else ''
            raise PreviewError(f'미리보기 준비 시간이 초과됐습니다{suffix}')

    def stop(self, item_id):
        item_id = self._safe_id(item_id)
        with self._lock:
            session = self._sessions.pop(item_id, None)
        if session is not None:
            process = session['process']
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except Exception:
                    try:
                        process.kill()
                        process.wait(timeout=2)
                    except Exception:
                        pass
        shutil.rmtree(self._session_dir(item_id), ignore_errors=True)

    def touch(self, item_id):
        item_id = self._safe_id(item_id)
        with self._lock:
            session = self._sessions.get(item_id)
            if session is not None:
                session['last_access'] = time.time()

    def playlist_path(self, item_id):
        return self._session_dir(item_id) / 'live.m3u8'

    def segment_path(self, item_id, filename):
        safe_name = Path(str(filename or '')).name
        if safe_name != filename or not safe_name.startswith('segment_') or not safe_name.endswith('.ts'):
            raise PreviewError('세그먼트 이름이 올바르지 않습니다.')
        return self._session_dir(item_id) / safe_name

    def _cleanup_loop(self):
        while True:
            time.sleep(5)
            now = time.time()
            with self._lock:
                expired = [
                    item_id
                    for item_id, session in self._sessions.items()
                    if session['process'].poll() is not None
                    or now - session['last_access'] > self.idle_seconds
                ]
            for item_id in expired:
                self.stop(item_id)
