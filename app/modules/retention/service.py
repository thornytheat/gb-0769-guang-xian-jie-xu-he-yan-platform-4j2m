import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.config import SOURCES
from app.errors import InspectionError
from app.modules.archive.identity import canonical, digest
from app.modules.archive.storage import get as get_inspection
from app.modules.certificate.service import certify

from .storage import json_dumps, json_loads, retention_session

FAR_FUTURE = '2999-12-31T00:00:00+00:00'
ORIGINAL_PURGED_CODE = 'ORIGINAL_PURGED'
ACTIVE_DISPUTE_STATUSES = {'accepted', 'open', 'active', 'under_review', 'filed', 'investigating'}
CLOSED_DISPUTE_STATUSES = {'closed', 'resolved', 'rejected', 'withdrawn', 'cancelled'}


def now_iso(value=None):
    moment = temporal(value)
    return moment.isoformat(timespec='seconds')


def temporal(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip().replace('/', '-')
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise InspectionError('INVALID_RETENTION_DATE', status=400) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def pick(data, keys, default=None):
    for key in keys:
        if isinstance(data, dict) and key in data and data[key] not in (None, ''):
            return data[key]
    return default


def new_id(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:16]}'


def as_dict(payload):
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    raise InspectionError('JSON_OBJECT_REQUIRED', status=400)


def truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'accept', 'accepted'}


def row_dict(row):
    return dict(row) if row is not None else None


def parse_materials(data):
    data = as_dict(data)
    materials = pick(data, ('materials', 'material'))
    if materials is not None:
        if not isinstance(materials, dict) or not isinstance(materials.get('traces'), dict):
            raise InspectionError('TRACES_REQUIRED', status=400)
        return materials
    traces = pick(data, ('traces',))
    sample_name = pick(data, ('sample', 'sample_data', 'dataset'))
    if traces is not None:
        if not isinstance(traces, dict):
            raise InspectionError('TRACE_TEXT_REQUIRED', status=400)
        settings = pick(data, ('settings', 'setting'), default={})
        if settings is None:
            settings = {}
        if not isinstance(settings, dict):
            raise InspectionError('SETTING_FIELDS', status=422)
        return {'traces': traces, 'settings': settings}
    if sample_name is not None:
        from app.input import sample

        materials = sample(sample_name)
        settings = pick(data, ('settings', 'setting'))
        if isinstance(settings, dict):
            materials = {**materials, 'settings': settings}
        return materials
    return None


def due_value(data, now=None):
    explicit = pick(data, (
        'due_at', 'due_on', 'due_date', 'expires_at', 'expires_on', 'expiry_date',
        'retention_until', 'retain_until', 'retention_date', 'valid_until',
        'cleanup_after', 'delete_after', 'delete_at', 'expiration', 'expiration_date',
    ))
    if explicit is not None:
        # Preserve the common date-only contract while validating that it parses.
        if isinstance(explicit, str) and len(explicit.strip()) == 10:
            temporal(explicit)
            return explicit.strip(), True
        if isinstance(explicit, date) and not isinstance(explicit, datetime):
            return explicit.isoformat(), True
        return now_iso(explicit), True
    days = pick(data, ('retention_days', 'ttl_days', 'expire_days', 'expires_in_days',
                       'retention_period_days', 'days_until_expiry', 'ttl'))
    if days is not None:
        try:
            number = int(days)
            if number < 0:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise InspectionError('INVALID_RETENTION_DAYS', status=400) from exc
        base = temporal(now)
        return now_iso(base + timedelta(days=number)), True
    return FAR_FUTURE, False


def upsert_original(db, sha, content, at, *, resurrect=True):
    size = len(content.encode('utf-8')) if isinstance(content, str) else len(content or b'')
    existing = db.execute('SELECT sha256, status FROM retention_originals WHERE sha256=?', (sha,)).fetchone()
    if existing is None:
        db.execute(
            'INSERT INTO retention_originals (sha256, content, size_bytes, status, created_at) VALUES (?,?,?,?,?)',
            (sha, content, size, 'active', at),
        )
        return 'stored'
    if existing['status'] == 'purged' and not resurrect:
        return 'tombstone'
    db.execute(
        """
        UPDATE retention_originals
           SET content=?, size_bytes=?, status='active', purged_at=NULL
         WHERE sha256=?
        """,
        (content, size, sha),
    )
    return 'restored' if existing['status'] == 'purged' else 'active'


def store_archive_materials(database, manifest, materials=None, *, fresh=True, now=None):
    """Persist content-addressed originals without creating a business reference.

    Idempotent archive replays have ``fresh=False``.  They must not overwrite a
    purge tombstone.
    """
    at = now_iso(now)
    hashes = manifest.get('traces_sha256', {})
    traces = (materials or {}).get('traces', {})
    with retention_session(database) as db:
        for source, sha in hashes.items():
            content = traces.get(source) if isinstance(traces, dict) else None
            if fresh and isinstance(content, str):
                upsert_original(db, sha, content, at, resurrect=True)
            else:
                db.execute(
                    """
                    INSERT OR IGNORE INTO retention_originals
                        (sha256, content, size_bytes, status, created_at)
                    VALUES (?, NULL, 0, 'missing', ?)
                    """,
                    (sha, at),
                )


