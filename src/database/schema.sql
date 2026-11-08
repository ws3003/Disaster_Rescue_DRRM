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

-- ============================================================
-- V4.4 E9-03: 媒体取证表 (图片/视频证据记录)
-- 记录媒体证据与告警的关系、本地位置、上传结果与任务状态。
-- 约束/语义:
--   - media_id        唯一证据标识 (候选格式 M-<YYYYMMDD>-<uuid8>)
--   - source          本期仅 'sensor' (关联传感器(环境)告警)
--   - ref_id          归属告警; 本期为 sensor_alerts.alert_id; 应用层校验存在, 不加外键
--   - media_type      image | video
--   - status          主状态四态 (pending/processing/success/failed-awaiting-retry)
--   - success 为终态, 不可回退 (由 DAO 状态转换强制)
--   - capture_result / upload_status 为辅助状态 (可空)
--   - 时间字段带时区 ISO 8601 (captured_at/created_at 必填; uploaded_at 可空)
--   - 不存储 received_at(C4 服务端副本) 与 oss_url(未授权长期 URL, 依 H-7)
-- ============================================================
CREATE TABLE IF NOT EXISTS media_evidence (
    media_id       TEXT PRIMARY KEY,            -- M-<YYYYMMDD>-<uuid8>
    source         TEXT NOT NULL CHECK (source IN ('sensor', 'person')),
    ref_id         TEXT NOT NULL,               -- 归属告警 (sensor_alerts.alert_id / events.event_id)
    media_type     TEXT NOT NULL CHECK (media_type IN ('image', 'video')),
    local_path     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'processing', 'success', 'failed-awaiting-retry')),
    capture_result TEXT CHECK (capture_result IN ('ok', 'failed')),          -- 可空
    upload_status  TEXT CHECK (upload_status IN ('pending', 'uploading', 'success', 'failed')),  -- 可空
    object_key     TEXT,                        -- OSS 对象标识 (H-7 未定时不存 URL)
    captured_at    TEXT NOT NULL,               -- 采集时间 (带时区 ISO 8601)
    created_at     TEXT NOT NULL,               -- 登记时间 (带时区 ISO 8601)
    uploaded_at    TEXT,                        -- 上传完成时间 (可空, 带时区 ISO 8601)
    retry_count    INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    last_error     TEXT
);

CREATE INDEX IF NOT EXISTS idx_media_ref_status
ON media_evidence(ref_id, status);

-- ============================================================
-- V4.4 P1-B: 投递登记表 (deliveries)
-- 记录某告警实体针对某类投递任务的一次性幂等登记。
-- 约束/语义:
--   - source        实体来源: person=人员事件(events.event_id) / sensor=传感器告警(sensor_alerts.alert_id);
--                     两域共用于 E- 前缀, 必须以 source 消歧 (契约: alert_key = person:<id>|sensor:<id>)
--   - alert_id      目标告警/事件的原始 ID (复合键成分, 不加外键, 与项目全库无 FK 惯例一致)
--   - task_kind     投递任务类别, 开放 TEXT (不设枚举): 合法取值待真实投递消费方明确后再定义, 禁臆造
--   - status        主状态两态 pending/success; 仅 pending -> success, success 为终态 (由 DAO 状态转换强制)
--                     success 仅表示本期记录被标记成功, 不代表远端已收到/处理
--   - created_at    登记时间 (带时区 ISO 8601)
--   - 复合主键 (source, alert_id, task_kind) = 库层幂等唯一性兜底;
--                     未来如需重投, 须另行设计重投模型 (本表同一复合键至多一条)
-- ============================================================
CREATE TABLE IF NOT EXISTS deliveries (
    source     TEXT NOT NULL CHECK (source IN ('person', 'sensor')),
    alert_id   TEXT NOT NULL,
    task_kind  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'success')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (source, alert_id, task_kind)
);

-- ============================================================
-- V4.4 C4-01: 单向同步 C4 Outbox 待发队列
-- 记录一条业务记录的首次同步任务、发送生命周期、重试与恢复信息。
-- 约束/语义:
--   - id                 任务主键 (领取/续租/成功/失败/死信 以 id+status+gen 条件更新)
--   - record_type        业务类型 (first snapshot; person_event/reading/sensor_alert)
--   - source_record_id   源记录主键 (事件 event_id / 读数 reading_id / 告警 alert_id)
--   - UNIQUE(record_type, source_record_id) = 一业务记录一首次同步任务 (业务级唯一)
--   - status             pending/processing/retry_wait/success/dead_letter (success 终态)
--   - gen                单调递增 fencing 代数 (领取时 +1; 续租不变)
--   - worker_id          领取 worker 诊断; lease_until 租约到期(UTC epoch ms)
--   - next_attempt_at    UTC epoch ms; pending=NULL=可立即领取; retry_wait NULL=异常(不静默重试)
--   - retry_count        重试次数, 不含首次发送
--   - last_error         最近失败摘要 (不含敏感凭据)
--   - 时间字段统一 UTC epoch 毫秒整数 (不改变既有业务表时间格式)
-- ============================================================
CREATE TABLE IF NOT EXISTS sync_outbox (
    id                INTEGER PRIMARY KEY,
    record_type       TEXT NOT NULL,
    source_record_id  TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','processing','retry_wait','success','dead_letter')),
    gen               INTEGER NOT NULL DEFAULT 0,
    worker_id         TEXT,
    lease_until       INTEGER,
    next_attempt_at   INTEGER,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT,
    created_at        INTEGER NOT NULL,
    completed_at      INTEGER,
    UNIQUE (record_type, source_record_id)
);

