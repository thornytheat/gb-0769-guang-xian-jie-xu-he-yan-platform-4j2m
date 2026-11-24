import json
from flask import request

from .config import DATA, SOURCES
from .errors import InspectionError

RETENTION_KEYS = {
    'batch_id', 'batch', 'id', 'batch_no', 'batch_number', 'issuance_id',
    'inspection_id', 'inspection', 'certificate_id',
    'due_at', 'due_on', 'due_date', 'expires_at', 'expires_on', 'expiry_date',
    'expiration_date', 'retention_date', 'valid_until', 'delete_at',
    'retention_until', 'retain_until', 'retention_days', 'ttl_days',
    'expire_days', 'expires_in_days', 'retention_period_days', 'days_until_expiry', 'ttl',
    'dispute', 'disputed', 'legal_hold', 'hold', 'reason', 'dispute_reason',
    'dispute_id', 'now', 'as_of', 'batches', 'retention',
}


def sample(kind):
    if kind not in ('clean','dirty'):
        raise InspectionError('UNKNOWN_TRACE_SET', status=404)
    folder = DATA / kind
    return {'traces': {key:(folder / f'{key}.csv').read_text(encoding='utf-8') for key in SOURCES},
            'settings':json.loads((folder / 'link.json').read_text(encoding='utf-8'))}


def request_material(allow_retention=False):
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise InspectionError('JSON_OBJECT_REQUIRED', status=400)
    allowed_extra = RETENTION_KEYS if allow_retention else set()
    if set(body) <= ({'sample','settings'} | allowed_extra) and 'sample' in body:
        data = sample(body['sample'])
        return {**data, 'settings':body.get('settings', data['settings'])}
    data = body.get('traces')
    allowed = {'traces','settings'} | allowed_extra
    if set(body) - allowed or not isinstance(data,dict) or set(data)!=set(SOURCES):
        raise InspectionError('THREE_TRACES_REQUIRED', status=400)
    if any(not isinstance(text,str) for text in data.values()):
        raise InspectionError('TRACE_TEXT_REQUIRED', status=400)
    return body