def register_archive(database, manifest, materials=None, *, fresh=True, due_at=None, now=None):
    """Register an inspection as a retention batch and content-addressed references.

    A non-fresh archive call is an old-batch replay.  It must never overwrite a
    purge tombstone or revive a released reference.
    """
    at = now_iso(now)
    due = now_iso(due_at) if due_at is not None else FAR_FUTURE
    inspection_id = manifest['inspection_id']
    hashes = manifest.get('traces_sha256', {})
    traces = (materials or {}).get('traces', {})

    with retention_session(database) as db:
        db.execute(
            """
            INSERT INTO retention_batches
                (batch_id, inspection_id, due_at, status, created_at, updated_at)
            VALUES (?,?,?, 'active', ?, ?)
            ON CONFLICT(batch_id) DO NOTHING
            """,
            (inspection_id, inspection_id, due, at, at),
        )
        for source, sha in hashes.items():
            content = traces.get(source)
            if fresh and isinstance(content, str):
                upsert_original(db, sha, content, at, resurrect=True)
            else:
                db.execute(
                    """
                    INSERT OR IGNORE INTO retention_originals
                        (sha256, content, size_bytes, status, created_at)
                    VALUES (?, NULL, 0, 'missing', ?)
                    """,
                    (sha, at),
                )
            db.execute(
                """
                INSERT OR IGNORE INTO retention_references
                    (batch_id, sha256, source, filename, status, created_at)
                VALUES (?,?,?,?, 'active', ?)
                """,
                (inspection_id, sha, source, f'{source}.csv', at),
            )
    return batch_detail(database, inspection_id, include_certificate=False)


def _fetch_batch(db, identifier):
    row = db.execute('SELECT * FROM retention_batches WHERE batch_id=?', (identifier,)).fetchone()
    if row is not None:
        return row
    return db.execute(
        'SELECT * FROM retention_batches WHERE inspection_id=? ORDER BY rowid DESC LIMIT 1',
        (identifier,),
    ).fetchone()


def ensure_batch(database, identifier):
    with retention_session(database) as db:
        row = _fetch_batch(db, identifier)
    if row is not None:
        return row_dict(row)
    inspection = get_inspection(database, identifier)
    return register_archive(database, inspection, fresh=False)


def create_batch(database, payload=None, **kwargs):
    data = {**as_dict(payload), **kwargs}
    created = True

    batch_id = pick(data, ('batch_id', 'id', 'batch', 'batch_no', 'batch_number', 'issuance_id'))
    inspection_id = pick(data, ('inspection_id', 'inspection', 'certificate_id', 'run_id', 'archive_id'))
    materials = parse_materials(data)

    if not inspection_id and materials is not None:
        from app.modules.archive.service import archive

        manifest, archive_fresh = archive(database, materials)
        inspection_id = manifest['inspection_id']
    if not inspection_id:
        raise InspectionError('INSPECTION_ID_OR_MATERIALS_REQUIRED', status=400)

    inspection = get_inspection(database, inspection_id)
    inspection_id = inspection['inspection_id']
    if not batch_id:
        batch_id = inspection_id

    due, due_provided = due_value(data)
    at = now_iso(pick(data, ('created_at', 'now')))
    with retention_session(database) as db:
        existing = db.execute('SELECT batch_id FROM retention_batches WHERE batch_id=?', (batch_id,)).fetchone()
        if existing is not None:
            created = False
            if due_provided:
                db.execute(
                    'UPDATE retention_batches SET due_at=?, updated_at=? WHERE batch_id=?',
                    (due, at, batch_id),
                )
        else:
            db.execute(
                """
                INSERT INTO retention_batches
                    (batch_id, inspection_id, due_at, status, created_at, updated_at, metadata)
                VALUES (?,?,?, 'active', ?, ?, ?)
                """,
                (batch_id, inspection_id, due, at, at, json_dumps(pick(data, ('metadata',), {}) or {})),
            )

        traces = (materials or {}).get('traces', {})
        for source, sha in inspection.get('traces_sha256', {}).items():
            content = traces.get(source) if isinstance(traces, dict) else None
            if isinstance(content, str):
                upsert_original(db, sha, content, at, resurrect=True)
            else:
                db.execute(
                    """
                    INSERT OR IGNORE INTO retention_originals
                        (sha256, content, size_bytes, status, created_at)
                    VALUES (?, NULL, 0, 'missing', ?)
                    """,
                    (sha, at),
                )
            db.execute(
                """
                INSERT OR IGNORE INTO retention_references
                    (batch_id, sha256, source, filename, status, created_at)
                VALUES (?,?,?,?, 'active', ?)
                """,
                (batch_id, sha, source, f'{source}.csv', at),
            )

        # Explicit business batches own retention.  The content-addressed
        # inspection's lazy/implicit reference must not become a third holder.
        if batch_id != inspection_id and not truthy(pick(data, ('keep_implicit',), False)):
            db.execute(
                "UPDATE retention_references SET status='superseded', released_at=? WHERE batch_id=? AND status='active'",
                (at, inspection_id),
            )
            db.execute(
                "UPDATE retention_batches SET status='superseded', updated_at=? WHERE batch_id=? AND status='active'",
                (at, inspection_id),
            )

    dispute_requested = truthy(pick(data, ('dispute', 'disputed', 'legal_hold', 'hold'), False))
    if dispute_requested:
        dispute = accept_dispute(database, {
            'batch_id': batch_id,
            'reason': pick(data, ('dispute_reason', 'reason'), '签发争议留档'),
            'dispute_id': pick(data, ('dispute_id',)),
        })
        result = batch_detail(database, batch_id)
        dispute_summary = {key: value for key, value in dispute.items() if key != 'batch'}
        result['dispute'] = dispute_summary
        result['created'] = created
        return result

    result = batch_detail(database, batch_id)
    result['created'] = created
    return result


def update_batch(database, identifier, payload=None, **kwargs):
    data = {**as_dict(payload), **kwargs}
    batch = ensure_batch(database, identifier)
    due, provided = due_value(data)
    at = now_iso(pick(data, ('now',)))
    with retention_session(database) as db:
        if provided:
            db.execute('UPDATE retention_batches SET due_at=?, updated_at=? WHERE batch_id=?',
                       (due, at, batch['batch_id']))
        status = pick(data, ('status', 'state'))
        if status and status in {'active', 'expired', 'cleaned'}:
            db.execute('UPDATE retention_batches SET status=?, updated_at=? WHERE batch_id=?',
                       (status, at, batch['batch_id']))
    return batch_detail(database, batch['batch_id'])


def _dispute_rows(db, batch_id):
    return [row_dict(row) for row in db.execute(
        'SELECT * FROM retention_disputes WHERE batch_id=? ORDER BY rowid DESC', (batch_id,)
    ).fetchall()]


