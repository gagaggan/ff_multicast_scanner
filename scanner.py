import ipaddress
import json
import select
import socket
import struct
import subprocess
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone


TS_PACKET_SIZE = 188
MAX_HARD_TARGETS = 65536
BITRATE_SAMPLE_SECONDS = 2.0


class ScanValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ScanConfig:
    port: int = 49220
    interface_address: str = '0.0.0.0'
    batch_size: int = 4
    dwell_seconds: float = 1.2
    ffprobe_path: str = 'ffprobe'
    ffprobe_timeout: float = 4.0
    probe_workers: int = 2

    def validate(self):
        if not 1 <= int(self.port) <= 65535:
            raise ScanValidationError('포트는 1~65535 범위여야 합니다.')
        try:
            interface = ipaddress.ip_address(self.interface_address)
        except ValueError as exception:
            raise ScanValidationError('수신 인터페이스 주소가 올바르지 않습니다.') from exception
        if interface.version != 4 or interface.is_multicast:
            raise ScanValidationError('수신 인터페이스에는 IPv4 유니캐스트 주소를 입력하세요.')
        if not 1 <= int(self.batch_size) <= 32:
            raise ScanValidationError('배치 크기는 1~32 범위여야 합니다.')
        if not 0.2 <= float(self.dwell_seconds) <= 30:
            raise ScanValidationError('주소 수신 시간은 0.2~30초 범위여야 합니다.')
        if not 0.5 <= float(self.ffprobe_timeout) <= 30:
            raise ScanValidationError('ffprobe 제한 시간은 0.5~30초 범위여야 합니다.')
        if not 1 <= int(self.probe_workers) <= 4:
            raise ScanValidationError('ffprobe 동시 작업 수는 1~4 범위여야 합니다.')
        if not str(self.ffprobe_path or '').strip():
            raise ScanValidationError('ffprobe 실행 경로를 입력하세요.')
        return self


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _split_specs(raw):
    text = str(raw or '').replace(',', '\n')
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith('#')]


def _validate_multicast(address):
    try:
        parsed = ipaddress.ip_address(str(address).strip())
    except ValueError as exception:
        raise ScanValidationError(f'잘못된 IP 주소입니다: {address}') from exception
    if parsed.version != 4 or not parsed.is_multicast:
        raise ScanValidationError(f'IPv4 멀티캐스트 주소가 아닙니다: {address}')
    return parsed


def expand_target_specs(raw, max_targets=8192):
    try:
        max_targets = int(max_targets)
    except (TypeError, ValueError) as exception:
        raise ScanValidationError('최대 주소 수가 올바르지 않습니다.') from exception
    if not 1 <= max_targets <= MAX_HARD_TARGETS:
        raise ScanValidationError(f'최대 주소 수는 1~{MAX_HARD_TARGETS} 범위여야 합니다.')

    specs = _split_specs(raw)
    if not specs:
        raise ScanValidationError('스캔할 멀티캐스트 주소나 CIDR 범위를 입력하세요.')

    targets = []
    seen = set()

    def append_target(address):
        parsed = _validate_multicast(address)
        value = str(parsed)
        if value in seen:
            return
        seen.add(value)
        targets.append(value)
        if len(targets) > max_targets:
            raise ScanValidationError(
                f'대상 주소가 안전 제한 {max_targets}개를 초과합니다. 범위를 나누거나 최대 주소 수를 조정하세요.'
            )

    for spec in specs:
        if '/' in spec:
            try:
                network = ipaddress.ip_network(spec, strict=False)
            except ValueError as exception:
                raise ScanValidationError(f'잘못된 CIDR 범위입니다: {spec}') from exception
            if network.version != 4 or not network.network_address.is_multicast or not network.broadcast_address.is_multicast:
                raise ScanValidationError(f'IPv4 멀티캐스트 CIDR 범위가 아닙니다: {spec}')
            for address in network:
                append_target(address)
            continue

        if '-' in spec:
            start_text, end_text = [part.strip() for part in spec.split('-', 1)]
            start = _validate_multicast(start_text)
            end = _validate_multicast(end_text)
            if int(end) < int(start):
                raise ScanValidationError(f'범위의 끝 주소가 시작 주소보다 작습니다: {spec}')
            if int(end) - int(start) + 1 > max_targets:
                raise ScanValidationError(f'단일 범위가 안전 제한 {max_targets}개를 초과합니다: {spec}')
            for value in range(int(start), int(end) + 1):
                append_target(ipaddress.ip_address(value))
            continue

        append_target(spec)

    return targets


def find_mpeg_ts_offset(payload):
    if not payload:
        return None
    limit = min(TS_PACKET_SIZE, len(payload))
    for offset in range(limit):
        if payload[offset] != 0x47:
            continue
        remaining = len(payload) - offset
        packet_count = remaining // TS_PACKET_SIZE
        if packet_count < 2:
            continue
        checks = min(packet_count, 5)
        if all(payload[offset + (index * TS_PACKET_SIZE)] == 0x47 for index in range(checks)):
            return offset
    return None


