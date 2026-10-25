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

-- ============================================================
-- V4.4 E9-01: 环境读数表 (温湿度有效采样)
-- 无效读数不得进入本表 (REQ-E7-02), 有效采样经上层校验后落库。
-- 时间戳一律带时区 ISO 8601。received_at 由服务端接收同步时填充 (REQ-C4-01)；
-- Pi 本地写入时须为 NULL, 不得以本地时刻冒充服务端接收时间。
-- ============================================================
CREATE TABLE IF NOT EXISTS readings (
    reading_id         TEXT PRIMARY KEY,          -- R-<YYYYMMDD>-<uuid8>
    sensor_id          TEXT NOT NULL,             -- 来源: 硬件传感器识别
    temperature        REAL NOT NULL,             -- 有效温湿度读数
    humidity           REAL,                      -- 视传感器是否含湿度
    device_captured_at TEXT NOT NULL,             -- 设备采样时间 (带时区 ISO 8601)
    edge_received_at   TEXT,                      -- Pi 本地接收/写库时间 (带时区 ISO 8601)
    received_at        TEXT                       -- 仅服务端接收同步时填充; Pi 本地为 NULL
);

CREATE INDEX IF NOT EXISTS idx_readings_sensor_time
ON readings(sensor_id, device_captured_at);

-- ============================================================
-- V4.4 E9-02: 传感器环境告警表 (独立存储, 不与图像环境告警混用)
-- 与既有 events 表隔离 (独立表 + 独立 type 空间)。
-- reading_id 可空; 如提供, 由应用层校验关联有效性 (不加外键)。
-- ============================================================
CREATE TABLE IF NOT EXISTS sensor_alerts (
    alert_id           TEXT PRIMARY KEY,          -- E-<YYYYMMDD>-<uuid8>
    type               TEXT NOT NULL,             -- 传感器环境告警类型
    severity           TEXT,                      -- 预留严重度
    temperature        REAL,                      -- 触发读数快照
    humidity           REAL,
    threshold          REAL,                      -- 触发阈值快照 (REQ-E7-03)
    status             TEXT NOT NULL DEFAULT 'active',  -- active | resolved (REQ-E7-04)
    occurred_at        TEXT NOT NULL,             -- 告警发生/确认时间 (带时区 ISO 8601)
    resolved_at        TEXT,                      -- 告警恢复时间
    device_captured_at TEXT,                      -- 触发时采样时间
    received_at        TEXT,                      -- 仅服务端接收同步时填充; Pi 本地为 NULL
    reading_id         TEXT                       -- 关联触发读数 (可空, 应用层校验)
);

CREATE INDEX IF NOT EXISTS idx_sensor_status_time
ON sensor_alerts(status, occurred_at);