def _active_dispute(disputes):
    for dispute in disputes:
        if dispute['status'] in ACTIVE_DISPUTE_STATUSES:
            return dispute
    return None


def review_manifest(db, batch_id):
    refs = [row_dict(row) for row in db.execute(
        'SELECT * FROM retention_references WHERE batch_id=?', (batch_id,)
    ).fetchall()]
    originals = {row['sha256']: row_dict(row) for row in db.execute('SELECT * FROM retention_originals')}
    by_source = {ref['source']: ref for ref in refs}
    files = []
    complete = True
    for source in SOURCES:
        ref = by_source.get(source)
        original = originals.get(ref['sha256']) if ref else None
        stored = bool(original and original['status'] == 'active' and original.get('content') is not None)
        reference_active = bool(ref and ref['status'] == 'active')
        available = stored and reference_active
        complete = complete and available
        files.append({
            'source': source,
            'filename': (ref or {}).get('filename', f'{source}.csv'),
            'sha256': (ref or {}).get('sha256'),
            'reference_status': (ref or {}).get('status', 'missing'),
            'original_status': (original or {}).get('status', 'missing'),
            'size_bytes': (original or {}).get('size_bytes', 0),
            'available': available,
        })
    return {'complete': complete, 'expected': list(SOURCES), 'files': files}


def batch_detail(database, identifier, include_certificate=True):
    with retention_session(database) as db:
        row = _fetch_batch(db, identifier)
        batch = row_dict(row) if row is not None else None

    if batch is None:
        # Fall back to an inspection id and create its retention projection.
        inspection = get_inspection(database, identifier)
        register_archive(database, inspection, fresh=False)
        with retention_session(database) as db:
            row = _fetch_batch(db, identifier)
            if row is None:
                raise InspectionError('BATCH_NOT_FOUND', status=404)
            result = _batch_payload(db, row_dict(row))
    else:
        with retention_session(database) as db:
            fresh_row = _fetch_batch(db, batch['batch_id'])
            result = _batch_payload(db, row_dict(fresh_row))

    if include_certificate:
        result = _with_certificate(database, result)
    return result


def _batch_payload(db, batch):
    batch_id = batch['batch_id']
    refs = [row_dict(r) for r in db.execute(
        'SELECT * FROM retention_references WHERE batch_id=? ORDER BY source', (batch_id,)
    ).fetchall()]
    disputes = _dispute_rows(db, batch_id)
    active_dispute_row = _active_dispute(disputes)
    manifest = review_manifest(db, batch_id)

    reference_counts = {}
    for ref in refs:
        count = db.execute(
            "SELECT COUNT(*) AS n FROM retention_references WHERE sha256=? AND status='active'",
            (ref['sha256'],),
        ).fetchone()['n']
        reference_counts[ref['sha256']] = count

    due = temporal(batch['due_at'])
    now = temporal()
    expired = due <= now
    cleaned = bool(batch.get('cleaned_at')) or batch['status'] in {'expired', 'cleaned', 'purged'}
    superseded = batch['status'] == 'superseded'
    disputed = active_dispute_row is not None
    can_download = manifest['complete'] and not cleaned and not superseded
    if disputed:
        # A legal hold remains downloadable even if a stale cleanup marked the
        # batch row; cleanup itself is conditional and will not release it.
        can_download = manifest['complete']

    if disputed:
        state, status = 'DISPUTED', 'disputed'
    elif cleaned or expired:
        state, status = 'EXPIRED', 'expired'
    elif superseded:
        state, status = 'SUPERSEDED', 'superseded'
    else:
        state, status = 'ACTIVE', 'active'
    active_reference_count = sum(1 for ref in refs if ref['status'] == 'active')
    result = {
        **batch,
        'id': batch_id,
        'batch_id': batch_id,
        'reference_count': active_reference_count,
        'ref_count': active_reference_count,
        'active_reference_count': active_reference_count,
        'inspection_id': batch['inspection_id'],
        'due_at': batch['due_at'],
        'expires_at': batch['due_at'],
        'expires_on': batch['due_at'][:10],
        'expiry_date': batch['due_at'][:10],
        'retention_until': batch['due_at'],
        'expired': expired,
        'due': expired,
        'cleaned': cleaned,
        'superseded': superseded,
        'purged': cleaned and not manifest['complete'],
        'disputed': disputed,
        'legal_hold': disputed,
        'active_dispute': active_dispute_row,
        'disputes': [{**d, 'manifest': json_loads(d.get('manifest'))} for d in disputes],
        'references': refs,
        'reference_counts': reference_counts,
        'materials': manifest['files'],
        'manifest': manifest,
        'materials_complete': manifest['complete'],
        'review_materials_complete': manifest['complete'],
        'originals_available': can_download,
        'can_download_originals': can_download,
        'can_rerun': can_download,
        'originals_url': f"/api/batches/{batch_id}/originals",
        'download_url': f"/api/batches/{batch_id}/originals/download",
        'dispute_url': f"/api/batches/{batch_id}/dispute",
        'rerun_url': f"/api/batches/{batch_id}/rerun",
        'state': state,
        'retention_state': state,
        'status': status,
        'batch_status': status,
    }
    return result


def _with_certificate(database, result):
    full_certificate = certify(get_inspection(database, result['inspection_id']))
    certificate = full_certificate
    if result.get('cleaned') and not result.get('disputed'):
        certificate = {
            'inspection_id': full_certificate['inspection_id'],
            'approved': full_certificate['approved'],
            'blockers': full_certificate['blockers'],
            'integrity_sha256': full_certificate['integrity_sha256'],
            'conclusion_only': True,
        }
    conclusion = {
        'inspection_id': full_certificate['inspection_id'],
        'approved': full_certificate['approved'],
        'blockers': full_certificate['blockers'],
        'integrity_sha256': full_certificate['integrity_sha256'],
        'rule': full_certificate['record'].get('rule'),
        'generated_at': full_certificate['record'].get('generated_at'),
        'conclusion': '可签发' if full_certificate['approved'] else '待处置',
        'conclusion_text': '可签发' if full_certificate['approved'] else '待处置',
    }
    result['certificate'] = certificate
    result['conclusion'] = conclusion
    result['issuance_conclusion'] = conclusion
    return result