CREATE INDEX IF NOT EXISTS idx_outbox_status_next
ON sync_outbox(status, next_attempt_at);

CREATE INDEX IF NOT EXISTS idx_outbox_status_lease
ON sync_outbox(status, lease_until);

-- ============================================================
-- V4.4 C4-01: 服务端幂等登记 (首次内容哈希)
-- 业务写 + 幂等登记同事务; 保持首次 content_hash; 不设 TTL/不自动清理。
-- ============================================================
CREATE TABLE IF NOT EXISTS c4_idem (
    id                INTEGER PRIMARY KEY,
    record_type       TEXT NOT NULL,
    source_record_id  TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    received_at       INTEGER NOT NULL,
    edge_id           TEXT,
    UNIQUE (record_type, source_record_id)
);

-- ============================================================
-- V4.4 C4-01: 内容冲突审计 (同键异内容, 同事务追加)
-- 记录身份/首本次哈希/时间/差异摘要, 保留历史; 不设 TTL/不清理。
-- ============================================================
CREATE TABLE IF NOT EXISTS c4_content_conflict (
    id                INTEGER PRIMARY KEY,
    record_type       TEXT NOT NULL,
    source_record_id  TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    conflict_hash     TEXT NOT NULL,
    detected_at       INTEGER NOT NULL,
    edge_id           TEXT,
    diff_summary      TEXT
);

CREATE INDEX IF NOT EXISTS idx_c4_conflict_key
ON c4_content_conflict(record_type, source_record_id);

-- ============================================================
-- V4.4 C4-01 阶段 2: 待核验记录表 (c4_verify)
-- 规范化失败 / 存在待核验原因时, 不进入自动合并(enqueue_sync), 记入本表待人工核验。
-- 约束/语义:
--   - record_type + source_record_id 唯一: 一业务键至多一条待核验记录。
--   - reasons         稳定序列化的原因码集合 (含多个, canonical_reasons_json)。
--   - business_json   完整原始载荷快照 (含未知键/benign 键, 供人工复核, 不裁剪)。
--   - created_at/updated_at  UTC epoch 毫秒整数。
-- ============================================================
CREATE TABLE IF NOT EXISTS c4_verify (
    id               INTEGER PRIMARY KEY,
    record_type      TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    reasons          TEXT NOT NULL,
    business_json    TEXT,
    edge_id          TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    UNIQUE (record_type, source_record_id)
);

CREATE INDEX IF NOT EXISTS idx_c4_verify_key
ON c4_verify(record_type, source_record_id);

-- ======================================================================
-- V4.4 微信用户认证 (login / me / logout)
-- 设计: users(user_id, wechat_openid 唯一) + sessions(session_token 仅存 SHA-256 哈希)
-- 说明: session_token_hash 只存哈希; revoked_at 非空=已撤销; expires_at 判断过期。
-- ======================================================================
CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT PRIMARY KEY,            -- U-<YYYYMMDD>-<uuid8>
    wechat_openid TEXT NOT NULL UNIQUE,        -- 微信 openid, 一 openid 一内部用户
    created_at    TEXT NOT NULL                -- 带时区 ISO 8601
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,         -- S-<YYYYMMDD>-<uuid8>
    user_id          TEXT NOT NULL,
    session_token_hash TEXT NOT NULL UNIQUE,   -- 仅存 SHA-256 哈希, 不存明文 Token
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    revoked_at       TEXT,                     -- NULL=未撤销; 非空=已撤销
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id
ON sessions(user_id);

-- ============================================================
-- V4.4 E7-07: 传感器设备状态与故障历史（A3）
-- sensor_status: 每传感器一行当前状态；程序重启后据此恢复离线/降质状态。
--   status: online | degraded | offline
--       degraded  = 读到数据但质量不合格(如越界/非法值)；不视为设备断线
--       offline   = 设备级连续读取失败达到 offline_after_failures
--   fault_type: device(读取故障) | data_quality(数据质量问题)（与 VLM 图像环境告警独立）
-- sensor_fault_events: 故障发生/恢复历史（不删、不改已有表）
-- 以下均为新增表（IF NOT EXISTS 幂等），符合"新增表迁移"最小方案。
-- ============================================================
CREATE TABLE IF NOT EXISTS sensor_status (
    sensor_id            TEXT PRIMARY KEY,
    status               TEXT NOT NULL CHECK (status IN ('online','degraded','offline')),
    fault_type           TEXT,
    last_error           TEXT,
    first_fault_at       TEXT,
    last_fault_at        TEXT,
    recovered_at         TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    quality_failures     INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sensor_status_updated
ON sensor_status(updated_at);

CREATE TABLE IF NOT EXISTS sensor_fault_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id    TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('online','degraded','offline')),
    fault_type   TEXT,
    detail       TEXT,
    occurred_at  TEXT NOT NULL,
    recovered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sensor_fault_sensor_time
ON sensor_fault_events(sensor_id, occurred_at);
