import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent


def load_runtime(path=None):
    path=Path(path or ROOT/'runtime_config.json')
    config=json.loads(path.read_text(encoding='utf-8'))
    if config.get('schema_version')!=1 or not isinstance(config.get('keyword'),str) or not config['keyword'].strip():
        raise ValueError('invalid_runtime_config')
    for key in ('search_interval_seconds','refresh_seconds','poll_seconds','max_requests_per_run',
                'max_details_per_run','max_tasks_per_run','max_requests_per_hour','max_requests_per_day',
                'min_interval_seconds','cooldown_seconds','max_consecutive_errors','max_attempts',
                'retry_base_seconds','request_timeout_seconds'):
        if type(config.get(key)) is not int or config[key]<1:
            raise ValueError('invalid_runtime_setting:'+key)
    if config['min_interval_seconds']>60 or config['poll_seconds']>60 or config['request_timeout_seconds']>60:
        raise ValueError('runtime_wait_or_timeout_exceeds_60_seconds')
    if config['max_attempts']>3:
        raise ValueError('max_attempts_must_not_exceed_3')
    if not (config['max_requests_per_run']<=config['max_requests_per_hour']<=config['max_requests_per_day']):
        raise ValueError('inconsistent_request_budgets')
    for key in ('database','run_dir','evidence_dir','report_dir'):
        if not isinstance(config.get(key),str) or not config[key]:
            raise ValueError('invalid_output_path:'+key)
        config[key]=str((path.parent/config[key]).resolve())
    return config