def batch_detail_if_exists(database, identifier, include_certificate=False):
    with retention_session(database) as db:
        row = _fetch_batch(db, identifier)
        if row is None:
            return None
        result = _batch_payload(db, row_dict(row))
    if include_certificate:
        result = _with_certificate(database, result)
    return result


def list_batches(database):
    with retention_session(database) as db:
        rows = [row_dict(r) for r in db.execute(
            'SELECT batch_id FROM retention_batches ORDER BY rowid DESC'
        ).fetchall()]
    batches = []
    for row in rows:
        try:
            batches.append(batch_detail(database, row['batch_id']))
        except InspectionError:
            continue
    return batches


def accept_dispute(database, batch_id=None, data=None, **kwargs):
    if isinstance(batch_id, dict) and data is None:
        data = batch_id
        batch_id = None
    merged = {**as_dict(data), **kwargs}
    batch_id = batch_id or pick(merged, ('batch_id', 'id', 'batch', 'inspection_id', 'inspection', 'certificate_id'))
    if not batch_id:
        raise InspectionError('BATCH_ID_REQUIRED', status=400)

    # Creates the retention projection from an inspection when necessary.
    detail = batch_detail(database, batch_id, include_certificate=False)
    real_batch_id = detail['batch_id']
    at = now_iso(pick(merged, ('accepted_at', 'now', 'created_at')))
    dispute_id = pick(merged, ('dispute_id', 'id')) or new_id('D')
    reason = pick(merged, ('reason', 'note', 'description', 'subject'), '签发争议留档')

    with retention_session(database) as db:
        existing = _active_dispute(_dispute_rows(db, real_batch_id))
        if existing is not None and not truthy(pick(merged, ('force',), False)):
            full = dispute_detail(database, existing['dispute_id'])
            full['created'] = False
            return full

        manifest = review_manifest(db, real_batch_id)
        if not manifest['complete']:
            raise InspectionError('REVIEW_MATERIALS_INCOMPLETE', source=real_batch_id, status=409)
        inspection_id = db.execute(
            'SELECT inspection_id FROM retention_batches WHERE batch_id=?', (real_batch_id,)
        ).fetchone()['inspection_id']
        db.execute(
            """
            INSERT INTO retention_disputes
                (dispute_id, batch_id, inspection_id, status, reason, materials_complete,
                 manifest, created_at, accepted_at)
            VALUES (?,?,?, 'accepted', ?, 1, ?, ?, ?)
            """,
            (dispute_id, real_batch_id, inspection_id, reason, json_dumps(manifest), at, at),
        )
        db.execute(
            "UPDATE retention_batches SET updated_at=? WHERE batch_id=?",
            (at, real_batch_id),
        )

    full = dispute_detail(database, dispute_id)
    full['created'] = True
    return full


def create_dispute(database, payload=None, **kwargs):
    return accept_dispute(database, data=payload, **kwargs)


def dispute_detail(database, dispute_id):
    with retention_session(database) as db:
        row = db.execute('SELECT * FROM retention_disputes WHERE dispute_id=?', (dispute_id,)).fetchone()
        if row is None:
            raise InspectionError('DISPUTE_NOT_FOUND', status=404)
        dispute = row_dict(row)
        dispute['id'] = dispute['dispute_id']
        dispute['state'] = dispute['status']
        dispute['accepted'] = dispute['status'] in ACTIVE_DISPUTE_STATUSES
        dispute['manifest'] = json_loads(dispute.get('manifest'))
        batch_row = _fetch_batch(db, dispute['batch_id'])
        batch = _batch_payload(db, row_dict(batch_row)) if batch_row else None
    if batch is not None:
        batch = _with_certificate(database, batch)
    dispute['batch'] = batch
    dispute['review_materials_complete'] = bool(dispute.get('materials_complete'))
    dispute['review_materials'] = dispute.get('manifest', {}).get('files', [])
    dispute['originals_url'] = f"/api/disputes/{dispute_id}/originals"
    dispute['download_url'] = f"/api/disputes/{dispute_id}/originals/download"
    dispute['status_label'] = '已受理' if dispute['status'] in ACTIVE_DISPUTE_STATUSES else '已结案'
    return dispute


def list_disputes(database):
    with retention_session(database) as db:
        ids = [r['dispute_id'] for r in db.execute('SELECT dispute_id FROM retention_disputes ORDER BY rowid DESC')]
    return [dispute_detail(database, dispute_id) for dispute_id in ids]


def close_dispute(database, dispute_id, payload=None, **kwargs):
    data = {**as_dict(payload), **kwargs}
    at = now_iso(pick(data, ('closed_at', 'now')))
    status = pick(data, ('status',), 'closed')
    if status not in CLOSED_DISPUTE_STATUSES:
        status = 'closed'
    resolution = pick(data, ('resolution', 'note', 'reason'), '争议处理完毕')
    with retention_session(database) as db:
        exists = db.execute('SELECT dispute_id FROM retention_disputes WHERE dispute_id=?', (dispute_id,)).fetchone()
        if exists is None:
            raise InspectionError('DISPUTE_NOT_FOUND', status=404)
        db.execute(
            'UPDATE retention_disputes SET status=?, resolution=?, closed_at=? WHERE dispute_id=?',
            (status, resolution, at, dispute_id),
        )
    return dispute_detail(database, dispute_id)


