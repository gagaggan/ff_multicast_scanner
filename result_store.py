import hashlib
import ipaddress
import json
import os
import sqlite3
import threading
import urllib.parse
from pathlib import Path


def endpoint_key(url):
    parsed = urllib.parse.urlparse(str(url or '').strip())
    host = (parsed.hostname or '').strip().lower()
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not host:
        return ''
    port = parsed.port or 0
    return f'{host}:{port}'


def result_id(url):
    key = endpoint_key(url) or str(url or '')
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]


class ResultStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self):
        with self._lock:
            if not self.path.exists():
                return []
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
            except (OSError, ValueError, TypeError):
                return []
            return data if isinstance(data, list) else []

    def save(self, results):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + '.tmp')
            temporary.write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            os.replace(temporary, self.path)

    def merge(self, hit):
        with self._lock:
            results = self.load()
            key = endpoint_key(hit.get('url'))
            found = None
            for item in results:
                if endpoint_key(item.get('url')) == key:
                    found = item
                    break

            if found is None:
                found = {
                    'id': result_id(hit.get('url')),
                    'name': hit.get('service_name') or f'채널 {hit.get("address")}',
                    'custom_name': False,
                    'enabled': not bool(hit.get('scrambled')),
                    'first_seen': hit.get('detected_at'),
                    'seen_count': 0,
                }
                results.append(found)

            custom_name = bool(found.get('custom_name'))
            current_name = found.get('name')
            found.update(hit)
            if custom_name:
                found['name'] = current_name
                found['custom_name'] = True
            elif hit.get('service_name'):
                found['name'] = hit['service_name']
            found['last_seen'] = hit.get('detected_at')
            found['seen_count'] = int(found.get('seen_count') or 0) + 1
            found.setdefault('enabled', True)
            if found.get('scrambled'):
                found['enabled'] = False
            results.sort(key=lambda item: tuple(int(part) for part in str(item.get('address') or '0.0.0.0').split('.')))
            self.save(results)
            return dict(found)

    def update(self, item_id, name=None, enabled=None):
        with self._lock:
            results = self.load()
            updated = None
            for item in results:
                if str(item.get('id')) != str(item_id):
                    continue
                if name is not None:
                    clean_name = str(name).strip()
                    if not clean_name:
                        raise ValueError('채널 이름을 입력하세요.')
                    item['name'] = clean_name
                    item['custom_name'] = True
                if enabled is not None:
                    item['enabled'] = bool(enabled)
                updated = dict(item)
                break
            if updated is None:
                raise KeyError('검색 결과를 찾을 수 없습니다.')
            self.save(results)
            return updated

    def get(self, item_id):
        for item in self.load():
            if str(item.get('id')) == str(item_id):
                return dict(item)
        raise KeyError('검색 결과를 찾을 수 없습니다.')

    def update_probe(self, item_id, metadata):
        allowed = {
            'probe_ok',
            'probe_error',
            'service_name',
            'service_provider',
            'format_name',
            'bit_rate',
            'bit_rate_source',
            'video_codec',
            'width',
            'height',
            'frame_rate',
            'audio_codec',
            'audio_only',
            'program_count',
            'sample_ts_packets',
            'scrambled_ts_packets',
            'transport_error_packets',
            'scrambled',
            'scrambled_ratio',
        }
        with self._lock:
            results = self.load()
            updated = None
            for item in results:
                if str(item.get('id')) != str(item_id):
                    continue
                for key in allowed:
                    if key in metadata:
                        item[key] = metadata[key]
                if metadata.get('probe_ok'):
                    item.pop('probe_error', None)
                if metadata.get('scrambled'):
                    item['enabled'] = False
                if not item.get('custom_name') and metadata.get('service_name'):
                    item['name'] = metadata['service_name']
                updated = dict(item)
                break
            if updated is None:
                raise KeyError('검색 결과를 찾을 수 없습니다.')
            self.save(results)
            return updated

    def clear(self):
        self.save([])


def load_iproxy_channels(database_path):
    path = Path(str(database_path or '')).expanduser()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f'I-Proxy DB를 찾을 수 없습니다: {path}')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        row = connection.execute(
            "SELECT value FROM ff_iproxy_setting WHERE key = 'manual_channels' LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None or not row[0]:
        return []
    data = json.loads(row[0])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get('url')]


def existing_endpoint_keys(channels):
    return {endpoint_key(item.get('url')) for item in channels if endpoint_key(item.get('url'))}


def build_iproxy_channels(results, excluded_endpoints=None, enabled_only=True):
    excluded_endpoints = set(excluded_endpoints or set())
    channels = []
    for item in results or []:
        if enabled_only and not item.get('enabled', True):
            continue
        if item.get('scrambled'):
            continue
        if endpoint_key(item.get('url')) in excluded_endpoints:
            continue
        scheme = urllib.parse.urlparse(item.get('url') or '').scheme.lower()
        if scheme not in ('udp', 'rtp'):
            scheme = 'udp'
        channels.append({
            'id': f'scan_{item.get("id") or result_id(item.get("url"))}',
            'name': str(item.get('name') or f'채널 {item.get("address")}').strip(),
            'url': item.get('url'),
            'type': scheme,
            'type_diagnosed': True,
            'epg_enabled': True,
            'hidden': False,
            'rtp': scheme == 'rtp',
        })
    return channels


def build_m3u(channels):
    lines = ['#EXTM3U']
    for item in channels or []:
        name = str(item.get('name') or '').replace('"', "'").replace('\n', ' ').strip()
        channel_id = str(item.get('id') or '').replace('"', '').strip()
        lines.append(f'#EXTINF:-1 tvg-id="{channel_id}" tvg-name="{name}",{name}')
        lines.append(str(item.get('url') or '').strip())
    return '\n'.join(lines) + '\n'
