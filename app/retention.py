"""Compatibility facade for retention/dispute/cleanup workflows."""

from app.modules.retention.service import (
    accept_dispute,
    add_reference,
    batch_detail,
    cleanup_expired,
    close_dispute,
    create_batch,
    create_dispute,
    list_batches,
    list_disputes,
    list_originals,
    original_bundle,
    recover_cleanup,
    register_archive,
    rerun_batch,
    resume_cleanup,
    run_cleanup,
    run_detail,
)

__all__ = [
    'accept_dispute', 'add_reference', 'batch_detail', 'cleanup_expired',
    'close_dispute', 'create_batch', 'create_dispute', 'list_batches',
    'list_disputes', 'list_originals', 'original_bundle', 'recover_cleanup',
    'register_archive', 'rerun_batch', 'resume_cleanup', 'run_cleanup',
    'run_detail',
]