def list_originals(database):
    with retention_session(database) as db:
        original_rows = [row_dict(r) for r in db.execute(
            'SELECT sha256, size_bytes, status, created_at, purged_at FROM retention_originals ORDER BY rowid DESC'
        )]
        reference_rows = [row_dict(r) for r in db.execute(
            'SELECT batch_id, sha256, source, filename, status, released_at FROM retention_references ORDER BY batch_id, source'
        )]
    by_sha = {}
    for ref in reference_rows:
        by_sha.setdefault(ref['sha256'], []).append(ref)
    for original in original_rows:
        refs = by_sha.get(original['sha256'], [])
        original['references'] = refs
        original['reference_count'] = sum(1 for ref in refs if ref['status'] == 'active')
        original['active_references'] = original['reference_count']
        original['retained'] = original['status'] == 'active' and original['reference_count'] > 0
    return original_rows


def original_content(database, sha):
    with retention_session(database) as db:
        row = db.execute(
            'SELECT sha256, content, status FROM retention_originals WHERE sha256=?', (sha,)
        ).fetchone()
        if row is None:
            raise InspectionError('ORIGINAL_NOT_FOUND', status=404)
        item = row_dict(row)
    if item['status'] == 'purged' or item.get('content') is None:
        raise InspectionError(ORIGINAL_PURGED_CODE, status=410)
    return item


def add_reference(database, payload=None, **kwargs):
    data = {**as_dict(payload), **kwargs}
    sha = pick(data, ('sha256', 'sha', 'original_id', 'artifact_id', 'material_id', 'digest'))
    batch_id = pick(data, ('batch_id', 'id', 'batch', 'inspection_id'))
    if not batch_id:
        raise InspectionError('BATCH_ID_REQUIRED', status=400)
    at = now_iso(pick(data, ('now',)))
    due, due_provided = due_value(data)
    source = pick(data, ('source', 'kind'), 'bundle')
    content = pick(data, ('content', 'text', 'data'))
    filename = pick(data, ('filename', 'name'), f'{source}.bin')
    with retention_session(database) as db:
        if not sha and isinstance(content, str):
            sha = digest(content.encode())
        if not sha:
            raise InspectionError('ORIGINAL_ID_OR_CONTENT_REQUIRED', status=400)
        existing = db.execute('SELECT sha256 FROM retention_originals WHERE sha256=?', (sha,)).fetchone()
        if existing is None:
            upsert_original(db, sha, content if isinstance(content, str) else '', at, resurrect=True)
        elif isinstance(content, str):
            upsert_original(db, sha, content, at, resurrect=truthy(pick(data, ('resurrect',), False)))
        db.execute(
            """
            INSERT OR IGNORE INTO retention_references
                (batch_id, sha256, source, filename, status, created_at)
            VALUES (?,?,?,?, 'active', ?)
            """,
            (batch_id, sha, source, filename, at),
        )
        if due_provided:
            db.execute(
                """
                INSERT INTO retention_batches (batch_id, inspection_id, due_at, status, created_at, updated_at)
                VALUES (?,?,?, 'active', ?, ?)
                ON CONFLICT(batch_id) DO UPDATE SET due_at=excluded.due_at, updated_at=excluded.updated_at
                """,
                (batch_id, pick(data, ('inspection_id',), batch_id), due, at, at),
            )
        else:
            db.execute(
                """
                INSERT INTO retention_batches (batch_id, inspection_id, due_at, status, created_at, updated_at)
                VALUES (?,?,?, 'active', ?, ?)
                ON CONFLICT(batch_id) DO NOTHING
                """,
                (batch_id, pick(data, ('inspection_id',), batch_id), FAR_FUTURE, at, at),
            )
    return batch_detail(database, batch_id, include_certificate=False)


def original_bundle(database, identifier, *, dispute_id=None, source=None):
    dispute = None
    if dispute_id is not None:
        dispute = dispute_detail(database, dispute_id)
        identifier = dispute['batch_id']
        if dispute['status'] not in ACTIVE_DISPUTE_STATUSES:
            raise InspectionError('DISPUTE_CLOSED', status=403)

    detail = batch_detail(database, identifier, include_certificate=False)
    if source is not None:
        source = source[:-4] if source.endswith('.csv') else source
        if source not in SOURCES:
            raise InspectionError('UNKNOWN_SOURCE', status=404)

    if dispute is None and not detail['originals_available']:
        raise InspectionError(ORIGINAL_PURGED_CODE, source=detail['batch_id'], status=410)
    if dispute is not None and not detail['materials_complete']:
        raise InspectionError('REVIEW_MATERIALS_INCOMPLETE', source=detail['batch_id'], status=409)

    with retention_session(database) as db:
        files = []
        traces = {}
        for item in detail['manifest']['files']:
            if source is not None and item['source'] != source:
                continue
            original = db.execute(
                'SELECT content, status FROM retention_originals WHERE sha256=?', (item['sha256'],)
            ).fetchone()
            if original is None or original['status'] != 'active' or original['content'] is None:
                raise InspectionError(ORIGINAL_PURGED_CODE, source=item['source'], status=410)
            content = original['content']
            expected_sha = digest(content.encode())
            if expected_sha != item['sha256']:
                raise InspectionError('ORIGINAL_CHECKSUM_MISMATCH', source=item['source'], status=409)
            traces[item['source']] = content
            files.append({**item, 'content': content})

    if source is not None:
        item = files[0]
        return {
            'source': source,
            'filename': item['filename'],
            'content': item['content'],
            'sha256': item['sha256'],
        }

    certificate = certify(get_inspection(database, detail['inspection_id']))
    bundle = {
        'batch_id': detail['batch_id'],
        'inspection_id': detail['inspection_id'],
        'dispute_id': dispute_id or (detail.get('active_dispute') or {}).get('dispute_id'),
        'complete': True,
        'materials_complete': True,
        'review_materials_complete': True,
        'format': 'json',
        'filenames': {item['source']: item['filename'] for item in files},
        'traces': traces,
        'files': files,
        'review_materials': files,
        'manifest': detail['manifest'],
        'certificate': certificate,
        'conclusion': {key: certificate[key] for key in ('inspection_id', 'approved', 'blockers', 'integrity_sha256')},
    }
    return bundle


