import json
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from flask import Blueprint, Response, current_app, jsonify, request

from . import service

retention_api = Blueprint('retention_api', __name__, url_prefix='/api')


def database():
    return current_app.config['DATABASE']


def payload():
    body = request.get_json(silent=True)
    if body is None:
        body = request.form.to_dict(flat=True) if request.form else {}
    if not isinstance(body, dict):
        body = {}
    return {**request.args.to_dict(flat=True), **body}


def attachment(response, filename):
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def bundle_response(bundle):
    identifier = str(bundle.get('batch_id') or bundle.get('inspection_id') or 'batch')
    short = identifier[:12]
    wants_zip = request.args.get('format') == 'zip' or request.args.get('download') == 'zip'
    if wants_zip:
        buffer = BytesIO()
        with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
            for item in bundle.get('files', []):
                archive.writestr(item['filename'], item['content'])
            archive.writestr('manifest.json', json.dumps(
                bundle.get('manifest', {}), ensure_ascii=False, indent=2, sort_keys=True))
        response = Response(buffer.getvalue(), mimetype='application/zip')
        return attachment(response, f'originals-{short}.zip')

    response = jsonify(bundle)
    if request.args.get('download') not in {'0', 'false', 'no'}:
        attachment(response, f'originals-{short}.json')
    return response


def source_response(item):
    response = Response(item['content'], mimetype='text/csv; charset=utf-8')
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    return attachment(response, item['filename'])


@retention_api.get('/batches')
def list_batches():
    batches = service.list_batches(database())
    return {'batches': batches, 'items': batches, 'count': len(batches)}


@retention_api.post('/batches')
def create_batch():
    result = service.create_batch(database(), payload())
    return jsonify(result), 201 if result.get('created') else 200


@retention_api.get('/batches/<identifier>')
def get_batch(identifier):
    return service.batch_detail(database(), identifier)


@retention_api.patch('/batches/<identifier>')
@retention_api.put('/batches/<identifier>')
def patch_batch(identifier):
    return service.update_batch(database(), identifier, payload())


@retention_api.post('/batches/<identifier>')
def post_batch(identifier):
    data = payload()
    action = data.get('action')
    if action in {'dispute', 'accept_dispute', 'hold', 'legal_hold'}:
        return batch_dispute(identifier)
    if action in {'rerun', 'replay'}:
        return batch_rerun(identifier)
    if action in {'cleanup', 'clean', 'purge'}:
        result = service.run_cleanup(database(), data)
        return jsonify(result), 200
    return service.update_batch(database(), identifier, data)


@retention_api.post('/batches/<identifier>/dispute')
@retention_api.post('/batches/<identifier>/disputes')
def batch_dispute(identifier):
    data = payload()
    data.setdefault('batch_id', identifier)
    result = service.accept_dispute(database(), data)
    return jsonify(result), 201 if result.get('created') else 200


@retention_api.get('/batches/<identifier>/conclusion')
def batch_conclusion(identifier):
    batch = service.batch_detail(database(), identifier)
    return batch.get('conclusion') or batch.get('certificate')


@retention_api.get('/batches/<identifier>/originals')
def batch_originals(identifier):
    source = request.args.get('source')
    result = service.original_bundle(database(), identifier, source=source)
    if source is not None:
        return source_response(result)
    return bundle_response(result)


@retention_api.get('/batches/<identifier>/originals/download')
def batch_originals_zip(identifier):
    result = service.original_bundle(database(), identifier)
    request.args = {**request.args, 'format': 'zip'}
    return bundle_response(result)


@retention_api.get('/batches/<identifier>/originals/<source>')
def batch_original_source(identifier, source):
    result = service.original_bundle(database(), identifier, source=source)
    return source_response(result)


@retention_api.post('/batches/<identifier>/cleanup')
def cleanup_batch(identifier):
    data = payload()
    data.setdefault('batch_id', identifier)
    result = service.run_cleanup(database(), data)
    return jsonify(result), 200


@retention_api.post('/batches/<identifier>/rerun')
@retention_api.get('/batches/<identifier>/rerun')
def batch_rerun(identifier):
    return service.rerun_batch(database(), identifier)


@retention_api.get('/originals')
def list_originals():
    items = service.list_originals(database())
    return {'originals': items, 'items': items, 'count': len(items)}


@retention_api.get('/originals/<sha>')
def get_original(sha):
    item = service.original_content(database(), sha)
    if request.args.get('content') in {'1', 'true', 'csv'}:
        response = Response(item['content'], mimetype='text/csv; charset=utf-8')
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        return attachment(response, f'{sha[:12]}.csv')
    return {'sha256': item['sha256'], 'content': item['content'], 'status': item['status']}


