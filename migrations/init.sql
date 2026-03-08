-- Autonomous Atlassian Task - Database Schema

-- Repositories: registered project repositories
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

-- Agent profiles: configurable CLI agent definitions
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

-- Jira label-to-repo mapping
CREATE TABLE IF NOT EXISTS label_repo_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jira_label TEXT NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Key-value settings (Jira credentials, etc.)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Runs: a collection of tickets to execute
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    epic_key TEXT,
    max_parallel INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'idle',
    project_path TEXT,
    parent_branch TEXT,
    repository_id INTEGER REFERENCES repositories(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tickets: individual work items within a run
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    jira_key TEXT NOT NULL,
    summary TEXT,
    state TEXT NOT NULL DEFAULT 'todo',
    rank INTEGER NOT NULL DEFAULT 0,
    branch_name TEXT,
    worktree_path TEXT,
    pr_url TEXT,
    pr_number INTEGER,
    worker_pid INTEGER,
    paused BOOLEAN DEFAULT FALSE,
    log_file TEXT,
    repository_id INTEGER REFERENCES repositories(id),
    parent_branch TEXT,
    profile_id INTEGER REFERENCES agent_profiles(id),
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(run_id, jira_key)
);

-- Schedules: timed execution of runs
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    schedule_type TEXT NOT NULL,
    cron_expression TEXT,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    next_run TIMESTAMP,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Logs: append-only terminal output per ticket
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    line TEXT NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_tickets_run_id ON tickets(run_id);
CREATE INDEX IF NOT EXISTS idx_tickets_state ON tickets(state);
CREATE INDEX IF NOT EXISTS idx_logs_ticket_id ON logs(ticket_id);
CREATE INDEX IF NOT EXISTS idx_schedules_run_id ON schedules(run_id);
