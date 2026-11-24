import pytest

from app.modules.retention.service import (
    accept_dispute,
    plan_cleanup,
    execute_cleanup,
    resume_cleanup,
    run_cleanup,
)


def archive_clean(client):
    return client.post('/api/inspections', json={'sample': 'clean'}).json['inspection_id']


def make_shared_batches(client, inspection_id):
    for batch_id, due in (('due-batch', '2000-01-01'), ('dispute-batch', '2000-01-01')):
        response = client.post('/api/batches', json={
            'batch_id': batch_id,
            'inspection_id': inspection_id,
            'due_at': due,
        })
        assert response.status_code == 201
    dispute = client.post('/api/disputes', json={
        'batch_id': 'dispute-batch', 'reason': '法院复算',
    })
    assert dispute.status_code == 201
    return dispute.json['dispute_id']


def test_expired_batch_keeps_conclusion_but_not_original(client):
    inspection_id = archive_clean(client)
    client.post('/api/batches', json={
        'batch_id': 'old-batch', 'inspection_id': inspection_id, 'due_at': '2000-01-01',
    })

    result = client.post('/api/cleanup', json={'as_of': '2026-11-24'})
    assert result.status_code == 200
    assert result.json['released_batches'] == ['old-batch']

    batch = client.get('/api/batches/old-batch')
    assert batch.status_code == 200
    assert batch.json['status'] == 'expired'
    assert batch.json['originals_available'] is False
    assert batch.json['conclusion']['approved'] is True
    assert batch.json['conclusion']['integrity_sha256']

    conclusion = client.get('/api/certificate/' + inspection_id)
    assert conclusion.status_code == 200
    assert conclusion.json['inspection_id'] == inspection_id
    assert conclusion.json['conclusion_only'] is True

    stored = client.get('/api/inspections/' + inspection_id)
    assert stored.status_code == 200
    assert stored.json['retention_state'] == 'conclusion_only'
    assert 'computed' not in stored.json

    originals = client.get('/api/batches/old-batch/originals')
    assert originals.status_code == 410
    assert originals.json['code'] == 'ORIGINAL_PURGED'


def test_disputed_shared_batch_still_downloads_complete_originals(client):
    inspection_id = archive_clean(client)
    dispute_id = make_shared_batches(client, inspection_id)

    cleaned = client.post('/api/cleanup', json={'as_of': '2026-11-24'}).json
    assert cleaned['released_batches'] == ['due-batch']
    assert cleaned['protected_batches'] == ['dispute-batch']
    assert cleaned['purged_originals'] == []

    expired = client.get('/api/batches/due-batch/originals')
    assert expired.status_code == 410

    expired_certificate = client.get(f'/api/certificate/{inspection_id}?batch_id=due-batch').json
    assert expired_certificate['conclusion_only'] is True
    assert 'record' not in expired_certificate
    dispute_certificate = client.get(f'/api/certificate/{inspection_id}?batch_id=dispute-batch').json
    assert 'record' in dispute_certificate

    bundle = client.get('/api/disputes/' + dispute_id + '/originals')
    assert bundle.status_code == 200
    assert set(bundle.json['traces']) == {'a', 'b', 'design'}
    assert all(text.endswith('\n') or text for text in bundle.json['traces'].values())
    assert 'attachment;' in bundle.headers['Content-Disposition']

    batch_bundle = client.get('/api/batches/dispute-batch/originals')
    assert batch_bundle.status_code == 200
    assert set(batch_bundle.json['traces']) == {'a', 'b', 'design'}
    assert 'computed' in client.get('/api/inspections/' + inspection_id).json


def test_last_reference_expiring_without_dispute_permanently_purges(client):
    inspection_id = archive_clean(client)
    dispute_id = make_shared_batches(client, inspection_id)
    client.post('/api/cleanup', json={'as_of': '2026-11-24'})

    closed = client.post(f'/api/disputes/{dispute_id}/close', json={'resolution': '维持结论'})
    assert closed.status_code == 200

    second = client.post('/api/cleanup', json={'as_of': '2026-11-25'}).json
    assert set(second['released_batches']) == {'dispute-batch'}
    assert len(second['purged_originals']) == 3

    assert client.get('/api/batches/dispute-batch/originals').status_code == 410
    assert client.get('/api/disputes/' + dispute_id + '/originals').status_code == 403
    redacted = client.get('/api/inspections/' + inspection_id).json
    assert redacted['retention_state'] == 'conclusion_only'
    assert 'computed' not in redacted


