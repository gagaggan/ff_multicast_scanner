import json
import traceback
from pathlib import Path

from flask import Response, abort, jsonify, render_template, request
from plugin import F, PluginModuleBase

from .manager import ScanManager
from .result_store import (
    build_iproxy_channels,
    build_m3u,
    endpoint_key,
    existing_endpoint_keys,
    load_iproxy_channels,
)
from .scanner import ScanConfig, ScanValidationError, expand_target_specs
from .setup import P


logger = P.logger
package_name = P.package_name
ModelSetting = P.ModelSetting
SystemModelSetting = F.SystemModelSetting
blueprint = P.blueprint
RESULT_PATH = Path(__file__).resolve().parent / 'data' / 'results.json'
manager = ScanManager(RESULT_PATH, logger=logger)


def parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'y', 'on')


def get_public_api_key():
    if not SystemModelSetting.get_bool('use_apikey'):
        return None
    return SystemModelSetting.get('apikey') or None


def require_api_key(req):
    expected = get_public_api_key()
    if expected is not None and req.args.get('apikey') != expected:
        abort(403)


def get_base_url(req=None):
    ddns = (SystemModelSetting.get('ddns') or '').strip()
    if ddns:
        return ddns.rstrip('/') + f'/{package_name}'
    req = req or request
    return req.url_root.rstrip('/') + f'/{package_name}'


def with_apikey(url):
    api_key = get_public_api_key()
    if not api_key:
        return url
    separator = '&' if '?' in url else '?'
    return f'{url}{separator}apikey={api_key}'


def _value(req, key):
    if req is not None and key in req.form:
        return req.form.get(key, '')
    return ModelSetting.get(key)


def build_config(req=None):
    config = ScanConfig(
        port=int(_value(req, 'scan_port') or 49220),
        interface_address=(_value(req, 'interface_address') or '0.0.0.0').strip(),
        batch_size=int(_value(req, 'batch_size') or 4),
        dwell_seconds=float(_value(req, 'dwell_seconds') or 1.2),
        ffprobe_path=(_value(req, 'ffprobe_path') or 'ffprobe').strip(),
        ffprobe_timeout=float(_value(req, 'ffprobe_timeout') or 4),
        probe_workers=int(_value(req, 'probe_workers') or 2),
    )
    return config.validate()


def load_existing():
    channels = load_iproxy_channels(ModelSetting.get('iproxy_db_path'))
    return channels, existing_endpoint_keys(channels)


def export_channels(include_existing=False):
    excluded = set()
    if not include_existing:
        try:
            _channels, excluded = load_existing()
        except Exception:
            logger.warning('Unable to read I-Proxy channels while exporting', exc_info=True)
    return build_iproxy_channels(manager.store.load(), excluded_endpoints=excluded, enabled_only=True)