@retention_api.post('/references')
def add_reference():
    result = service.add_reference(database(), payload())
    return jsonify(result), 201


@retention_api.get('/references')
def list_references():
    items = service.list_originals(database())
    references = [ref for item in items for ref in item['references']]
    return {'references': references, 'items': references, 'count': len(references)}


@retention_api.post('/dispute')
def create_dispute_singular():
    return create_dispute()


@retention_api.get('/disputes')
def list_disputes():
    disputes = service.list_disputes(database())
    return {'disputes': disputes, 'items': disputes, 'count': len(disputes)}


@retention_api.post('/disputes')
def create_dispute():
    result = service.accept_dispute(database(), payload())
    return jsonify(result), 201 if result.get('created') else 200


@retention_api.get('/disputes/<dispute_id>')
def get_dispute(dispute_id):
    return service.dispute_detail(database(), dispute_id)


@retention_api.post('/disputes/<dispute_id>/close')
def close_dispute(dispute_id):
    return service.close_dispute(database(), dispute_id, payload())


@retention_api.get('/disputes/<dispute_id>/originals')
def dispute_originals(dispute_id):
    source = request.args.get('source')
    result = service.original_bundle(database(), None, dispute_id=dispute_id, source=source)
    if source is not None:
        return source_response(result)
    return bundle_response(result)


@retention_api.get('/disputes/<dispute_id>/originals/download')
def dispute_originals_zip(dispute_id):
    result = service.original_bundle(database(), None, dispute_id=dispute_id)
    request.args = {**request.args, 'format': 'zip'}
    return bundle_response(result)


@retention_api.post('/cleanup')
@retention_api.get('/cleanup')
def cleanup():
    if request.method == 'GET':
        runs = service.list_runs(database())
        return {'runs': runs, 'items': runs, 'count': len(runs)}
    result = service.run_cleanup(database(), payload())
    return jsonify(result), 200


@retention_api.post('/cleanup/resume')
@retention_api.post('/cleanup/recover')
def cleanup_resume():
    result = service.resume_cleanup(database(), payload=payload())
    return jsonify(result), 200


@retention_api.get('/cleanup/runs')
def cleanup_runs():
    runs = service.list_runs(database())
    return {'runs': runs, 'items': runs}


@retention_api.get('/cleanup/runs/<run_id>')
def cleanup_run(run_id):
    return service.run_detail(database(), run_id)


@retention_api.post('/cleanup/runs/<run_id>/resume')
@retention_api.post('/cleanup/runs/<run_id>/recover')
def cleanup_run_resume(run_id):
    return service.resume_cleanup(database(), run_id, payload())


@retention_api.post('/cleanup/runs/<run_id>/post')
def cleanup_run_post(run_id):
    return service.post_cleanup(database(), run_id, payload())


# Semantic aliases matching the existing inspection/certificate vocabulary.
for alias in (
    '/certificates/<identifier>/originals',
    '/inspections/<identifier>/originals',
    '/certificate/<identifier>/originals',
    '/archive/<identifier>/originals',
    '/export/<identifier>/originals',
):
    retention_api.add_url_rule(alias, view_func=batch_originals, methods=['GET'])

for alias in (
    '/certificates/<identifier>/originals/<source>',
    '/inspections/<identifier>/originals/<source>',
    '/certificate/<identifier>/originals/<source>',
    '/archive/<identifier>/originals/<source>',
):
    retention_api.add_url_rule(alias, view_func=batch_original_source, methods=['GET'])

for alias in (
    '/certificates/<identifier>/dispute',
    '/inspections/<identifier>/dispute',
    '/certificate/<identifier>/dispute',
    '/archive/<identifier>/dispute',
    '/inspections/<identifier>/disputes',
    '/certificate/<identifier>/disputes',
    '/certificates/<identifier>/disputes',
):
    retention_api.add_url_rule(alias, view_func=batch_dispute, methods=['POST'])

for alias in (
    '/inspections/<identifier>/retention',
    '/certificate/<identifier>/retention',
    '/certificates/<identifier>/retention',
    '/archive/<identifier>/retention',
    '/issuances/<identifier>/retention',
):
    retention_api.add_url_rule(alias, view_func=patch_batch, methods=['PATCH', 'PUT', 'POST'])

for alias in (
    '/inspections/<identifier>/cleanup',
    '/certificate/<identifier>/cleanup',
    '/certificates/<identifier>/cleanup',
    '/archive/<identifier>/cleanup',
    '/issuances/<identifier>/cleanup',
):
    retention_api.add_url_rule(alias, view_func=cleanup_batch, methods=['POST'])

