"""Retention helpers kept beside archive for feature-discovery compatibility."""

from app.modules.retention.service import (
    accept_dispute,
    cleanup_expired,
    create_batch,
    original_bundle,
    plan_cleanup,
    purge_expired_originals,
    recover_cleanup,
    register_archive,
    rerun_batch,
    resume_cleanup,
    run_cleanup,
    run_detail,
)

cleanup = run_cleanup
dispute = accept_dispute
retain = accept_dispute
release = cleanup_expired
download = original_bundle
rerun = rerun_batch

__all__ = [
    'accept_dispute', 'cleanup', 'cleanup_expired', 'create_batch', 'dispute',
    'download', 'original_bundle', 'plan_cleanup', 'purge_expired_originals',
    'recover_cleanup', 'register_archive', 'release', 'rerun', 'rerun_batch',
    'resume_cleanup', 'run_cleanup', 'run_detail', 'retain',
]
