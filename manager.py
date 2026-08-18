import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from .result_store import ResultStore, endpoint_key
from .scanner import probe_stream, scan_targets


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ScanManager:
    def __init__(self, result_path, logger=None, runner=scan_targets):
        self.store = ResultStore(result_path)
        self.logger = logger
        self.runner = runner
        self._lock = threading.RLock()
        self._thread = None
        self._stop_event = threading.Event()
        self._run_hit_keys = set()
        self._status = self._new_status()

    @staticmethod
    def _new_status():
        return {
            'state': 'idle',
            'total': 0,
            'scanned': 0,
            'found': 0,
            'skipped_existing': 0,
            'current': '',
            'started_at': '',
            'finished_at': '',
            'message': '대기 중',
            'error': '',
        }

    def snapshot(self):
        with self._lock:
            status = dict(self._status)
            status['result_count'] = len(self.store.load())
            return status

    def is_active(self):
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def start(self, targets, config, excluded_endpoints=None):
        config.validate()
        excluded_endpoints = set(excluded_endpoints or set())
        filtered = [target for target in targets if f'{target}:{config.port}' not in excluded_endpoints]
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError('이미 스캔 또는 미디어 정보 확인이 실행 중입니다.')
            self._stop_event = threading.Event()
            self._run_hit_keys = set()
            self._status = self._new_status()
            self._status.update({
                'state': 'running',
                'total': len(filtered),
                'skipped_existing': len(targets) - len(filtered),
                'started_at': utc_now(),
                'message': '스캔을 시작했습니다.',
            })
            self._thread = threading.Thread(
                target=self._run,
                args=(filtered, config),
                name='ff-multicast-scanner',
                daemon=True,
            )
            self._thread.start()
        return self.snapshot()

    def start_reprobe(self, config):
        config.validate()
        results = self.store.load()
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError('이미 스캔 또는 미디어 정보 확인이 실행 중입니다.')
            self._stop_event = threading.Event()
            self._status = self._new_status()
            self._status.update({
                'state': 'probing',
                'total': len(results),
                'started_at': utc_now(),
                'message': '저장된 결과의 미디어 정보를 확인합니다.',
            })
            self._thread = threading.Thread(
                target=self._run_reprobe,
                args=(results, config),
                name='ff-multicast-reprobe',
                daemon=True,
            )
            self._thread.start()
        return self.snapshot()

    def stop(self):
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return False
            self._status['state'] = 'stopping'
            self._status['message'] = '현재 배치를 정리한 뒤 중지합니다.'
            self._stop_event.set()
            return True

    def clear_results(self):
        if self.is_active():
            raise RuntimeError('스캔 또는 미디어 정보 확인 중에는 결과를 비울 수 없습니다.')
        self.store.clear()

    def update_result(self, item_id, name=None, enabled=None):
        return self.store.update(item_id, name=name, enabled=enabled)

    def _progress(self, scanned, total, current):
        with self._lock:
            self._status['scanned'] = scanned
            self._status['total'] = total
            self._status['current'] = current
            self._status['message'] = f'{scanned}/{total} 주소를 확인했습니다.'

    def _hit(self, hit):
        key = endpoint_key(hit.get('url'))
        self.store.merge(hit)
        with self._lock:
            self._run_hit_keys.add(key)
            self._status['found'] = len(self._run_hit_keys)
            self._status['message'] = f'{hit.get("address")}에서 MPEG-TS 스트림을 찾았습니다.'

    def _scan_error(self, group, message):
        if self.logger is not None:
            self.logger.warning('Multicast scan socket error group=%s error=%s', group, message)

    def _run(self, targets, config):
        try:
            if not targets:
                with self._lock:
                    self._status.update({
                        'state': 'completed',
                        'finished_at': utc_now(),
                        'message': '검색할 신규 주소가 없습니다.',
                    })
                return
            scanned = self.runner(
                targets,
                config,
                stop_event=self._stop_event,
                progress_callback=self._progress,
                hit_callback=self._hit,
                error_callback=self._scan_error,
            )
            with self._lock:
                stopped = self._stop_event.is_set()
                self._status.update({
                    'state': 'stopped' if stopped else 'completed',
                    'scanned': scanned,
                    'finished_at': utc_now(),
                    'message': '사용자 요청으로 중지했습니다.' if stopped else '스캔이 완료되었습니다.',
                })
        except Exception as exception:
            if self.logger is not None:
                self.logger.exception('Multicast scan failed')
            with self._lock:
                self._status.update({
                    'state': 'error',
                    'finished_at': utc_now(),
                    'message': '스캔 중 오류가 발생했습니다.',
                    'error': str(exception),
                })

    def _run_reprobe(self, results, config):
        if not results:
            with self._lock:
                self._status.update({
                    'state': 'completed',
                    'finished_at': utc_now(),
                    'message': '확인할 검색 결과가 없습니다.',
                })
            return
        completed_count = 0
        verified_count = 0
        try:
            workers = min(config.probe_workers, len(results))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(probe_stream, item, config): item
                    for item in results
                }
                for future in as_completed(futures):
                    if self._stop_event.is_set():
                        for pending in futures:
                            pending.cancel()
                        break
                    item = futures[future]
                    probed = future.result()
                    self.store.update_probe(item.get('id'), probed)
                    completed_count += 1
                    if probed.get('probe_ok'):
                        verified_count += 1
                    with self._lock:
                        self._status.update({
                            'scanned': completed_count,
                            'found': verified_count,
                            'current': item.get('address') or item.get('url') or '',
                            'message': f'{completed_count}/{len(results)}개 미디어 정보를 확인했습니다.',
                        })
            with self._lock:
                stopped = self._stop_event.is_set()
                self._status.update({
                    'state': 'stopped' if stopped else 'completed',
                    'finished_at': utc_now(),
                    'message': (
                        '미디어 정보 확인을 중지했습니다.'
                        if stopped
                        else f'미디어 정보 확인 완료: {verified_count}/{len(results)}개'
                    ),
                })
        except Exception as exception:
            if self.logger is not None:
                self.logger.exception('Multicast reprobe failed')
            with self._lock:
                self._status.update({
                    'state': 'error',
                    'finished_at': utc_now(),
                    'message': '미디어 정보 확인 중 오류가 발생했습니다.',
                    'error': str(exception),
                })