def is_rtp_payload(payload, ts_offset):
    if ts_offset is None or ts_offset < 12 or len(payload) < 12:
        return False
    version = payload[0] >> 6
    header_length = 12 + ((payload[0] & 0x0F) * 4)
    return version == 2 and ts_offset >= header_length


def mpeg_ts_packet_stats(payload, ts_offset):
    stats = {
        'sample_ts_packets': 0,
        'scrambled_ts_packets': 0,
        'transport_error_packets': 0,
    }
    if ts_offset is None:
        return stats
    for position in range(ts_offset, len(payload) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
        packet = payload[position:position + TS_PACKET_SIZE]
        if len(packet) != TS_PACKET_SIZE or packet[0] != 0x47:
            continue
        stats['sample_ts_packets'] += 1
        if packet[1] & 0x80:
            stats['transport_error_packets'] += 1
        if (packet[3] >> 6) & 0x03:
            stats['scrambled_ts_packets'] += 1
    return stats


def _finalize_scrambling_stats(stats):
    sampled = int(stats.get('sample_ts_packets') or 0)
    scrambled = int(stats.get('scrambled_ts_packets') or 0)
    stats['scrambled'] = scrambled > 0
    stats['scrambled_ratio'] = (scrambled / sampled) if sampled else 0.0
    return stats


def _membership(group, interface_address):
    return struct.pack('=4s4s', socket.inet_aton(group), socket.inet_aton(interface_address))


def _open_group_socket(group, port, interface_address):
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, 'SO_REUSEPORT'):
        try:
            receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    receiver.bind((group, port))
    membership = _membership(group, interface_address)
    receiver.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    receiver.setblocking(False)
    return receiver, membership


def scan_batch(groups, config, stop_event=None, error_callback=None):
    config.validate()
    receivers = {}
    hits = {}
    try:
        for group in groups:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                receiver, membership = _open_group_socket(group, config.port, config.interface_address)
                receivers[receiver] = (group, membership)
            except OSError as exception:
                if error_callback is not None:
                    error_callback(group, str(exception))

        deadline = time.monotonic() + config.dwell_seconds
        while receivers and time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                break
            wait = min(0.2, max(0.0, deadline - time.monotonic()))
            readable, _, _ = select.select(list(receivers), [], [], wait)
            for receiver in readable:
                group, _ = receivers[receiver]
                try:
                    payload, _peer = receiver.recvfrom(65535)
                except (BlockingIOError, OSError):
                    continue
                ts_offset = find_mpeg_ts_offset(payload)
                if ts_offset is None:
                    continue
                rtp = is_rtp_payload(payload, ts_offset)
                scheme = 'rtp' if rtp else 'udp'
                hit = hits.get(group)
                if hit is None:
                    hit = {
                        'address': group,
                        'port': config.port,
                        'scheme': scheme,
                        'url': f'{scheme}://{group}:{config.port}',
                        'ts_offset': ts_offset,
                        'sample_bytes': 0,
                        'sample_ts_packets': 0,
                        'scrambled_ts_packets': 0,
                        'transport_error_packets': 0,
                        'detected_at': utc_now(),
                        'probe_ok': False,
                    }
                    hits[group] = hit
                packet_stats = mpeg_ts_packet_stats(payload, ts_offset)
                hit['sample_bytes'] += len(payload)
                for key in ('sample_ts_packets', 'scrambled_ts_packets', 'transport_error_packets'):
                    hit[key] += packet_stats[key]
    finally:
        for receiver, (_group, membership) in list(receivers.items()):
            try:
                receiver.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, membership)
            except OSError:
                pass
            try:
                receiver.close()
            except OSError:
                pass
    return [_finalize_scrambling_stats(hit) for hit in hits.values()]


def sample_scrambling(url, interface_address, sample_seconds=0.75, max_datagrams=512):
    parsed = urllib.parse.urlparse(str(url or ''))
    group = parsed.hostname or ''
    port = parsed.port or 0
    receiver = None
    membership = None
    stats = {
        'sample_ts_packets': 0,
        'scrambled_ts_packets': 0,
        'transport_error_packets': 0,
    }
    try:
        receiver, membership = _open_group_socket(group, port, interface_address)
        deadline = time.monotonic() + max(0.2, float(sample_seconds))
        datagrams = 0
        while datagrams < int(max_datagrams) and time.monotonic() < deadline:
            wait = min(0.1, max(0.0, deadline - time.monotonic()))
            readable, _, _ = select.select([receiver], [], [], wait)
            if not readable:
                continue
            payload, _peer = receiver.recvfrom(65535)
            ts_offset = find_mpeg_ts_offset(payload)
            if ts_offset is None:
                continue
            packet_stats = mpeg_ts_packet_stats(payload, ts_offset)
            for key in stats:
                stats[key] += packet_stats[key]
            datagrams += 1
    finally:
        if receiver is not None and membership is not None:
            try:
                receiver.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, membership)
            except OSError:
                pass
        if receiver is not None:
            receiver.close()
    return _finalize_scrambling_stats(stats)