class Logic(PluginModuleBase):
    db_default = {
        'scan_ranges': '239.192.67.0/24',
        'scan_port': '49220',
        'interface_address': '0.0.0.0',
        'batch_size': '4',
        'dwell_seconds': '1.2',
        'ffprobe_path': 'ffprobe',
        'ffprobe_timeout': '4',
        'probe_workers': '2',
        'max_targets': '8192',
        'exclude_existing': 'True',
        'iproxy_db_path': '/data/db/ff_iproxy.db',
    }

    scan_setting_keys = [
        'scan_ranges',
        'scan_port',
        'interface_address',
        'batch_size',
        'dwell_seconds',
        'exclude_existing',
    ]
    advanced_setting_keys = [
        'ffprobe_path',
        'ffprobe_timeout',
        'probe_workers',
        'max_targets',
        'iproxy_db_path',
    ]

    def __init__(self, PM):
        super().__init__(PM, None)
        self.name = 'scan'

    def process_menu(self, sub, req):
        arg = ModelSetting.to_dict()
        arg['package_name'] = package_name
        arg['status'] = manager.snapshot()
        arg['results'] = []
        arg['existing_count'] = 0
        arg['iproxy_error'] = ''
        try:
            channels, existing = load_existing()
            arg['existing_count'] = len(channels)
        except Exception as exception:
            existing = set()
            arg['iproxy_error'] = str(exception)

        if sub == 'results':
            for result in manager.store.load():
                row = dict(result)
                row['existing'] = endpoint_key(row.get('url')) in existing
                arg['results'].append(row)
            base = get_base_url(req)
            arg['json_url'] = with_apikey(f'{base}/api/results.json')
            arg['m3u_url'] = with_apikey(f'{base}/api/results.m3u')
        return render_template(f'{package_name}_{sub}.html', arg=arg)

    def process_ajax(self, sub, req):
        try:
            if sub == 'status':
                return jsonify({'ret': 'success', 'data': manager.snapshot()})
            if sub == 'start_scan':
                config = build_config(req)
                targets = expand_target_specs(
                    _value(req, 'scan_ranges'),
                    max_targets=int(ModelSetting.get('max_targets') or 8192),
                )
                excluded = set()
                if parse_bool(_value(req, 'exclude_existing')):
                    _channels, excluded = load_existing()
                status = manager.start(targets, config, excluded_endpoints=excluded)
                self._save_values(req, self.scan_setting_keys)
                return jsonify({'ret': 'success', 'msg': '스캔을 시작했습니다.', 'data': status})
            if sub == 'stop_scan':
                stopped = manager.stop()
                return jsonify({
                    'ret': 'success' if stopped else 'warning',
                    'msg': '중지 요청을 보냈습니다.' if stopped else '실행 중인 스캔이 없습니다.',
                    'data': manager.snapshot(),
                })
            if sub == 'clear_results':
                manager.clear_results()
                return jsonify({'ret': 'success', 'msg': '검색 결과를 비웠습니다.'})
            if sub == 'update_result':
                enabled = parse_bool(req.form.get('enabled')) if 'enabled' in req.form else None
                result = manager.update_result(
                    req.form.get('id', ''),
                    name=req.form.get('name') if 'name' in req.form else None,
                    enabled=enabled,
                )
                return jsonify({'ret': 'success', 'msg': '검색 결과를 저장했습니다.', 'data': result})
            if sub == 'save_settings':
                build_config(req)
                max_targets = int(_value(req, 'max_targets') or 8192)
                expand_target_specs(ModelSetting.get('scan_ranges'), max_targets=max_targets)
                if ModelSetting.get_bool('exclude_existing'):
                    load_iproxy_channels(_value(req, 'iproxy_db_path'))
                self._save_values(req, self.advanced_setting_keys)
                return jsonify({'ret': 'success', 'msg': '설정을 저장했습니다.'})
            return jsonify({'ret': 'warning', 'msg': f'Unknown ajax: {sub}'})
        except (ScanValidationError, ValueError, KeyError, FileNotFoundError, RuntimeError) as exception:
            return jsonify({'ret': 'warning', 'msg': str(exception)})
        except Exception as exception:
            logger.error('Exception:%s', exception)
            logger.error(traceback.format_exc())
            return jsonify({'ret': 'danger', 'msg': str(exception)})

    @staticmethod
    def _save_values(req, keys):
        for key in keys:
            if key == 'exclude_existing':
                ModelSetting.set(key, 'True' if parse_bool(req.form.get(key)) else 'False')
            elif key in req.form:
                ModelSetting.set(key, req.form.get(key, '').strip())


@blueprint.route('/api/results.json', methods=['GET'])
def api_results_json():
    require_api_key(request)
    channels = export_channels(include_existing=parse_bool(request.args.get('include_existing')))
    response = Response(
        json.dumps(channels, ensure_ascii=False, indent=2) + '\n',
        content_type='application/json; charset=utf-8',
    )
    response.headers['Cache-Control'] = 'no-store'
    return response


@blueprint.route('/api/results.m3u', methods=['GET'])
def api_results_m3u():
    require_api_key(request)
    channels = export_channels(include_existing=parse_bool(request.args.get('include_existing')))
    response = Response(build_m3u(channels), content_type='audio/mpegurl; charset=utf-8')
    response.headers['Cache-Control'] = 'no-store'
    return response
