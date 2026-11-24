from flask import Blueprint, abort, current_app, render_template, request

from .config import SOURCES, SPEC
from .errors import InspectionError
from .input import sample
from .modules.archive.service import archive
from .modules.archive.storage import get, recent
from .modules.certificate.service import certify
from .modules.retention import service as retention
from .pipeline import run
from .modules.calibration.contract import FIELDS
from .views import CODES, LABELS, half_mm_metres, metres, trace_frames

console = Blueprint('console', __name__)
console.add_app_template_filter(metres, 'metres')
console.add_app_template_filter(half_mm_metres, 'half_mm_metres')


def material(mode):
    if mode == 'custom':
        return {'traces':{key:request.form.get(key,'') for key in SOURCES},
                'settings':{key:request.form.get(key,'') for key in FIELDS}}
    return sample(mode)


def handle_retention_action(database, identifier):
    action = request.form.get('action') or request.args.get('action')
    if not action:
        for candidate in ('dispute', 'accept_dispute', 'legal_hold', 'cleanup', 'run_cleanup',
                          'resume', 'recover_cleanup', 'rerun', 'replay', 'set_due', 'close_dispute'):
            if candidate in request.form or candidate in request.args:
                action = candidate
                break
    if action in {'legal_hold'}:
        action = 'dispute'
    if action in {'set_due', 'update_due'}:
        action = 'due'
    if action in {'recover_cleanup'}:
        action = 'resume'
    if action in {'replay'}:
        action = 'rerun'
    if not action:
        return None
    payload = request.form.to_dict(flat=True)
    if action in {'dispute', 'accept_dispute', 'hold'}:
        payload.setdefault('batch_id', identifier)
        result = retention.accept_dispute(database, payload)
        return {'kind': 'dispute', 'text': '争议已受理，完整原件已封存待复核', 'result': result}
    if action in {'cleanup', 'run_cleanup', 'clean_expired'}:
        result = retention.run_cleanup(database, payload)
        return {'kind': 'cleanup', 'text': '清理已落账：到期批结论保留，引用中的原件继续保留', 'result': result}
    if action in {'resume', 'recover_cleanup'}:
        result = retention.resume_cleanup(database, payload=payload)
        return {'kind': 'cleanup', 'text': '清理已恢复并完成对账', 'result': result}
    if action in {'post', 'post_cleanup'}:
        result = retention.post_cleanup(database, request.form.get('run_id'), now=payload.get('now'))
        return {'kind': 'cleanup', 'text': '清理台账已落账', 'result': result}
    if action in {'due', 'set_due', 'update_due'}:
        result = retention.update_batch(database, identifier, payload)
        return {'kind': 'batch', 'text': '到期日已更新', 'result': result}
    if action in {'close_dispute'}:
        result = retention.close_dispute(database, request.form.get('dispute_id'), payload)
        return {'kind': 'dispute', 'text': '争议已结案', 'result': result}
    if action in {'rerun', 'replay'}:
        result = retention.rerun_batch(database, identifier)
        return {'kind': 'rerun', 'text': '旧批已按在保原件复算；已清除原件不会恢复', 'result': result}
    return None


@console.route('/', methods=['GET','POST'])
@console.route('/inspect/<section>', methods=['GET','POST'])
@console.route('/inspect/<section>/<path:path_identifier>', methods=['GET','POST'])
def view(section='design', path_identifier=None):
    if section not in LABELS:
        abort(404)
    mode = request.values.get('sample','dirty')
    database = current_app.config['DATABASE']
    evidence, frames, entries, data = None, {}, [], {}
    issued, fresh, error, sources = None, False, None, {}
    batch, retention_notice, batch_rows, batch_by_inspection = None, None, [], {}
    try:
        sources = material(mode)
        if section in ('archive','certificate'):
            if section == 'archive' and request.method == 'POST' and not request.form.get('action'):
                issued, fresh = archive(database, sources)
            entries = recent(database)
            inspection_identifier = (request.args.get('inspection') or request.form.get('inspection')
                                     or path_identifier
                                     or (entries[0]['inspection_id'] if entries else None))
            batch_identifier = (request.args.get('batch_id') or request.form.get('batch_id')
                                or request.args.get('id') or request.form.get('id')
                                or inspection_identifier)

            if section == 'certificate' and batch_identifier and request.method == 'POST' and request.form.get('action'):
                retention_notice = handle_retention_action(database, batch_identifier)

            if section == 'certificate' and inspection_identifier:
                evidence = certify(get(database, inspection_identifier))
                batch = retention.batch_detail(database, batch_identifier, include_certificate=True)
                if batch.get('cleaned') and not batch.get('disputed'):
                    evidence = batch['certificate']
            batch_rows = retention.list_batches(database)
            batch_by_inspection = {row['inspection_id']: row for row in batch_rows}
        else:
            data = run(sources, section)
            if 'calibration' in data:
                frames = trace_frames(data['calibration'])
    except InspectionError as exc:
        error = exc.code
    return render_template('console.html', section=section, labels=LABELS, codes=CODES, spec=SPEC, mode=mode,
                           data=data, frames=frames, error=error, materials=sources, entries=entries,
                           evidence=evidence, issued=issued, fresh=fresh, fields=FIELDS, batch=batch,
                           retention_notice=retention_notice, batch_rows=batch_rows,
                           batch_by_inspection=batch_by_inspection)
