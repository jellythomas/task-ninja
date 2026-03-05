-- Autonomous Atlassian Task - Database Schema

-- Runs: a collection of tickets to execute
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    epic_key TEXT,
    max_parallel INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL DEFAULT 'idle',
    project_path TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tickets: individual work items within a run
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    jira_key TEXT NOT NULL,
    summary TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    rank INTEGER NOT NULL DEFAULT 0,
    branch_name TEXT,
    worktree_path TEXT,
    pr_url TEXT,
    pr_number INTEGER,
    worker_pid INTEGER,
    paused BOOLEAN DEFAULT FALSE,
    log_file TEXT,
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
