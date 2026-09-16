from pathlib import Path
import sqlite3


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL DEFAULT 'legacy',
    recipient TEXT,
    status TEXT NOT NULL,
    transcript_path TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at
ON jobs (status, created_at);
CREATE TABLE IF NOT EXISTS memos (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT,
    transcript_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    source TEXT NOT NULL,
    duration_seconds REAL,
    language TEXT,
    speaker_count INTEGER,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    audio_deleted_at TEXT,
    email_sent_at TEXT,
    notion_page_id TEXT,
    notion_url TEXT,
    notion_published_at TEXT,
    action_items_json TEXT NOT NULL DEFAULT '[]',
    decisions_json TEXT NOT NULL DEFAULT '[]',
    topics_json TEXT NOT NULL DEFAULT '[]',
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_memos_created_at ON memos (created_at DESC);
CREATE TABLE IF NOT EXISTS app_preferences (
    id TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recording_sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    client_id TEXT NOT NULL DEFAULT 'legacy',
    original_filename TEXT NOT NULL,
    recipient TEXT,
    status TEXT NOT NULL,
    final_chunk_index INTEGER,
    job_id TEXT UNIQUE,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recording_sessions_status_updated_at
ON recording_sessions (status, updated_at);
CREATE TABLE IF NOT EXISTS recording_chunks (
    session_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    stored_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    transcript_path TEXT,
    language TEXT,
    duration_seconds REAL,
    speaker_count INTEGER,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, chunk_index),
    FOREIGN KEY (session_id) REFERENCES recording_sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_recording_chunks_status_created_at
ON recording_chunks (status, created_at);
CREATE TABLE IF NOT EXISTS gemini_deliveries (
    job_id TEXT PRIMARY KEY,
    transcript_path TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_at TEXT,
    conversation_url TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_gemini_deliveries_status_next_attempt
ON gemini_deliveries (status, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS notion_deliveries (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    page_id TEXT,
    page_url TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    transcript_hash TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_notion_deliveries_status_next_attempt
ON notion_deliveries (status, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS notion_audio_jobs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    session_id TEXT,
    revision TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    state_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, revision),
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_notion_audio_jobs_due
ON notion_audio_jobs (status, next_attempt_at);
CREATE TABLE IF NOT EXISTS notion_live_sessions (
    session_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    state_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES recording_sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_notion_live_sessions_due
ON notion_live_sessions (status, next_attempt_at);
CREATE TABLE IF NOT EXISTS gemini_worker_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_submission_at TEXT,
    challenge_count INTEGER NOT NULL DEFAULT 0,
    last_challenge_at TEXT,
    blocked_until TEXT
);
INSERT OR IGNORE INTO gemini_worker_state (id, challenge_count)
VALUES (1, 0);
"""


def connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(database_path: Path) -> None:
    connection = connect(database_path)
    with connection:
        connection.executescript(SCHEMA_SQL)
        for table, column, definition in (
            ("jobs", "client_id", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("jobs", "recipient", "TEXT"),
            ("recording_sessions", "client_id", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("recording_sessions", "recipient", "TEXT"),
            ("memos", "notion_page_id", "TEXT"),
            ("memos", "notion_url", "TEXT"),
            ("memos", "notion_published_at", "TEXT"),
            ("memos", "action_items_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("memos", "decisions_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("memos", "topics_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("notion_deliveries", "transcript_hash", "TEXT"),
        ):
            columns = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    connection.close()