for alias in (
    '/certificates/<identifier>/rerun',
    '/inspections/<identifier>/rerun',
    '/certificate/<identifier>/rerun',
    '/archive/<identifier>/rerun',
):
    retention_api.add_url_rule(alias, view_func=batch_rerun, methods=['GET', 'POST'])

for alias in (
    '/retention/cleanup',
    '/retention/run',
    '/retention/runs',
    '/archive/cleanup',
    '/inspections/cleanup',
    '/certificate/cleanup',
    '/purge',
):
    retention_api.add_url_rule(alias, view_func=cleanup, methods=['GET', 'POST'])

for alias in ('/retention/cleanup/resume', '/retention/recover', '/archive/cleanup/resume'):
    retention_api.add_url_rule(alias, view_func=cleanup_resume, methods=['POST'])

# Collection aliases.
retention_api.add_url_rule('/retention', view_func=list_batches, methods=['GET'])
retention_api.add_url_rule('/retention', view_func=create_batch, methods=['POST'])
retention_api.add_url_rule('/retentions', view_func=list_batches, methods=['GET'])
retention_api.add_url_rule('/retentions', view_func=create_batch, methods=['POST'])
retention_api.add_url_rule('/retentions/<identifier>', view_func=get_batch, methods=['GET'])
retention_api.add_url_rule('/retentions/<identifier>', view_func=patch_batch, methods=['PATCH', 'PUT', 'POST'])
retention_api.add_url_rule('/retentions/<identifier>/originals', view_func=batch_originals, methods=['GET'])
retention_api.add_url_rule('/retention/batches', view_func=list_batches, methods=['GET'])
retention_api.add_url_rule('/retention/batches', view_func=create_batch, methods=['POST'])
retention_api.add_url_rule('/retention/dispute', view_func=create_dispute, methods=['POST'])
retention_api.add_url_rule('/retention/disputes', view_func=list_disputes, methods=['GET'])
retention_api.add_url_rule('/retention/disputes', view_func=create_dispute, methods=['POST'])

# Issuance vocabulary aliases.
retention_api.add_url_rule('/issuances', view_func=list_batches, methods=['GET'])
retention_api.add_url_rule('/issuances', view_func=create_batch, methods=['POST'])
retention_api.add_url_rule('/issuances/<identifier>', view_func=get_batch, methods=['GET'])
retention_api.add_url_rule('/issuances/<identifier>', view_func=patch_batch, methods=['PATCH', 'PUT'])
retention_api.add_url_rule('/issuances/<identifier>/originals', view_func=batch_originals, methods=['GET'])
retention_api.add_url_rule('/issuances/<identifier>/originals/<source>', view_func=batch_original_source, methods=['GET'])
retention_api.add_url_rule('/issuances/<identifier>/dispute', view_func=batch_dispute, methods=['POST'])
retention_api.add_url_rule('/issuances/<identifier>/disputes', view_func=batch_dispute, methods=['POST'])
retention_api.add_url_rule('/issuances/<identifier>/rerun', view_func=batch_rerun, methods=['GET', 'POST'])
retention_api.add_url_rule('/batches/<identifier>/download', view_func=batch_originals, methods=['GET'])
retention_api.add_url_rule('/disputes/<dispute_id>/download', view_func=dispute_originals, methods=['GET'])
for alias in ('/inspections/<identifier>/download-originals', '/certificate/<identifier>/download-originals',
              '/archive/<identifier>/download-originals'):
    retention_api.add_url_rule(alias, view_func=batch_originals, methods=['GET'])

for alias in ('/retention/expire', '/retention/purge', '/expire', '/purge/expired',
              '/archive/expire', '/archive/purge'):
    retention_api.add_url_rule(alias, view_func=cleanup, methods=['GET', 'POST'])

retention_api.add_url_rule('/archive/disputes', view_func=list_disputes, methods=['GET'])
retention_api.add_url_rule('/archive/disputes', view_func=create_dispute, methods=['POST'])
retention_api.add_url_rule('/retention/ledger', view_func=cleanup_runs, methods=['GET'])
retention_api.add_url_rule('/cleanup/post', view_func=cleanup, methods=['POST'])
retention_api.add_url_rule('/artifacts', view_func=list_originals, methods=['GET'])
retention_api.add_url_rule('/materials', view_func=list_originals, methods=['GET'])
retention_api.add_url_rule('/retention/references', view_func=list_references, methods=['GET'])
retention_api.add_url_rule('/retention/references', view_func=add_reference, methods=['POST'])