def test_unexpired_reference_protects_shared_original(client):
    inspection_id = archive_clean(client)
    client.post('/api/batches', json={
        'batch_id': 'due', 'inspection_id': inspection_id, 'due_at': '2000-01-01',
    })
    client.post('/api/batches', json={
        'batch_id': 'future', 'inspection_id': inspection_id, 'due_at': '2999-01-01',
    })

    result = client.post('/api/cleanup', json={'as_of': '2026-11-24'}).json
    assert result['released_batches'] == ['due']
    assert result['purged_originals'] == []
    assert result['held_originals'] == []

    assert client.get('/api/batches/due/originals').status_code == 410
    future = client.get('/api/batches/future/originals')
    assert future.status_code == 200
    assert set(future.json['traces']) == {'a', 'b', 'design'}


def test_interrupted_cleanup_can_resume(client, tmp_path):
    database = tmp_path / 'inspection.sqlite'
    inspection_id = client.post('/api/inspections', json={'sample': 'clean'}).json['inspection_id']
    client.post('/api/batches', json={
        'batch_id': 'interruptible', 'inspection_id': inspection_id, 'due_at': '2000-01-01',
    })

    with pytest.raises(RuntimeError, match='CLEANUP_INTERRUPTED'):
        run_cleanup(database, {'as_of': '2026-11-24', 'interrupt_after': 1})

    resumed = resume_cleanup(database)
    assert resumed['status'] == 'posted'
    assert resumed['released_batches'] == ['interruptible']
    assert len(resumed['purged_originals']) == 3
    assert client.get('/api/batches/interruptible/originals').status_code == 410


def test_dispute_accepted_after_plan_before_post_is_held(client, tmp_path):
    database = tmp_path / 'inspection.sqlite'
    inspection_id = client.post('/api/inspections', json={'sample': 'clean'}).json['inspection_id']
    client.post('/api/batches', json={
        'batch_id': 'late-hold', 'inspection_id': inspection_id, 'due_at': '2000-01-01',
    })
    plan = plan_cleanup(database, '2026-11-24', run_id='run-late-hold')
    assert plan['ledger'][0]['status'] == 'pending'

    accept_dispute(database, {'batch_id': 'late-hold', 'reason': '受理在落账前'})
    executed = execute_cleanup(database, 'run-late-hold')
    assert executed['protected_batches'] == ['late-hold']
    assert executed['released_batches'] == []
    assert executed['purged_originals'] == []
    assert client.get('/api/batches/late-hold/originals').status_code == 200


def test_old_archive_replay_does_not_restore_purged_original(client):
    payload = {'sample': 'clean'}
    inspection_id = client.post('/api/inspections', json=payload).json['inspection_id']
    client.post('/api/batches', json={
        'batch_id': 'replay-batch', 'inspection_id': inspection_id, 'due_at': '2000-01-01',
    })
    client.post('/api/cleanup', json={'as_of': '2026-11-24'})
    assert client.get('/api/batches/replay-batch/originals').status_code == 410

    replay = client.post('/api/inspections', json=payload)
    assert replay.status_code == 200
    assert replay.json['created'] is False
    assert all(item['status'] == 'purged' for item in client.get('/api/originals').json['originals'])
    assert client.get('/api/batches/replay-batch/originals').status_code == 410

    rerun = client.post('/api/batches/replay-batch/rerun')
    assert rerun.status_code == 410
    assert rerun.json['code'] == 'ORIGINAL_PURGED'


def test_issuance_page_shows_conclusion_dispute_and_download(client):
    inspection_id = archive_clean(client)
    response = client.get('/inspect/certificate?inspection=' + inspection_id)
    assert response.status_code == 200
    text = response.data.decode()
    assert '到期清理 / 争议留档' in text
    assert '受理争议并保留完整原件' in text
    assert '/api/batches/' + inspection_id + '/originals' in text
