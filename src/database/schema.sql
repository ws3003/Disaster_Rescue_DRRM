CREATE TABLE IF NOT EXISTS persons (
    person_id    INTEGER PRIMARY KEY,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    latest_state TEXT,
    latest_risk  REAL
);

CREATE TABLE IF NOT EXISTS events (
    event_id  TEXT PRIMARY KEY,
    ts        TEXT NOT NULL,
    type      TEXT NOT NULL,
    severity  TEXT,
    person_id INTEGER,
    payload   TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    trigger_event TEXT,
    plan_trace    TEXT,
    status        TEXT DEFAULT 'running',
    created       TEXT,
    finished      TEXT
);

CREATE TABLE IF NOT EXISTS agent_logs (
    log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    ts         TEXT NOT NULL,
    tool_name  TEXT NOT NULL,
    input      TEXT,
    output     TEXT,
    latency_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_logs_task
ON agent_logs(task_id);