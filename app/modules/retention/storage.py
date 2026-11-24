import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from app.errors import InspectionError

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS retention_batches (
        batch_id TEXT PRIMARY KEY,
        inspection_id TEXT NOT NULL,
        due_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        cleaned_at TEXT,
        last_run_id TEXT,
        metadata TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_retention_batches_inspection ON retention_batches(inspection_id)",
    "CREATE INDEX IF NOT EXISTS idx_retention_batches_due ON retention_batches(due_at, status)",
    """
    CREATE TABLE IF NOT EXISTS retention_originals (
        sha256 TEXT PRIMARY KEY,
        content TEXT,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        purged_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS retention_references (
        batch_id TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        source TEXT NOT NULL,
        filename TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        released_at TEXT,
        PRIMARY KEY (batch_id, source, sha256)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_retention_references_sha ON retention_references(sha256, status)",
    """
    CREATE TABLE IF NOT EXISTS retention_disputes (
        dispute_id TEXT PRIMARY KEY,
        batch_id TEXT NOT NULL,
        inspection_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'accepted',
        reason TEXT,
        materials_complete INTEGER NOT NULL DEFAULT 1,
        manifest TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        accepted_at TEXT,
        closed_at TEXT,
        resolution TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_retention_disputes_batch ON retention_disputes(batch_id, status)",
    """
    CREATE TABLE IF NOT EXISTS cleanup_runs (
        run_id TEXT PRIMARY KEY,
        as_of TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'planned',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        posted_at TEXT,
        summary TEXT NOT NULL DEFAULT '{}',
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cleanup_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        reason TEXT,
        detail TEXT NOT NULL DEFAULT '{}',
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (run_id, action, target_type, target_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cleanup_ledger_run ON cleanup_ledger(run_id, status)",
)


def init_retention(db):
    for statement in SCHEMA:
        db.execute(statement)


@contextmanager
def retention_session(database):
    if str(database) != ':memory:':
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=15)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA secure_delete=ON')
        init_retention(connection)
        with connection:
            yield connection


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def json_loads(value, default=None):
    if not value:
        return {} if default is None else default
    return json.loads(value)


def not_found(code='RETENTION_NOT_FOUND'):
    raise InspectionError(code, status=404)
