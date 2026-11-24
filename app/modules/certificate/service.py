from app.modules.archive.identity import canonical, digest


def certify(record):
    if record.get('retention_state') == 'conclusion_only' or 'computed' not in record:
        saved = record.get('certificate') or {}
        return {
            'inspection_id': record['inspection_id'],
            'approved': saved.get('approved'),
            'blockers': saved.get('blockers', []),
            'integrity_sha256': saved.get('integrity_sha256') or digest(canonical(record)),
            'conclusion_only': True,
            'record': record,
        }

    stages = record['computed']
    design = stages['design']
    blockers = []
    if design['missing']:
        blockers.append('MISSING_JOINTS')
    if design['over_limit']:
        blockers.append('LOSS_OVER_LIMIT')
    if not design['design_count']:
        blockers.append('EMPTY_DESIGN')
    if any(line['source'] == 'design' and line['code'] != 'DUPLICATE_EVENT' for line in stages['trace']['isolated']):
        blockers.append('UNVERIFIED_DESIGN_ROWS')
    return {'inspection_id':record['inspection_id'], 'approved': not blockers,
            'blockers':blockers, 'integrity_sha256':digest(canonical(record)), 'record':record}