def _stored_settings(settings):
    def metres(value):
        return format(Decimal(int(value)) / Decimal(1000), 'f')

    return {
        'length_m': metres(settings['length_mm']),
        'zero_a_m': metres(settings['zero_a_mm']),
        'zero_b_m': metres(settings['zero_b_mm']),
        'pair_tol_m': metres(settings['pair_tol_mm']),
        'design_tol_m': metres(settings['design_tol_mm']),
        'reference_limit_db': format(Decimal(int(settings['reference_limit_mdb'])) / Decimal(1000), 'f'),
    }


def rerun_batch(database, identifier):
    detail = batch_detail(database, identifier)
    if not detail['can_rerun']:
        raise InspectionError(ORIGINAL_PURGED_CODE, source=detail['batch_id'], status=410)
    bundle = original_bundle(database, detail['batch_id'])
    inspection = get_inspection(database, detail['inspection_id'])
    materials = {'traces': bundle['traces'], 'settings': _stored_settings(inspection['settings'])}

    from app.modules.archive.service import archive

    manifest, fresh = archive(database, materials)
    certificate = certify(manifest)
    return {
        'rerun': True,
        'replayed': True,
        'restored_deleted_originals': False,
        'originals_restored': False,
        'fresh': fresh,
        'inspection_id': manifest['inspection_id'],
        'batch_id': detail['batch_id'],
        'certificate': certificate,
        'conclusion': certificate,
    }


# ---------------------------------------------------------------------------
# Cleanup planning, execution, ledger and recovery
# ---------------------------------------------------------------------------

def _get_run_row(db, run_id):
    row = db.execute('SELECT * FROM cleanup_runs WHERE run_id=?', (run_id,)).fetchone()
    if row is None:
        raise InspectionError('CLEANUP_RUN_NOT_FOUND', status=404)
    return row_dict(row)


def run_detail(database, run_id):
    with retention_session(database) as db:
        run = _get_run_row(db, run_id)
        entries = [row_dict(r) for r in db.execute(
            'SELECT * FROM cleanup_ledger WHERE run_id=? ORDER BY id', (run_id,)
        ).fetchall()]
    summary = json_loads(run.get('summary'))
    result = {**run, 'summary': summary, 'ledger': entries}
    if isinstance(summary, dict):
        result.update(summary)
        result['summary'] = summary
    result['id'] = run_id
    result['run_id'] = run_id
    result['completed'] = run['status'] in {'completed', 'posted'}
    result['posted'] = run['status'] == 'posted' or bool(run.get('posted_at'))
    return result


def list_runs(database):
    with retention_session(database) as db:
        ids = [r['run_id'] for r in db.execute('SELECT run_id FROM cleanup_runs ORDER BY rowid DESC')]
    return [run_detail(database, run_id) for run_id in ids]


def _ledger_upsert(db, run_id, action, target_type, target_id, at, detail=None):
    db.execute(
        """
        INSERT OR IGNORE INTO cleanup_ledger
            (run_id, action, target_type, target_id, status, detail, created_at, updated_at)
        VALUES (?,?,?,?, 'pending', ?, ?, ?)
        """,
        (run_id, action, target_type, target_id, json_dumps(detail or {}), at, at),
    )
    row = db.execute(
        'SELECT * FROM cleanup_ledger WHERE run_id=? AND action=? AND target_type=? AND target_id=?',
        (run_id, action, target_type, target_id),
    ).fetchone()
    return row_dict(row)


def cutoff_value(value=None):
    if value is None:
        return now_iso()
    if isinstance(value, str) and len(value.strip()) == 10:
        temporal(value)
        return value.strip()
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    return now_iso(value)


def sql_cutoff(value):
    cutoff = cutoff_value(value)
    if len(cutoff) == 10:
        return cutoff + 'T23:59:59.999999+00:00'
    return cutoff


def plan_cleanup(database, as_of=None, *, run_id=None, now=None):
    at = now_iso(now)
    cutoff = cutoff_value(as_of)
    cutoff_bound = sql_cutoff(cutoff)
    run_id = run_id or new_id('C')
    with retention_session(database) as db:
        db.execute(
            """
            INSERT INTO cleanup_runs (run_id, as_of, status, created_at, updated_at, summary)
            VALUES (?,?, 'planned', ?, ?, '{}')
            ON CONFLICT(run_id) DO NOTHING
            """,
            (run_id, cutoff, at, at),
        )
        due = db.execute(
            "SELECT batch_id FROM retention_batches WHERE due_at <= ? AND status='active' ORDER BY rowid",
            (cutoff_bound,),
        ).fetchall()
        for row in due:
            _ledger_upsert(db, run_id, 'release_batch', 'batch', row['batch_id'], at, {'as_of': cutoff})
    return run_detail(database, run_id)


def _has_active_dispute(db, batch_id):
    row = db.execute(
        f"SELECT 1 FROM retention_disputes WHERE batch_id=? AND status IN ({','.join('?' for _ in ACTIVE_DISPUTE_STATUSES)}) LIMIT 1",
        (batch_id, *ACTIVE_DISPUTE_STATUSES),
    ).fetchone()
    return row is not None


def _has_active_reference(db, sha):
    return db.execute(
        "SELECT 1 FROM retention_references WHERE sha256=? AND status='active' LIMIT 1", (sha,)
    ).fetchone() is not None


def _mark_ledger(db, ledger_id, status, at, *, reason=None, detail=None, error=None):
    db.execute(
        """
        UPDATE cleanup_ledger
           SET status=?, reason=COALESCE(?, reason), detail=?, attempts=attempts+1, updated_at=?
         WHERE id=?
        """,
        (status, reason, json_dumps(detail or {'error': error} if error else {}), at, ledger_id),
    )


def _reconcile_purge_items(db, run_id, at):
    orphans = db.execute(
        """
        SELECT o.sha256
          FROM retention_originals o
         WHERE o.status='active'
           AND NOT EXISTS (
               SELECT 1 FROM retention_references r
                WHERE r.sha256=o.sha256 AND r.status='active'
           )
         ORDER BY o.sha256
        """
    ).fetchall()
    for row in orphans:
        _ledger_upsert(db, run_id, 'purge_original', 'original', row['sha256'], at)