def _probe_url(url, interface_address, timeout_seconds):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('udp', 'rtp'):
        return url
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    query['localaddr'] = [interface_address]
    query['timeout'] = [str(int(float(timeout_seconds) * 1_000_000))]
    query['overrun_nonfatal'] = ['1']
    query['fifo_size'] = ['1000000']
    query['buffer_size'] = ['16777216']
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))


def _first_stream(streams, codec_type):
    for stream in streams or []:
        if stream.get('codec_type') == codec_type:
            return stream
    return {}


def parse_ffprobe_output(raw, bitrate_sample_seconds=BITRATE_SAMPLE_SECONDS):
    data = json.loads(raw or '{}')
    programmes = data.get('programs') or []
    streams = data.get('streams') or []
    if not streams and programmes:
        for programme in programmes:
            streams.extend(programme.get('streams') or [])

    tags = {}
    for programme in programmes:
        tags.update(programme.get('tags') or {})
    tags.update((data.get('format') or {}).get('tags') or {})
    video = _first_stream(streams, 'video')
    audio = _first_stream(streams, 'audio')
    format_data = data.get('format') or {}
    bit_rate = int(format_data.get('bit_rate') or 0)
    bit_rate_source = 'reported' if bit_rate else ''
    if not bit_rate and bitrate_sample_seconds:
        packet_bytes = sum(int(packet.get('size') or 0) for packet in data.get('packets') or [])
        if packet_bytes:
            bit_rate = int((packet_bytes * 8) / float(bitrate_sample_seconds))
            bit_rate_source = 'measured'

    service_name = str(tags.get('service_name') or tags.get('SERVICE_NAME') or '').strip()
    service_provider = str(tags.get('service_provider') or tags.get('SERVICE_PROVIDER') or '').strip()
    return {
        'probe_ok': bool(programmes or streams or format_data.get('format_name')),
        'service_name': service_name,
        'service_provider': service_provider,
        'format_name': format_data.get('format_name') or '',
        'bit_rate': bit_rate,
        'bit_rate_source': bit_rate_source,
        'video_codec': video.get('codec_name') or '',
        'width': int(video.get('width') or 0),
        'height': int(video.get('height') or 0),
        'frame_rate': video.get('avg_frame_rate') or '',
        'audio_codec': audio.get('codec_name') or '',
        'program_count': len(programmes),
    }


def probe_stream(hit, config):
    result = dict(hit)
    try:
        scrambling_stats = sample_scrambling(hit['url'], config.interface_address)
        if scrambling_stats.get('sample_ts_packets'):
            result.update(scrambling_stats)
    except (OSError, ValueError):
        pass
    target = _probe_url(hit['url'], config.interface_address, config.ffprobe_timeout)
    command = [
        config.ffprobe_path,
        '-v',
        'error',
        '-analyzeduration',
        '3000000',
        '-probesize',
        '3000000',
        '-read_intervals',
        f'%+{BITRATE_SAMPLE_SECONDS:g}',
        '-print_format',
        'json',
        '-show_programs',
        '-show_streams',
        '-show_format',
        '-show_packets',
        '-show_entries',
        'program:stream:format:packet=size',
        target,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=config.ffprobe_timeout + 1,
            check=False,
        )
        if completed.stdout.strip():
            result.update(parse_ffprobe_output(completed.stdout))
        if completed.returncode != 0 and not result.get('probe_ok'):
            result['probe_error'] = (completed.stderr or 'ffprobe failed').strip()[-500:]
    except subprocess.TimeoutExpired:
        result['probe_error'] = 'ffprobe timeout'
    except (OSError, ValueError, json.JSONDecodeError) as exception:
        result['probe_error'] = str(exception)[-500:]
    return result


def _chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def scan_targets(targets, config, stop_event=None, progress_callback=None, hit_callback=None, error_callback=None):
    config.validate()
    total = len(targets)
    scanned = 0
    for batch in _chunks(targets, config.batch_size):
        if stop_event is not None and stop_event.is_set():
            break
        raw_hits = scan_batch(batch, config, stop_event=stop_event, error_callback=error_callback)
        enriched_hits = []
        if raw_hits and (stop_event is None or not stop_event.is_set()):
            workers = min(config.probe_workers, len(raw_hits))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(probe_stream, hit, config) for hit in raw_hits]
                for future in as_completed(futures):
                    enriched_hits.append(future.result())
        else:
            enriched_hits = raw_hits

        for hit in enriched_hits:
            if hit_callback is not None:
                hit_callback(hit)

        scanned += len(batch)
        if progress_callback is not None:
            progress_callback(scanned, total, batch[-1])
    return scanned
