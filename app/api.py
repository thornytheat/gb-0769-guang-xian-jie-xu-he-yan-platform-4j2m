from flask import Blueprint, current_app, jsonify, request

from .input import request_material
from .modules.archive.service import archive
from .modules.archive.storage import get, recent
from .modules.certificate.service import certify
from .modules.retention.service import batch_detail_if_exists, create_batch
from .pipeline import run

api = Blueprint('fiber_api', __name__, url_prefix='/api')


def calculate(stage):
    return run(request_material(), stage)[stage]


@api.post('/trace')
def trace():
    return calculate('trace')


@api.post('/calibration')
def calibration():
    return calculate('calibration')


@api.post('/pairs')
def pairs():
    return calculate('pairs')


@api.post('/loss')
def loss():
    return calculate('loss')


@api.post('/design')
def design():
    return calculate('design')


@api.post('/inspections')
def submit():
    report, fresh = archive(current_app.config['DATABASE'], request_material(allow_retention=True))
    body = request.get_json(silent=True) or {}
    result = {'inspection_id':report['inspection_id'], 'created':fresh, 'record':report}

    options = body.get('retention') if isinstance(body.get('retention'), dict) else body
    batches = body.get('batches')
    if isinstance(batches, list) and batches:
        result['retention_batches'] = []
        for item in batches:
            if not isinstance(item, dict):
                continue
            item = {**item, 'inspection_id': report['inspection_id']}
            result['retention_batches'].append(create_batch(current_app.config['DATABASE'], item))
    elif any(key in body for key in ('batch_id', 'due_at', 'due_date', 'expires_at', 'expiry_date',
                                     'expiration_date', 'retention_date', 'retention_until',
                                     'retention_days', 'dispute', 'disputed', 'legal_hold', 'retention')):
        item = {**options, 'inspection_id': report['inspection_id']}
        result['batch'] = create_batch(current_app.config['DATABASE'], item)
        result['batch_id'] = result['batch']['batch_id']
    return result, 201 if fresh else 200


@api.get('/inspections')
def list_inspections():
    return {'inspections':recent(current_app.config['DATABASE'])}


@api.get('/inspections/<identifier>')
def inspection(identifier):
    return get(current_app.config['DATABASE'], identifier)


@api.get('/certificate/<identifier>')
def certificate(identifier):
    database = current_app.config['DATABASE']
    pack = certify(get(database, identifier))
    batch_identifier = request.args.get('batch_id') or identifier
    batch = batch_detail_if_exists(database, batch_identifier)
    if batch is not None:
        retention = {key: batch.get(key) for key in (
            'batch_id', 'due_at', 'expires_at', 'state', 'status', 'expired', 'cleaned',
            'purged', 'disputed', 'legal_hold', 'materials_complete',
            'originals_available', 'can_rerun',
        )}
        if batch.get('cleaned') and not batch.get('disputed'):
            pack = {
                'inspection_id': pack['inspection_id'],
                'approved': pack['approved'],
                'blockers': pack['blockers'],
                'integrity_sha256': pack['integrity_sha256'],
                'conclusion_only': True,
                'retention': retention,
                **retention,
            }
        else:
            pack['retention'] = retention
            pack.update(retention)
    return pack


@api.get('/export/<identifier>')
def export(identifier):
    response = jsonify(certificate(identifier))
    response.headers['Content-Disposition'] = f'attachment; filename="splice-{identifier[:12]}.json"'
    return response