def execute_cleanup(database, run_id, *, interrupt_after=None, now=None, auto_post=True):
    at = now_iso(now)
    with retention_session(database) as db:
        run = _get_run_row(db, run_id)
        cutoff = run['as_of']
        cutoff_bound = sql_cutoff(cutoff)
        db.execute(
            "UPDATE cleanup_runs SET status='running', started_at=COALESCE(started_at,?), updated_at=?, error=NULL WHERE run_id=?",
            (at, at, run_id),
        )

    processed = 0
    try:
        # Re-plan due batches so a resumed/stale run catches new rows.
        with retention_session(database) as db:
            due = db.execute(
                "SELECT batch_id FROM retention_batches WHERE due_at <= ? AND status='active' ORDER BY rowid", (cutoff_bound,)
            ).fetchall()
            for row in due:
                _ledger_upsert(db, run_id, 'release_batch', 'batch', row['batch_id'], at)

        pending_releases = _pending_items(database, run_id, 'release_batch')
        for item in pending_releases:
            _execute_release(database, item, at)
            processed += 1
            if interrupt_after is not None and processed >= interrupt_after:
                raise RuntimeError('CLEANUP_INTERRUPTED')

        with retention_session(database) as db:
            _reconcile_purge_items(db, run_id, at)
        pending_purges = _pending_items(database, run_id, 'purge_original')
        for item in pending_purges:
            _execute_purge(database, item, at)
            processed += 1
            if interrupt_after is not None and processed >= interrupt_after:
                raise RuntimeError('CLEANUP_INTERRUPTED')

        redacted = redact_conclusion_only(database, at)
        summary = _build_summary(database, run_id, cutoff)
        summary['redacted_inspections'] = redacted
        summary['conclusion_only_inspections'] = redacted
        with retention_session(database) as db:
            db.execute(
                "UPDATE cleanup_runs SET status='completed', finished_at=?, updated_at=?, summary=? WHERE run_id=?",
                (at, at, json_dumps(summary), run_id),
            )
        if auto_post:
            return post_cleanup(database, run_id, now=at)
        return run_detail(database, run_id)
    except Exception as exc:
        error = getattr(exc, 'code', None) or str(exc)
        with retention_session(database) as db:
            db.execute(
                "UPDATE cleanup_runs SET status='interrupted', updated_at=?, error=? WHERE run_id=?",
                (at, error, run_id),
            )
        raise


def _pending_items(database, run_id, action):
    with retention_session(database) as db:
        rows = db.execute(
            "SELECT * FROM cleanup_ledger WHERE run_id=? AND action=? AND status='pending' ORDER BY id",
            (run_id, action),
        ).fetchall()
        return [row_dict(row) for row in rows]


def _execute_release(database, item, at):
    batch_id = item['target_id']
    with retention_session(database) as db:
        batch = _fetch_batch(db, batch_id)
        if batch is None:
            _mark_ledger(db, item['id'], 'skipped', at, reason='BATCH_NOT_FOUND')
            return
        if _has_active_dispute(db, batch_id):
            _mark_ledger(db, item['id'], 'skipped', at, reason='DISPUTE_HOLD',
                         detail={'batch_id': batch_id})
            return
        released = db.execute(
            "UPDATE retention_references SET status='released', released_at=? WHERE batch_id=? AND status='active'",
            (at, batch_id),
        ).rowcount
        db.execute(
            "UPDATE retention_batches SET status='expired', cleaned_at=COALESCE(cleaned_at,?), last_run_id=?, updated_at=? WHERE batch_id=?",
            (at, item['run_id'], at, batch_id),
        )
        _mark_ledger(db, item['id'], 'done', at, detail={'released_references': released})


def _execute_purge(database, item, at):
    sha = item['target_id']
    with retention_session(database) as db:
        if _has_active_reference(db, sha):
            holders = [r['batch_id'] for r in db.execute(
                "SELECT DISTINCT batch_id FROM retention_references WHERE sha256=? AND status='active'", (sha,)
            ).fetchall()]
            _mark_ledger(db, item['id'], 'skipped', at, reason='REFERENCE_HELD',
                         detail={'references': holders})
            return
        original = db.execute('SELECT status FROM retention_originals WHERE sha256=?', (sha,)).fetchone()
        if original is None:
            _mark_ledger(db, item['id'], 'done', at, reason='ALREADY_ABSENT')
            return
        if original['status'] != 'purged':
            db.execute(
                "UPDATE retention_originals SET content=NULL, size_bytes=0, status='purged', purged_at=? WHERE sha256=?",
                (at, sha),
            )
        _mark_ledger(db, item['id'], 'done', at, detail={'purged': True})


def _build_summary(database, run_id, cutoff):
    detail = run_detail(database, run_id)
    entries = detail['ledger']
    released = [e['target_id'] for e in entries if e['action'] == 'release_batch' and e['status'] == 'done']
    purged = [e['target_id'] for e in entries if e['action'] == 'purge_original' and e['status'] == 'done']
    protected = [e['target_id'] for e in entries if e['action'] == 'release_batch' and e['status'] == 'skipped']
    held = [e['target_id'] for e in entries if e['action'] == 'purge_original' and e['status'] == 'skipped']
    skipped = [{'id': e['target_id'], 'action': e['action'], 'reason': e['reason']}
               for e in entries if e['status'] == 'skipped']
    errors = [{'id': e['target_id'], 'action': e['action'], 'reason': e['reason']}
              for e in entries if e['status'] in {'failed'}]
    return {
        'run_id': run_id,
        'as_of': cutoff,
        'released_batches': released,
        'cleaned_batches': released,
        'expired_batches': released,
        'purged_originals': purged,
        'deleted_originals': purged,
        'permanently_purged': purged,
        'protected_batches': protected,
        'dispute_holds': protected,
        'held_originals': held,
        'retained_originals': held,
        'skipped': skipped,
        'errors': errors,
        'released_count': len(released),
        'purged_count': len(purged),
        'deleted_count': len(purged),
        'cleaned_count': len(released),
        'deleted': purged,
        'purged': purged,
        'cleaned': released,
        'preserved': sorted(set(protected + held)),
        'retained': sorted(set(protected + held)),
        'protected_count': len(protected) + len(held),
        'ledger_entries': len(entries),
    }


