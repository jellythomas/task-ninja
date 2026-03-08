-- V2 Migration: Project-based board, agent profiles, settings
-- Safe to run multiple times (uses IF NOT EXISTS / ignores errors on ALTER)

-- New tables
CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    default_branch TEXT DEFAULT 'main',
    default_profile_id INTEGER,
    is_deleted INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    command TEXT NOT NULL,
    args_template TEXT NOT NULL,
    log_format TEXT DEFAULT 'plain-text',
    is_default INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS label_repo_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jira_label TEXT NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Rename pending -> todo in existing tickets
UPDATE tickets SET state = 'todo' WHERE state = 'pending';

-- Note: indexes on new columns are created in state.py after ALTER TABLE
