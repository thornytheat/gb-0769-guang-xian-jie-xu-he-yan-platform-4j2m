from time import perf_counter

from app.modules.retention.service import (
    accept_dispute,
    cleanup_expired,
    create_batch,
    original_bundle,
    register_archive,
    resume_cleanup,
    run_cleanup,
)
from app.pipeline import run
from .identity import record
from .storage import publish


def archive(database, materials):
    start = perf_counter()
    results = run(materials)
    manifest, fresh = publish(database, record(materials, results, start))
    register_archive(database, manifest, materials, fresh=fresh)
    return manifest, fresh


cleanup = run_cleanup
clean_expired = cleanup_expired
dispute = accept_dispute
resume = resume_cleanup
download_originals = original_bundle
register_batch = create_batch