def redact_conclusion_only(database, at=None):
    """Remove shared full snapshots once no batch/reference/dispute remains."""
    at = now_iso(at)
    candidates = []
    with retention_session(database) as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inspections'").fetchone()
        if table is None:
            return []
        inspection_ids = [r['inspection_id'] for r in db.execute(
            'SELECT DISTINCT inspection_id FROM retention_batches'
        ).fetchall()]
        for inspection_id in inspection_ids:
            active_reference = db.execute(
                """
                SELECT 1
                  FROM retention_references r
                  JOIN retention_batches b ON b.batch_id = r.batch_id
                 WHERE b.inspection_id=? AND r.status='active'
                 LIMIT 1
                """,
                (inspection_id,),
            ).fetchone()
            if active_reference:
                continue
            dispute = db.execute(
                f"SELECT 1 FROM retention_disputes WHERE inspection_id=? AND status IN ({','.join('?' for _ in ACTIVE_DISPUTE_STATUSES)}) LIMIT 1",
                (inspection_id, *ACTIVE_DISPUTE_STATUSES),
            ).fetchone()
            if dispute:
                continue
            row = db.execute('SELECT document FROM inspections WHERE id=?', (inspection_id,)).fetchone()
            if row is not None:
                candidates.append((inspection_id, row['document']))

    redacted = []
    updates = []
    for inspection_id, document in candidates:
        record = json_loads(document)
        if record.get('retention_state') == 'conclusion_only':
            redacted.append(inspection_id)
            continue
        certificate = certify(record)
        compact = {
            'inspection_id': inspection_id,
            'rule': record.get('rule'),
            'generated_at': record.get('generated_at'),
            'duration_ms': record.get('duration_ms'),
            'traces_sha256': record.get('traces_sha256', {}),
            'retention_state': 'conclusion_only',
            'redacted_at': at,
            'certificate': {
                'approved': certificate['approved'],
                'blockers': certificate['blockers'],
                'integrity_sha256': certificate['integrity_sha256'],
            },
        }
        updates.append((canonical(compact), inspection_id))
        redacted.append(inspection_id)

    if updates:
        with retention_session(database) as db:
            for document, inspection_id in updates:
                db.execute('UPDATE inspections SET document=? WHERE id=?', (document, inspection_id))
    return redacted


def post_cleanup(database, run_id, *, now=None):
    at = now_iso(now)
    with retention_session(database) as db:
        run = _get_run_row(db, run_id)
        if run['status'] not in {'completed', 'posted'}:
            pending = db.execute(
                "SELECT COUNT(*) AS n FROM cleanup_ledger WHERE run_id=? AND status='pending'", (run_id,)
            ).fetchone()['n']
            if pending:
                raise InspectionError('CLEANUP_INCOMPLETE', status=409)
            summary = _build_summary(database, run_id, run['as_of'])
            db.execute(
                "UPDATE cleanup_runs SET status='completed', finished_at=COALESCE(finished_at,?), summary=? WHERE run_id=?",
                (at, json_dumps(summary), run_id),
            )
        db.execute(
            "UPDATE cleanup_runs SET status='posted', posted_at=COALESCE(posted_at,?), updated_at=? WHERE run_id=?",
            (at, at, run_id),
        )
    return run_detail(database, run_id)


def run_cleanup(database, payload=None, **kwargs):
    if not isinstance(payload, dict):
        payload = {'as_of': payload} if payload is not None else {}
    data = {**payload, **kwargs}
    as_of = pick(data, ('as_of', 'now', 'date', 'cutoff', 'cleanup_date', 'today'))
    run_id = pick(data, ('run_id', 'id'))
    interrupt_after = pick(data, ('interrupt_after', 'fail_after', 'crash_after'))
    if interrupt_after is not None:
        try:
            interrupt_after = int(interrupt_after)
        except (TypeError, ValueError):
            interrupt_after = None
    planned = plan_cleanup(database, as_of=as_of, run_id=run_id)
    return execute_cleanup(database, planned['run_id'], interrupt_after=interrupt_after)


cleanup_expired = run_cleanup
cleanup = run_cleanup
clean_expired = run_cleanup
purge_expired = run_cleanup


def resume_cleanup(database, run_id=None, payload=None, **kwargs):
    if isinstance(run_id, dict) and payload is None:
        payload = run_id
        run_id = None
    data = {**as_dict(payload), **kwargs}
    run_id = run_id or pick(data, ('run_id', 'id'))
    if run_id is None:
        with retention_session(database) as db:
            row = db.execute(
                """
                SELECT run_id FROM cleanup_runs
                 WHERE status IN ('planned','running','interrupted','failed')
                 ORDER BY rowid DESC LIMIT 1
                """
            ).fetchone()
        if row is not None:
            run_id = row['run_id']
    if run_id is None:
        return run_cleanup(database, data, **kwargs)
    interrupt_after = pick(data, ('interrupt_after', 'fail_after', 'crash_after'))
    if interrupt_after is not None:
        try:
            interrupt_after = int(interrupt_after)
        except (TypeError, ValueError):
            interrupt_after = None
    return execute_cleanup(database, run_id, interrupt_after=interrupt_after)


recover_cleanup = resume_cleanup
restore_after_interruption = resume_cleanup


# Descriptive aliases for callers/tests using the business language.
purge_expired_originals = run_cleanup
cleanup_expired_batches = run_cleanup
process_expirations = run_cleanup
run_retention = run_cleanup
recover = resume_cleanup
register_dispute = accept_dispute
open_dispute = accept_dispute
file_dispute = accept_dispute
get_batch = batch_detail
get_originals = original_bundle
download_originals = original_bundle
replay_batch = rerun_batch


settle_cleanup = post_cleanup
commit_cleanup = post_cleanup
