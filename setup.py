setting = {
    'filepath': __file__,
    'use_db': True,
    'use_default_setting': True,
    'home_module': 'scan',
    'menu': {
        'uri': __package__,
        'name': 'Multicast Scanner',
        'list': [
            {'uri': 'scan', 'name': '스캔'},
            {'uri': 'results', 'name': '검색 결과'},
            {'uri': 'setting', 'name': '설정'},
            {'uri': 'manual', 'name': '사용 안내'},
            {'uri': 'log', 'name': '로그'},
        ],
    },
    'default_route': 'single',
}

from plugin import *  # pylint: disable=wildcard-import,unused-wildcard-import

P = create_plugin_instance(setting)

try:
    from .logic import Logic

    P.set_module_list([Logic])
except Exception as exception:
    P.logger.error('Exception:%s', exception)
    P.logger.error(traceback.format_exc())
