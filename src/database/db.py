"""
Database Layer
--------------
SQLite persistence layer for the Disaster Rescue DRRM system.

Responsibilities:
1. Manage SQLite database connection.
2. Enable WAL mode.
3. Initialize required tables from schema.sql.
4. Save and retrieve events.
5. Upsert personnel state.
6. Save or update tasks.
7. Save agent execution logs.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# V4.4 E9-03 媒体取证 DAO 常量 (module-level, 供媒体方法引用)
_MEDIA_SOURCES = frozenset({"sensor"})
_MEDIA_TYPES = frozenset({"image", "video"})
_MEDIA_STATUS = frozenset({"pending", "processing", "success", "failed-awaiting-retry"})
_MEDIA_CAPTURE_RESULTS = frozenset({"ok", "failed"})
_MEDIA_UPLOAD_STATUS = frozenset({"pending", "uploading", "success", "failed"})
# 允许的主状态转换; success 为终态不可回退
_MEDIA_TRANSITIONS = {
    "pending": frozenset({"processing"}),
    "processing": frozenset({"success", "failed-awaiting-retry"}),
    "failed-awaiting-retry": frozenset({"processing"}),
}
_UM = object()  # 哨兵: update 的参数未提供(不更新该字段)

# V4.4 P1-B 投递登记表 DAO 常量 (module-level, 供 delivery 方法引用)
_DELIVERY_SOURCES = frozenset({"person", "sensor"})
_DELIVERY_STATUS = frozenset({"pending", "success"})
# 允许的主状态转换; success 为终态不可回退
_DELIVERY_TRANSITIONS = {
    "pending": frozenset({"success"}),
}

# V4.4 C4-01 Outbox 常量 (module-level)
_OUTBOX_STATUS = frozenset({"pending", "processing", "retry_wait", "success", "dead_letter"})
_C4_RECORD_TYPES = frozenset({"person_event", "reading", "sensor_alert"})


class Database:
    """
    SQLite database interface for the DRRM system.
    """

    def __init__(
        self,
        db_path: str = "data/drrm.db",
    ):
        self.db_path = Path(
            db_path
        )

        # Make sure the database directory exists.
        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.schema_path = (
            Path(__file__).resolve().parent
            / "schema.sql"
        )

        self._tx = False  # with_transaction 同实例嵌套保护标志(P1-A)

        self._initialize_database()

    # ================================================================
    # Connection
    # ================================================================

    def _connect(self, timeout: float = 5.0) -> sqlite3.Connection:
        """
        Create a SQLite connection.

        Row factory is enabled so database rows can be converted
        directly into dictionaries.

        timeout: SQLite busy-wait seconds.  Default 5.0 matches the
        sqlite3 default, so existing callers are unchanged; callers that
        need a shorter lock wait (e.g. the X4 inference log writer) pass
        an explicit value.  WAL / PRAGMA behavior is unchanged.
        """

        conn = sqlite3.connect(
            self.db_path,
            timeout=timeout,
        )

        conn.row_factory = (
            sqlite3.Row
        )

        # Enable WAL mode.
        conn.execute(
            "PRAGMA journal_mode=WAL;"
        )

        conn.execute(
            "PRAGMA foreign_keys=ON;"
        )

        return conn

    @contextmanager
    def with_transaction(self):
        """同一连接上的共享事务上下文（SQLite，P1-A）。

        用法: with db.with_transaction() as conn: conn.execute(...)

        - 复用 self._connect() 获取一个连接并交付调用方。
        - 正常退出提交; 事务体异常回滚并传播原始业务异常。
        - 提交异常: 尽力回滚, 传播提交异常(不解释为"一定已提交/一定未写")。
        - 所有退出路径尝试 close(), 并将 self._tx 复位为 False。
        - 同一 Database 实例内禁止嵌套; 内层抛 RuntimeError。
        - 清理异常(rollback/close)通过必要的 `from` 异常链保留为 __cause__,
          不覆盖主异常; 不手工赋值 __context__; 不增公共属性。
        - self._tx 仅用于同实例嵌套保护, 不宣称线程/跨实例/跨进程安全。
        """
        if self._tx:
            raise RuntimeError("该 Database 实例内不支持嵌套 with_transaction")
        self._tx = True
        conn = self._connect()
        primary = None
        cause = None
        try:
            try:
                yield conn
            except BaseException as business:  # noqa: BLE001 - 主异常待传播
                primary = business
                try:
                    conn.rollback()
                except BaseException as rb:  # noqa: BLE001 - 清理异常入链
                    cause = rb
            else:
                try:
                    conn.commit()
                except BaseException as cm:  # noqa: BLE001 - 提交异常待传播
                    primary = cm
                    try:
                        conn.rollback()
                    except BaseException as rb:  # noqa: BLE001 - 清理异常入链
                        cause = rb
        finally:
            try:
                conn.close()
            except BaseException as cl:  # noqa: BLE001 - 关闭异常
                if primary is None:
                    primary = cl  # 正常提交后关闭失败: 传播关闭异常
                elif cause is None:
                    cause = cl  # 已有主异常: 关闭异常入链, 不覆盖
            self._tx = False
        if primary is None:
            return
        if cause is None:
            raise primary
        raise primary from cause

    # ================================================================
    # Initialization
    # ================================================================

    def _initialize_database(self) -> None:
        """
        Create database tables if they do not exist.
        """

        if not self.schema_path.exists():
            raise FileNotFoundError(
                f"schema.sql not found: "
                f"{self.schema_path}"
            )

        schema_sql = (
            self.schema_path.read_text(
                encoding="utf-8"
            )
        )

        with self._connect() as conn:
            conn.executescript(
                schema_sql
            )

    # ================================================================
    # Utility
    # ================================================================

    @staticmethod
    def _now() -> str:
        """
        Return current local timestamp.
        """

        return datetime.now().isoformat()

    @staticmethod
    def _now_iso() -> str:
        """
        Return current timestamp as ISO 8601 WITH timezone.

        V4.4 新增时间戳统一采用带时区 ISO 8601 (REQ-C4-01 三类时间戳分开)。
        不改动既有 _now()；仅用于新增 readings / sensor_alerts 的时间字段。
        """

        return datetime.now().astimezone().isoformat(
            timespec="seconds"
        )

    @staticmethod
    def _reading_id() -> str:
        """
        Generate a reading ID: R-<YYYYMMDD>-<uuid8>.
        """

        return (
            "R-"
            + datetime.now().strftime(
                "%Y%m%d"
            )
            + "-"
            + uuid.uuid4().hex[
                :8
            ].upper()
        )

    @staticmethod
    def _sensor_alert_id() -> str:
        """
        Generate a sensor alert ID: E-<YYYYMMDD>-<uuid8>.

        前缀与既有 _event_id() 相同 (E-...) 是用户确认的格式；
        因位于独立表，跨表统一查询时须以实体来源 (source) 消歧，
        不能仅凭 ID 前缀判断记录来源。
        """

        return (
            "E-"
            + datetime.now().strftime(
                "%Y%m%d"
            )
            + "-"
            + uuid.uuid4().hex[
                :8
            ].upper()
        )

    @staticmethod
    def _event_id() -> str:
        """
        Generate an event ID.
        """

        return (
            "E-"
            + datetime.now().strftime(
                "%Y%m%d"
            )
            + "-"
            + uuid.uuid4().hex[
                :8
            ].upper()
        )

    @staticmethod
    def _task_id() -> str:
        """
        Generate a task ID.
        """

        return (
            "T-"
            + datetime.now().strftime(
                "%Y%m%d"
            )
            + "-"
            + uuid.uuid4().hex[
                :8
            ].upper()
        )

    # ================================================================
    # Events
    # ================================================================

    # ================================================================
    # V4.4 C4-01 共享业务写入核心 (受控事务入口与公开 save_* 共用同一 INSERT)
    # 接受调用方提供的连接, 保证业务写入与 Outbox 幂等登记在同一连接/事务内。
    # 公开 save_event / save_reading / save_sensor_alert 委托到这些核心, 行为逐字保留。
    # ================================================================

    def _event_write_core(self, conn, *, type, severity, person_id, payload, source_id: str | None = None) -> str:
        event_id = source_id or self._event_id()
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        ts = self._now()
        conn.execute(
            """
            INSERT INTO events (event_id, ts, type, severity, person_id, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, ts, type, severity, person_id, payload_json),
        )
        return event_id

    def _reading_write_core(self, conn, *, sensor_id, temperature, humidity, device_captured_at, edge_received_at, source_id: str | None = None) -> str:
        reading_id = source_id or self._reading_id()
        now_iso = self._now_iso()
        captured = device_captured_at or now_iso
        edge_ts = edge_received_at or now_iso
        conn.execute(
            """
            INSERT INTO readings (
                reading_id, sensor_id, temperature, humidity,
                device_captured_at, edge_received_at, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                reading_id,
                sensor_id,
                float(temperature),
                (None if humidity is None else float(humidity)),
                captured,
                edge_ts,
            ),
        )
        return reading_id

    def _sensor_alert_write_core(self, conn, *, type, temperature, humidity, threshold, severity, device_captured_at, reading_id, source_id: str | None = None, occurred_at: str | None = None) -> str:
        alert_id = source_id or self._sensor_alert_id()
        now_iso = self._now_iso()
        # occurred_at 一致性: 调用方显式传入则原样使用(不得重新生成), 否则保持默认保存时刻。
        used_oc = occurred_at if occurred_at is not None else now_iso
        captured = device_captured_at or now_iso
        conn.execute(
            """
            INSERT INTO sensor_alerts (
                alert_id, type, severity, temperature, humidity, threshold,
                status, occurred_at, resolved_at, device_captured_at, received_at, reading_id
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, NULL, ?)
            """,
            (
                alert_id,
                type,
                severity,
                (None if temperature is None else float(temperature)),
                (None if humidity is None else float(humidity)),
                (None if threshold is None else float(threshold)),
                used_oc,
                captured,
                reading_id,
            ),
        )
        return alert_id

    def save_event(
        self,
        type: str,
        severity: str,
        person_id: int | None,
        payload: dict,
    ) -> str:
        """
        Save one event.

        Parameters
        ----------
        type:
            Event type.

        severity:
            Event severity.

        person_id:
            Related person ID.

        payload:
            Event payload dictionary.

        Returns
        -------
        str
            Generated event ID.
        """

        with self._connect() as conn:
            return self._event_write_core(
                conn,
                type=type,
                severity=severity,
                person_id=person_id,
                payload=payload,
            )

    # ================================================================
    # Recent events
    # ================================================================

    def get_recent_events(
        self,
        hours: int = 1,
    ) -> list[dict]:
        """
        Get recent events within the given number of hours.

        Parameters
        ----------
        hours:
            Number of hours to look back.

        Returns
        -------
        list[dict]
            Recent event records.
        """

        hours = max(
            1,
            int(hours),
        )

        # SQLite datetime expression.
        cutoff = (
            f"-{hours} hours"
        )

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    event_id,
                    ts,
                    type,
                    severity,
                    person_id,
                    payload
                FROM events
                WHERE ts >= datetime(
                    'now',
                    ?
                )
                ORDER BY ts DESC
                """,
                (cutoff,),
            ).fetchall()

        result = []

        for row in rows:
            item = dict(row)

            payload = item.get(
                "payload"
            )

            if isinstance(
                payload,
                str,
            ):
                try:
                    item["payload"] = (
                        json.loads(
                            payload
                        )
                    )
                except json.JSONDecodeError:
                    pass

            result.append(item)

        return result

    # ================================================================
    # Persons
    # ================================================================

    def get_last_alert_for_state(
        self,
        person_id: int | str,
        state: str,
        event_type: str = "memory_alert",
        limit: int = 20,
    ) -> dict | None:
        """查询某人员+state 最近一次 Memory alert 事件(供跨重启 cooldown 恢复)。

        仅查 events、仅限类型、按 person_id 过滤、Bounded(limit) 扫描后在 Python
        内比对 payload.state，避免全表扫描。返回不含 result 语义的事件字段。
        """
        rows = []
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT event_id, ts, type, person_id, payload "
                "FROM events WHERE type = ? AND person_id = ? "
                "ORDER BY ts DESC LIMIT ?",
                (event_type, str(person_id), int(limit)),
            )
            for r in cur.fetchall():
                rows.append({
                    "event_id": r[0], "ts": r[1], "type": r[2],
                    "person_id": r[3], "payload": r[4],
                })
        for row in rows:
            try:
                import json as _json
                payload = _json.loads(row["payload"] or "{}")
            except Exception:
                continue
            if payload.get("state") == str(state):
                row["payload"] = payload
                return row
        return None

    def upsert_person(
        self,
        person_id: int,
        state: str,
        risk: float,
    ) -> None:
        """
        Insert or update a person's latest state.
        """

        now = self._now()

        with self._connect() as conn:

            existing = conn.execute(
                """
                SELECT person_id
                FROM persons
                WHERE person_id = ?
                """,
                (
                    person_id,
                ),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE persons
                    SET
                        last_seen = ?,
                        latest_state = ?,
                        latest_risk = ?
                    WHERE person_id = ?
                    """,
                    (
                        now,
                        state,
                        float(risk),
                        person_id,
                    ),
                )

            else:
                conn.execute(
                    """
                    INSERT INTO persons (
                        person_id,
                        first_seen,
                        last_seen,
                        latest_state,
                        latest_risk
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        person_id,
                        now,
                        now,
                        state,
                        float(risk),
                    ),
                )

    # ================================================================
    # Get person
    # ================================================================

    def get_person(
        self,
        person_id: int,
    ) -> dict | None:
        """
        Get one person's latest record.
        """

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    person_id,
                    first_seen,
                    last_seen,
                    latest_state,
                    latest_risk
                FROM persons
                WHERE person_id = ?
                """,
                (
                    person_id,
                ),
            ).fetchone()

        if row is None:
            return None

        return dict(row)

    # ================================================================
    # Tasks
    # ================================================================

    def save_task(
        self,
        task: dict,
    ) -> str:
        """
        Save a new task or update an existing task.

        This method intentionally supports both:

            INSERT:
                when task_id does not exist

            UPDATE:
                when task_id already exists

        This is required by Executor because a task normally
        follows this lifecycle:

            running
                ↓
            completed / failed
        """

        if not isinstance(
            task,
            dict,
        ):
            raise ValueError(
                "task must be a dictionary"
            )

        task_id = task.get(
            "task_id"
        )

        if not task_id:
            task_id = self._task_id()

        trigger_event = task.get(
            "trigger_event"
        )

        plan_trace = task.get(
            "plan_trace",
            [],
        )

        status = task.get(
            "status",
            "running",
        )

        created = task.get(
            "created"
        )

        finished = task.get(
            "finished"
        )

        if not created:
            created = self._now()

        # ------------------------------------------------------------
        # Convert plan_trace to JSON
        # ------------------------------------------------------------

        if isinstance(
            plan_trace,
            str,
        ):
            plan_trace_json = (
                plan_trace
            )

        else:
            plan_trace_json = json.dumps(
                plan_trace,
                ensure_ascii=False,
                default=str,
            )

        # ------------------------------------------------------------
        # Check whether the task already exists
        # ------------------------------------------------------------

        with self._connect() as conn:

            existing = conn.execute(
                """
                SELECT
                    task_id,
                    created
                FROM tasks
                WHERE task_id = ?
                """,
                (
                    task_id,
                ),
            ).fetchone()

            # --------------------------------------------------------
            # Existing task -> UPDATE
            # --------------------------------------------------------

            if existing:

                original_created = (
                    existing["created"]
                    or created
                )

                conn.execute(
                    """
                    UPDATE tasks
                    SET
                        trigger_event = ?,
                        plan_trace = ?,
                        status = ?,
                        created = ?,
                        finished = ?
                    WHERE task_id = ?
                    """,
                    (
                        trigger_event,
                        plan_trace_json,
                        status,
                        original_created,
                        finished,
                        task_id,
                    ),
                )

            # --------------------------------------------------------
            # New task -> INSERT
            # --------------------------------------------------------

            else:

                conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id,
                        trigger_event,
                        plan_trace,
                        status,
                        created,
                        finished
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        trigger_event,
                        plan_trace_json,
                        status,
                        created,
                        finished,
                    ),
                )

        return task_id

    # ================================================================
    # Get task
    # ================================================================

    def get_task(
        self,
        task_id: str,
    ) -> dict | None:
        """
        Get one task by task ID.
        """

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    task_id,
                    trigger_event,
                    plan_trace,
                    status,
                    created,
                    finished
                FROM tasks
                WHERE task_id = ?
                """,
                (
                    task_id,
                ),
            ).fetchone()

        if row is None:
            return None

        result = dict(row)

        plan_trace = result.get(
            "plan_trace"
        )

        if isinstance(
            plan_trace,
            str,
        ):
            try:
                result["plan_trace"] = (
                    json.loads(
                        plan_trace
                    )
                )
            except json.JSONDecodeError:
                pass

        return result

    # ================================================================
    # Agent logs
    # ================================================================

    def save_agent_log(
        self,
        task_id: str,
        tool_name: str,
        input: dict,
        output: dict,
        latency_ms: int | None,
        timeout: float = 5.0,
    ) -> int:
        """
        Save one Agent tool execution log.

        Returns
        -------
        int
            Generated log ID.
        """

        ts = self._now()

        input_json = json.dumps(
            input,
            ensure_ascii=False,
            default=str,
        )

        output_json = json.dumps(
            output,
            ensure_ascii=False,
            default=str,
        )

        with self._connect(timeout=timeout) as conn:

            cursor = conn.execute(
                """
                INSERT INTO agent_logs (
                    task_id,
                    ts,
                    tool_name,
                    input,
                    output,
                    latency_ms
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    ts,
                    tool_name,
                    input_json,
                    output_json,
                    latency_ms,
                ),
            )

            return int(
                cursor.lastrowid
            )

    # ================================================================
    # Get Agent logs
    # ================================================================

    def get_agent_logs(
        self,
        task_id: str,
    ) -> list[dict]:
        """
        Get all Agent logs for one task.
        """

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    log_id,
                    task_id,
                    ts,
                    tool_name,
                    input,
                    output,
                    latency_ms
                FROM agent_logs
                WHERE task_id = ?
                ORDER BY log_id ASC
                """,
                (
                    task_id,
                ),
            ).fetchall()

        result = []

        for row in rows:
            item = dict(row)

            for field in (
                "input",
                "output",
            ):
                value = item.get(
                    field
                )

                if isinstance(
                    value,
                    str,
                ):
                    try:
                        item[field] = (
                            json.loads(
                                value
                            )
                        )
                    except json.JSONDecodeError:
                        pass

            result.append(item)

        return result


    # ================================================================
    # Environment readings (V4.4 E9-01)
    # ================================================================

    def save_reading(
        self,
        sensor_id: str,
        temperature: float,
        humidity: float | None = None,
        device_captured_at: str | None = None,
        edge_received_at: str | None = None,
    ) -> str:
        """
        Save one valid temperate/humidity reading (REQ-E7-08 / REQ-E9-01).

        调用方须已按 REQ-E7-02 校验读数有效性; 本方法不定义有效范围。
        - reading_id   = R-<YYYYMMDD>-<uuid8> (自动生成)
        - device_captured_at = 设备采样时间 (带时区 ISO, 缺省=当前时)
        - edge_received_at   = Pi 本地写库时间 (带时区 ISO, 缺省=当前时)
        - received_at  保持 NULL (服务端接收同步时填充, 本轮不实现同步)

        Returns
        -------
        str
            Generated reading_id.
        """

        with self._connect() as conn:
            return self._reading_write_core(
                conn,
                sensor_id=sensor_id,
                temperature=temperature,
                humidity=humidity,
                device_captured_at=device_captured_at,
                edge_received_at=edge_received_at,
            )

    def get_reading(
        self,
        reading_id: str,
    ) -> dict | None:
        """
        Fetch one reading (or None).

        供应用层校验 reading_id 关联是否有效 (REQ-E9-02 关联不设外键)。
        """

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    reading_id,
                    sensor_id,
                    temperature,
                    humidity,
                    device_captured_at,
                    edge_received_at,
                    received_at
                FROM readings
                WHERE reading_id = ?
                """,
                (reading_id,),
            ).fetchone()

        return (
            dict(row)
            if row is not None
            else None
        )

    def query_readings(
        self,
        sensor_id: str | None = None,
        start_device_captured_at: str | None = None,
        end_device_captured_at: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Query readings by sensor and device-capture time range.

        供 S4-06 区间/分页查询; 字符串排序依赖统一带时区 ISO, 由上层保证。
        """

        clauses: list[str] = []
        params: list[Any] = []

        if sensor_id is not None:
            clauses.append("sensor_id = ?")
            params.append(sensor_id)

        if start_device_captured_at is not None:
            clauses.append("device_captured_at >= ?")
            params.append(start_device_captured_at)

        if end_device_captured_at is not None:
            clauses.append("device_captured_at <= ?")
            params.append(end_device_captured_at)

        where = (
            "WHERE " + " AND ".join(clauses)
            if clauses
            else ""
        )

        params.extend([int(limit), int(offset)])

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    reading_id,
                    sensor_id,
                    temperature,
                    humidity,
                    device_captured_at,
                    edge_received_at,
                    received_at
                FROM readings
                {where}
                ORDER BY device_captured_at ASC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]


    # ================================================================
    # Sensor alerts (V4.4 E9-02)
    # ================================================================

    def save_sensor_alert(
        self,
        type: str,
        temperature: float | None = None,
        humidity: float | None = None,
        threshold: float | None = None,
        severity: str | None = None,
        device_captured_at: str | None = None,
        reading_id: str | None = None,
        occurred_at: str | None = None,
    ) -> str:
        """
        Save one sensor environment alert (REQ-E9-02 / REQ-E7-04/05).

        - alert_id = E-<YYYYMMDD>-<uuid8> (自动生成, 位于独立表)
        - status    初始为 'active' (REQ-E7-04)
        - occurred_at = 告警发生/确认时间 (带时区 ISO, 缺省=当前时)
        - received_at  保持 NULL (服务端同步填充)
        - reading_id  可空; 若提供, 调用方须先经 get_reading 校验存在。

        Returns
        -------
        str
            Generated alert_id.
        """

        with self._connect() as conn:
            return self._sensor_alert_write_core(
                conn,
                type=type,
                temperature=temperature,
                humidity=humidity,
                threshold=threshold,
                severity=severity,
                device_captured_at=device_captured_at,
                reading_id=reading_id,
                occurred_at=occurred_at,
            )

    def get_sensor_alert(
        self,
        alert_id: str,
    ) -> dict | None:
        """
        Fetch one sensor alert (or None).
        """

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    alert_id,
                    type,
                    severity,
                    temperature,
                    humidity,
                    threshold,
                    status,
                    occurred_at,
                    resolved_at,
                    device_captured_at,
                    received_at,
                    reading_id
                FROM sensor_alerts
                WHERE alert_id = ?
                """,
                (alert_id,),
            ).fetchone()

        return (
            dict(row)
            if row is not None
            else None
        )

    def query_sensor_alerts(
        self,
        status: str | None = None,
        type: str | None = None,
        start_occurred_at: str | None = None,
        end_occurred_at: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Query sensor alerts by status / type / occurred time.

        供 S4-02 列表 (分页); 仅传感器环境告警, 与 events 隔离。
        """

        clauses: list[str] = []
        params: list[Any] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        if type is not None:
            clauses.append("type = ?")
            params.append(type)

        if start_occurred_at is not None:
            clauses.append("occurred_at >= ?")
            params.append(start_occurred_at)

        if end_occurred_at is not None:
            clauses.append("occurred_at <= ?")
            params.append(end_occurred_at)

        where = (
            "WHERE " + " AND ".join(clauses)
            if clauses
            else ""
        )

        params.extend([int(limit), int(offset)])

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    alert_id,
                    type,
                    severity,
                    temperature,
                    humidity,
                    threshold,
                    status,
                    occurred_at,
                    resolved_at,
                    device_captured_at,
                    received_at,
                    reading_id
                FROM sensor_alerts
                {where}
                ORDER BY occurred_at DESC, alert_id DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    def update_sensor_alert_status(
        self,
        alert_id: str,
        status: str,
        resolved_at: str | None = None,
    ) -> bool:
        """
        Update a sensor alert status (REQ-E7-04: active -> resolved).

        resolved_at: 提供则一并写入 (缺省=当前带时区时间)。
        返回是否影响 (False 表示 alert 不存在或状态未变)。
        """

        new_status = str(status).lower().strip()

        if new_status not in (
            "active",
            "resolved",
        ):
            raise ValueError(
                f"非法告警状态: {status!r} "
                f"(仅允许 active/resolved)"
            )

        ts = (
            resolved_at
            or self._now_iso()
        )

        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE sensor_alerts
                SET status = ?,
                    resolved_at = ?
                WHERE alert_id = ?
                """,
                (
                    new_status,
                    ts,
                    alert_id,
                ),
            )

        return (
            cursor.rowcount > 0
        )

    # ================================================================
    # S4 read queries (V4.4 / S4 contract v0.1.0)
    # 只读: 供 server S4 查询接口使用。全部使用参数化 SQL。
    # COUNT 查询与对应列表查询使用完全一致的过滤条件。
    # ================================================================

    @staticmethod
    def _deserialize_payload(item: dict) -> dict:
        """把 events.payload JSON 字符串反序列化为 dict(失败留原值)。"""
        payload = item.get("payload")
        if isinstance(payload, str):
            try:
                item["payload"] = json.loads(payload)
            except json.JSONDecodeError:
                pass
        return item

    def query_person_alerts(
        self,
        type_: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        """
        Query person alert events filtered by event type (contract:
        person alerts 仅筛选 events.type == high_risk_alert)。

        SELECT 字段与 save_event/get_recent_events 一致;
        排序 events.ts DESC, event_id DESC (契约 S4-02 人员组)。
        """

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    event_id,
                    ts,
                    type,
                    severity,
                    person_id,
                    payload
                FROM events
                WHERE type = ?
                ORDER BY ts DESC, event_id DESC
                LIMIT ? OFFSET ?
                """,
                (
                    type_,
                    int(limit),
                    int(offset),
                ),
            ).fetchall()

        return [
            self._deserialize_payload(dict(row))
            for row in rows
        ]

    def get_event(
        self,
        event_id: str,
    ) -> dict | None:
        """
        Fetch a single event by exact event_id (or None)。
        供 S4-03 人员告警详情查询。
        """

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    event_id,
                    ts,
                    type,
                    severity,
                    person_id,
                    payload
                FROM events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()

        if row is None:
            return None

        return self._deserialize_payload(dict(row))

    def count_person_alerts(
        self,
        type_: str,
    ) -> int:
        """
        Count person alert events by event type.
        过滤条件与 query_person_alerts 一致(仅 type)。
        """

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM events
                WHERE type = ?
                """,
                (type_,),
            ).fetchone()

        return int(row["n"])

    def count_sensor_alerts(
        self,
        status: str | None = None,
        type: str | None = None,
        start_occurred_at: str | None = None,
        end_occurred_at: str | None = None,
    ) -> int:
        """
        Count sensor alerts with the same filters as query_sensor_alerts.
        (status / type / occurred 闭区间)
        """

        clauses: list[str] = []
        params: list[Any] = []

        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        if type is not None:
            clauses.append("type = ?")
            params.append(type)

        if start_occurred_at is not None:
            clauses.append("occurred_at >= ?")
            params.append(start_occurred_at)

        if end_occurred_at is not None:
            clauses.append("occurred_at <= ?")
            params.append(end_occurred_at)

        where = (
            "WHERE " + " AND ".join(clauses)
            if clauses
            else ""
        )

        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM sensor_alerts
                {where}
                """,
                params,
            ).fetchone()

        return int(row["n"])

    def count_readings(
        self,
        sensor_id: str | None = None,
        start_device_captured_at: str | None = None,
        end_device_captured_at: str | None = None,
    ) -> int:
        """
        Count readings with the same filters as query_readings
        (sensor_id / device_captured_at 闭区间)。
        """

        clauses: list[str] = []
        params: list[Any] = []

        if sensor_id is not None:
            clauses.append("sensor_id = ?")
            params.append(sensor_id)

        if start_device_captured_at is not None:
            clauses.append("device_captured_at >= ?")
            params.append(start_device_captured_at)

        if end_device_captured_at is not None:
            clauses.append("device_captured_at <= ?")
            params.append(end_device_captured_at)

        where = (
            "WHERE " + " AND ".join(clauses)
            if clauses
            else ""
        )

        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS n
                FROM readings
                {where}
                """,
                params,
            ).fetchone()

        return int(row["n"])


# ====================================================================
# V4.4 E9-03 媒体取证 DAO
# 表: media_evidence (schema.sql)。沿用现有 _connect/参数化风格, 不改既有方法。
# ====================================================================

    @staticmethod
    def _media_id() -> str:
        """生成媒体证据 ID (候选格式 M-<YYYYMMDD>-<uuid8>)。"""

        return (
            "M-"
            + datetime.now().strftime("%Y%m%d")
            + "-"
            + uuid.uuid4().hex[:8].upper()
        )


    @staticmethod
    def _require_tz_iso(value: str) -> str:
        """校验字符串为带时区 ISO 8601 时间(真实日期解析); 返回原字符串。"""

        text = str(value)
        strip_z = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(strip_z)
        except ValueError:
            raise ValueError("时间必须是有效的带时区 ISO 8601 格式")
        if parsed.tzinfo is None:
            raise ValueError("时间必须带时区")
        return text


    def save_media_evidence(
        self,
        *,
        source: str,
        ref_id: str,
        media_type: str,
        local_path: str,
        captured_at: str,
        status: str = "pending",
        created_at: str | None = None,
        media_id: str | None = None,
        capture_result: str | None = None,
        upload_status: str | None = None,
        object_key: str | None = None,
        uploaded_at: str | None = None,
        retry_count: int = 0,
        last_error: str | None = None,
    ) -> str:
        """
        保存一条媒体取证记录, 返回 media_id。

        - 校验枚举/时间/路径/告警存在。
        - 幂等: 相同 media_id 且 source/ref_id/media_type/local_path/captured_at 一致则返回既有 id;
          关键字段冲突则抛 ValueError, 不覆盖。
        - source='sensor' 时校验 sensor_alerts.alert_id 存在。
        """

        if source not in _MEDIA_SOURCES:
            raise ValueError(f"非法 source: {source!r} (仅允许 sensor)")
        if media_type not in _MEDIA_TYPES:
            raise ValueError(f"非法 media_type: {media_type!r} (仅允许 image/video)")
        if status not in _MEDIA_STATUS:
            raise ValueError(f"非法 status: {status!r}")
        if capture_result is not None and capture_result not in _MEDIA_CAPTURE_RESULTS:
            raise ValueError(f"非法 capture_result: {capture_result!r}")
        if upload_status is not None and upload_status not in _MEDIA_UPLOAD_STATUS:
            raise ValueError(f"非法 upload_status: {upload_status!r}")
        if not isinstance(retry_count, int) or retry_count < 0:
            raise ValueError("retry_count 必须为非负整数")
        if not isinstance(local_path, str) or not local_path:
            raise ValueError("local_path 必须为非空字符串")

        captured_at = self._require_tz_iso(captured_at)
        created_at = self._require_tz_iso(created_at) if created_at else self._now_iso()
        if uploaded_at is not None:
            uploaded_at = self._require_tz_iso(uploaded_at)

        # 本期仅允许关联传感器告警: 校验告警存在
        if source == "sensor" and self.get_sensor_alert(ref_id) is None:
            raise ValueError(f"关联告警不存在: {ref_id!r}")

        media_id = media_id or self._media_id()

        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO media_evidence (
                        media_id, source, ref_id, media_type, local_path, status,
                        capture_result, upload_status, object_key,
                        captured_at, created_at, uploaded_at, retry_count, last_error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        media_id, source, ref_id, media_type, local_path, status,
                        capture_result, upload_status, object_key,
                        captured_at, created_at, uploaded_at, retry_count, last_error,
                    ),
                )
        except sqlite3.IntegrityError:
            # media_id 已存在: 幂等或冲突
            existing = self.get_media_evidence(media_id)
            if existing is None:  # pragma: no cover - 不应发生
                raise
            if (
                existing.get("source") == source
                and existing.get("ref_id") == ref_id
                and existing.get("media_type") == media_type
                and existing.get("local_path") == local_path
                and existing.get("captured_at") == captured_at
            ):
                return media_id
            raise ValueError(f"媒体 ID 冲突，关键字段不一致: {media_id!r}")

        return media_id


    def get_media_evidence(self, media_id: str) -> dict | None:
        """按 media_id 获取媒体取证记录 (或 None)。"""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM media_evidence
                WHERE media_id = ?
                """,
                (media_id,),
            ).fetchone()

        return dict(row) if row is not None else None


    def list_media_evidence_by_ref(self, source: str, ref_id: str) -> list[dict]:
        """按归属告警列出媒体取证记录 (稳定顺序: created_at ASC)。"""

        if source not in _MEDIA_SOURCES:
            raise ValueError(f"非法 source: {source!r} (仅允许 sensor)")

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM media_evidence
                WHERE source = ? AND ref_id = ?
                ORDER BY created_at ASC, media_id ASC
                """,
                (source, ref_id),
            ).fetchall()

        return [dict(r) for r in rows]


    def update_media_evidence_status(
        self,
        media_id: str,
        *,
        status: str,
        capture_result: object = _UM,
        upload_status: object = _UM,
        object_key: object = _UM,
        uploaded_at: object = _UM,
        last_error: object = _UM,
        retry_count: int | None = None,
    ) -> bool:
        """
        更新媒体取证主状态(及可选辅助字段)。

        - 仅允许契约状态转换; 非法转换/成功回退返回 False 且不改记录。
        - 单条原子 UPDATE; 辅助字段已提供才更新。只负责数据库状态, 不校验 OSS 真实性。
        """

        if not isinstance(status, str) or status not in _MEDIA_STATUS:
            raise ValueError(f"非法 status: {status!r}")

        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM media_evidence WHERE media_id = ?",
                (media_id,),
            ).fetchone()
            if row is None:
                return False
            current = row["status"]
            if status not in _MEDIA_TRANSITIONS.get(current, frozenset()):
                return False

            sets: list[str] = ["status = ?"]
            params: list[Any] = [status]

            def _add(col_name: str, provided: object, value_transform=None) -> None:
                if provided is _UM:
                    return
                val = value_transform(provided) if value_transform else provided
                sets.append(f"{col_name} = ?")
                params.append(val)

            if capture_result is not _UM:
                if capture_result is not None and capture_result not in _MEDIA_CAPTURE_RESULTS:
                    raise ValueError(f"非法 capture_result: {capture_result!r}")
                _add("capture_result", capture_result)
            if upload_status is not _UM:
                if upload_status is not None and upload_status not in _MEDIA_UPLOAD_STATUS:
                    raise ValueError(f"非法 upload_status: {upload_status!r}")
                _add("upload_status", upload_status)
            if object_key is not _UM:
                _add("object_key", object_key)
            if uploaded_at is not _UM:
                _add("uploaded_at", uploaded_at,
                     value_transform=(lambda v: self._require_tz_iso(v) if v is not None else None))
            if last_error is not _UM:
                _add("last_error", last_error)
            if retry_count is not None:
                if not isinstance(retry_count, int) or retry_count < 0:
                    raise ValueError("retry_count 必须为非负整数")
                _add("retry_count", retry_count)

            params.append(media_id)
            cur = conn.execute(
                f"UPDATE media_evidence SET {', '.join(sets)} WHERE media_id = ?",
                params,
            )
            return cur.rowcount > 0


    # ================================================================
    # V4.4 P1-B 投递登记 (deliveries)
    # 幂等登记: 首次创建 pending; 重复提交同复合键返回既有记录, 不覆盖 status/created_at。
    # task_kind 为开放 TEXT (不设枚举), 合法取值待真实投递消费方明确后再定义。
    # ================================================================

    def save_delivery(
        self,
        *,
        source: str,
        alert_id: str,
        task_kind: str,
        created_at: str | None = None,
    ) -> tuple[str, str, str]:
        """投递登记: 幂等创建一条 pending 记录, 返回复合键 (source, alert_id, task_kind)。

        - source 仅允许 person/sensor; alert_id / task_kind 须为非空字符串。
        - 仅创建初始 pending; 状态推进由 update_delivery_status 负责。
        - 重复提交同一复合键: 返回既有键, 不新增, 不覆盖 status/created_at。
        - 幂等判定: 捕获 IntegrityError 后按完整复合键回查; 回查命中才按幂等冲突,
          回查未命中必须重抛原异常 (不得吞掉 CHECK/NOT NULL 等其他约束错误)。
        """

        if source not in _DELIVERY_SOURCES:
            raise ValueError(f"非法 source: {source!r} (仅允许 person/sensor)")
        if not isinstance(alert_id, str) or not alert_id:
            raise ValueError("alert_id 必须为非空字符串")
        if not isinstance(task_kind, str) or not task_kind:
            raise ValueError("task_kind 必须为非空字符串")
        created_at = self._require_tz_iso(created_at) if created_at else self._now_iso()

        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO deliveries (source, alert_id, task_kind, status, created_at)
                    VALUES (?, ?, ?, 'pending', ?)
                    """,
                    (source, alert_id, task_kind, created_at),
                )
        except sqlite3.IntegrityError:
            # 按完整复合键回查: 命中 => 幂等冲突(返回既有记录); 未命中 => 非幂等约束错误, 重抛
            existing = self.get_delivery(source, alert_id, task_kind)
            if existing is None:  # pragma: no cover - 防御: 非幂等 IntegrityError 不得误判
                raise
            return (existing["source"], existing["alert_id"], existing["task_kind"])

        return (source, alert_id, task_kind)


    def get_delivery(
        self,
        source: str,
        alert_id: str,
        task_kind: str,
    ) -> dict | None:
        """按复合键获取一条投递记录 (或 None)。"""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT source, alert_id, task_kind, status, created_at
                FROM deliveries
                WHERE source = ? AND alert_id = ? AND task_kind = ?
                """,
                (source, alert_id, task_kind),
            ).fetchone()
        return dict(row) if row is not None else None


    def list_deliveries(self, *, status: str | None = None) -> list[dict]:
        """列出投递记录; 可选按 status 过滤 (稳定顺序: source, alert_id, task_kind)。"""

        if status is not None and status not in _DELIVERY_STATUS:
            raise ValueError(f"非法 status: {status!r} (仅允许 pending/success)")

        where = ""
        params: list[Any] = []
        if status is not None:
            where = "WHERE status = ?"
            params.append(status)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT source, alert_id, task_kind, status, created_at
                FROM deliveries
                {where}
                ORDER BY source, alert_id, task_kind
                """,
                params,
            ).fetchall()
        return [dict(r) for r in rows]


    def update_delivery_status(
        self,
        source: str,
        alert_id: str,
        task_kind: str,
        *,
        status: str,
    ) -> bool:
        """将投递记录推进到新状态 (仅 pending -> success)。

        - 非法 status 抛 ValueError; 记录不存在返回 False; 非法转换返回 False 且不改行。
        - 单条条件 UPDATE 仅改 status 列, 不覆盖其他字段, 避免并发覆盖。
        - success 仅表示本期记录被标记成功, 不代表远端已收到/处理。
        """

        if not isinstance(status, str) or status not in _DELIVERY_STATUS:
            raise ValueError(f"非法 status: {status!r} (仅允许 pending/success)")

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status FROM deliveries
                WHERE source = ? AND alert_id = ? AND task_kind = ?
                """,
                (source, alert_id, task_kind),
            ).fetchone()
            if row is None:
                return False
            current = row["status"]
            if status not in _DELIVERY_TRANSITIONS.get(current, frozenset()):
                return False
            cur = conn.execute(
                """
                UPDATE deliveries SET status = ?
                WHERE source = ? AND alert_id = ? AND task_kind = ?
                """,
                (status, source, alert_id, task_kind),
            )
            return cur.rowcount > 0


    # ================================================================
    # V4.4 C4-01: 单向同步 Outbox + 服务端幂等登记 + 冲突审计
    # - 首次业务写入、首次幂等登记、首次 Outbox 入队 在同一事务内完成。
    # - 同键同内容 -> already_synced; 同键异内容 -> c4_content_conflict 审计 + content_mismatch。
    # - 唯一约束冲突回滚后重新核验已存在内容, 不得一律视为成功。
    # - fencing: 领取时 gen+1; 回写均校验 id + 预期状态 + 领取时 gen; rowcount==0 即停止。
    # - 时间字段统一 UTC epoch 毫秒整数 (不改变既有业务表时间格式)。
    # ================================================================

    @staticmethod
    def _utc_now_ms() -> int:
        return int(datetime.now(timezone.utc).timestamp() * 1000)

    def _write_business_in_tx(self, conn, record_type: str, source_record_id: str, business: dict) -> None:
        """在同一连接/事务内以给定稳定 ID 写业务记录 (与公开 save_* 共用 core, 语义一致)。"""
        b = business if isinstance(business, dict) else {}
        if record_type == "person_event":
            self._event_write_core(
                conn, source_id=source_record_id,
                type=b.get("type", "c4"),
                severity=b.get("severity", "unknown"),
                person_id=b.get("person_id"),
                payload=b.get("payload", {}),
            )
        elif record_type == "reading":
            self._reading_write_core(
                conn, source_id=source_record_id,
                sensor_id=b.get("sensor_id", ""),
                temperature=b.get("temperature", 0.0),
                humidity=b.get("humidity"),
                device_captured_at=b.get("device_captured_at"),
                edge_received_at=b.get("edge_received_at"),
            )
        elif record_type == "sensor_alert":
            self._sensor_alert_write_core(
                conn, source_id=source_record_id,
                type=b.get("type", "sensor_hightemp"),
                temperature=b.get("temperature"),
                humidity=b.get("humidity"),
                threshold=b.get("threshold"),
                severity=b.get("severity"),
                device_captured_at=b.get("device_captured_at"),
                reading_id=b.get("reading_id"),
            )
        else:
            raise ValueError(f"未知 C4 record_type: {record_type!r}")

    def _verify_existing_after_conflict(
        self, tx, record_type: str, source_record_id: str, content_hash: str, edge_id: str | None,
    ) -> str:
        """唯一冲突后, 在给定连接上核验已存在登记内容 (不让所有冲突都视为 already_synced)。"""
        row = tx.execute(
            "SELECT content_hash FROM c4_idem WHERE record_type=? AND source_record_id=?",
            (record_type, source_record_id),
        ).fetchone()
        if row is None:
            return "unhandled"
        now = self._utc_now_ms()
        if row["content_hash"] == content_hash:
            return "already_synced"
        tx.execute(
            """
            INSERT INTO c4_content_conflict
                (record_type, source_record_id, content_hash, conflict_hash, detected_at, edge_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (record_type, source_record_id, row["content_hash"], content_hash, now, edge_id),
        )
        return "content_mismatch"

    def _enqueue_sync_in_tx(
        self,
        tx,
        record_type: str,
        source_record_id: str,
        content_hash: str,
        *,
        business: dict | None = None,
        edge_id: str | None = None,
        created_at_ms: int | None = None,
    ) -> tuple[str, str | None]:
        """在调用方提供的连接/事务内执行 enqueue_sync 的核心逻辑。

        - 不自行创建连接、不提交、不关闭；提交/回滚由调用方事务负责。
        - 幂等/冲突/Outbox 逻辑与公开 enqueue_sync 完全一致。
        """
        if record_type not in _C4_RECORD_TYPES:
            raise ValueError(f"非法 record_type: {record_type!r} (仅允许 {sorted(_C4_RECORD_TYPES)})")
        if not isinstance(source_record_id, str) or not source_record_id:
            raise ValueError("source_record_id 必须为非空字符串")
        if not isinstance(content_hash, str) or not content_hash:
            raise ValueError("content_hash 必须为非空字符串")
        now = created_at_ms if created_at_ms is not None else self._utc_now_ms()

        existing = tx.execute(
            "SELECT content_hash FROM c4_idem WHERE record_type=? AND source_record_id=?",
            (record_type, source_record_id),
        ).fetchone()
        if existing is None:
            # 首收: 业务写(如需) + 幂等登记 + Outbox 入队 (同事务原子)
            if business is not None:
                self._write_business_in_tx(tx, record_type, source_record_id, business)
            tx.execute(
                """
                INSERT INTO c4_idem (record_type, source_record_id, content_hash, received_at, edge_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record_type, source_record_id, content_hash, now, edge_id),
            )
            tx.execute(
                """
                INSERT INTO sync_outbox (record_type, source_record_id, status, created_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (record_type, source_record_id, now),
            )
            return ("queued", source_record_id)
        if existing["content_hash"] == content_hash:
            return ("already_synced", source_record_id)
        tx.execute(
            """
            INSERT INTO c4_content_conflict
                (record_type, source_record_id, content_hash, conflict_hash, detected_at, edge_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (record_type, source_record_id, existing["content_hash"], content_hash, now, edge_id),
        )
        return ("content_mismatch", source_record_id)

    def enqueue_sync(
        self,
        record_type: str,
        source_record_id: str,
        content_hash: str,
        *,
        business: dict | None = None,
        edge_id: str | None = None,
        created_at_ms: int | None = None,
    ) -> tuple[str, str | None]:
        """受控入口: 首次业务写 + 幂等登记 + Outbox 入队 (同事务), 或返回冲突/重复结果。

        返回 (result, source_record_id): result ∈
          "queued"           首收, 已登记并入队
          "already_synced"   同键同内容, 不写业务/入队
          "content_mismatch" 同键异内容, 追加冲突审计, 不覆盖首次
        """
        try:
            with self.with_transaction() as tx:
                return self._enqueue_sync_in_tx(
                    tx, record_type, source_record_id, content_hash,
                    business=business, edge_id=edge_id, created_at_ms=created_at_ms,
                )
        except sqlite3.IntegrityError:
            # 并发唯一冲突/约束冲突: 整事务回滚, 在新事务重核已存在内容
            with self.with_transaction() as tx:
                result = self._verify_existing_after_conflict(tx, record_type, source_record_id, content_hash, edge_id)
            if result == "unhandled":
                raise
            return (result, source_record_id)

    # ---- C4-01 待核验承载 (c4_verify): 规范化失败/存在原因码 -> 记入, 不进 enqueue_sync ----

    def _add_verify_in_tx(
        self,
        tx,
        record_type: str,
        source_record_id: str,
        reasons: set[str],
        *,
        business_json: dict | None = None,
        edge_id: str | None = None,
        now_ms: int | None = None,
    ) -> bool:
        """在调用方提供的连接/事务内执行 add_verify 核心 (新增/合并, 不丢并发原因)。

        - 不自行创建连接、不提交、不关闭; 提交/回滚由调用方事务负责。
        """
        from src.database.canonical import REASONS, canonical_reasons_json, parse_reasons_json

        new = {str(r) for r in reasons}
        if not new.issubset(REASONS):
            bad = sorted(new - REASONS)
            raise ValueError(f"非法原因码: {bad}")
        now = now_ms if now_ms is not None else self._utc_now_ms()
        biz = json.dumps(business_json, ensure_ascii=False, default=str) if isinstance(business_json, dict) else None

        row = tx.execute(
            "SELECT reasons FROM c4_verify WHERE record_type=? AND source_record_id=?",
            (record_type, source_record_id),
        ).fetchone()
        if row is not None:
            merged = new | parse_reasons_json(row["reasons"])
            tx.execute(
                """
                UPDATE c4_verify
                SET reasons=?, business_json=?, edge_id=?, updated_at=?
                WHERE record_type=? AND source_record_id=?
                """,
                (canonical_reasons_json(merged), biz, edge_id, now, record_type, source_record_id),
            )
            return True
        try:
            tx.execute(
                """
                INSERT INTO c4_verify
                    (record_type, source_record_id, reasons, business_json, edge_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (record_type, source_record_id, canonical_reasons_json(new), biz, edge_id, now, now),
            )
            return True
        except sqlite3.IntegrityError:
            # 并发 INSERT 冲突 -> 重取并合并
            row2 = tx.execute(
                "SELECT reasons FROM c4_verify WHERE record_type=? AND source_record_id=?",
                (record_type, source_record_id),
            ).fetchone()
            merged = new | parse_reasons_json(row2["reasons"])
            tx.execute(
                """
                UPDATE c4_verify
                SET reasons=?, business_json=?, edge_id=?, updated_at=?
                WHERE record_type=? AND source_record_id=?
                """,
                (canonical_reasons_json(merged), biz, edge_id, now, record_type, source_record_id),
            )
            return True

    def add_verify(
        self,
        record_type: str,
        source_record_id: str,
        reasons: set[str],
        *,
        business_json: dict | None = None,
        edge_id: str | None = None,
        now_ms: int | None = None,
    ) -> bool:
        """新增/合并一条待核验记录 (同键并发合并原因码, 不丢失)。"""
        with self.with_transaction() as conn:
            return self._add_verify_in_tx(
                conn, record_type, source_record_id, reasons,
                business_json=business_json, edge_id=edge_id, now_ms=now_ms,
            )

    def get_verify(self, record_type: str, source_record_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, record_type, source_record_id, reasons, business_json, edge_id, created_at, updated_at
                FROM c4_verify WHERE record_type=? AND source_record_id=?
                """,
                (record_type, source_record_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_verify(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM c4_verify ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- Outbox 状态操作 (id + 预期状态 + 领取时 gen 条件更新; rowcount==0 即停) ----

    def outbox_claim(self, worker_id: str, now_ms: int | None = None) -> int | None:
        """原子领取一条可处理任务; gen+1; 返回 outbox id (无可领取返 None)。"""
        now = now_ms if now_ms is not None else self._utc_now_ms()
        with self.with_transaction() as tx:
            row = tx.execute(
                """
                SELECT id FROM sync_outbox
                WHERE (status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=:now))
                   OR (status='retry_wait' AND next_attempt_at IS NOT NULL AND next_attempt_at<=:now)
                   OR (status='processing' AND lease_until IS NOT NULL AND lease_until<:now)
                ORDER BY id LIMIT 1
                """,
                {"now": now},
            ).fetchone()
            if row is None:
                return None
            oid = row["id"]
            cur = tx.execute(
                """
                UPDATE sync_outbox
                SET status='processing', gen=gen+1, worker_id=:me, lease_until=:until
                WHERE id=:id
                  AND ((status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=:now))
                    OR (status='retry_wait' AND next_attempt_at IS NOT NULL AND next_attempt_at<=:now)
                    OR (status='processing' AND lease_until IS NOT NULL AND lease_until<:now))
                """,
                {"id": oid, "me": worker_id, "until": now + self._outbox_lease_ms, "now": now},
            )
            if cur.rowcount != 1:
                return None  # 已被其余 worker 领取
            return oid

    _outbox_lease_ms = 30_000  # 默认租约 30s (候选, 可配置)

    def outbox_renew(self, outbox_id: int, worker_id: str, gen: int, now_ms: int | None = None) -> bool:
        """续租: gen 不变; 仅租约未过期且 gen 匹配才可续租。"""
        now = now_ms if now_ms is not None else self._utc_now_ms()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE sync_outbox SET lease_until=:until
                WHERE id=:id AND status='processing' AND worker_id=:me AND gen=:gen AND lease_until>=:now
                """,
                {"until": now + self._outbox_lease_ms, "id": outbox_id, "me": worker_id, "gen": gen, "now": now},
            )
            return cur.rowcount > 0

    def outbox_mark_success(self, outbox_id: int, worker_id: str, gen: int, now_ms: int | None = None) -> bool:
        """成功回写 (终态): 校验 id+processing+gen。"""
        now = now_ms if now_ms is not None else self._utc_now_ms()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE sync_outbox SET status='success', completed_at=:now
                WHERE id=:id AND status='processing' AND worker_id=:me AND gen=:gen
                """,
                {"id": outbox_id, "me": worker_id, "gen": gen, "now": now},
            )
            return cur.rowcount > 0

    def outbox_mark_retry_wait(
        self, outbox_id: int, worker_id: str, gen: int,
        *, next_attempt_at: int, last_error: str, now_ms: int | None = None,
    ) -> bool:
        """可重试失败回写 -> retry_wait, retry_count+1 (校验 id+processing+gen)。"""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE sync_outbox
                SET status='retry_wait', retry_count=retry_count+1, next_attempt_at=:na, last_error=:err
                WHERE id=:id AND status='processing' AND worker_id=:me AND gen=:gen
                """,
                {"id": outbox_id, "me": worker_id, "gen": gen, "na": next_attempt_at, "err": last_error},
            )
            return cur.rowcount > 0

    def outbox_mark_dead_letter(
        self, outbox_id: int, worker_id: str, gen: int, *, last_error: str, now_ms: int | None = None,
    ) -> bool:
        """重试耗尽/永久失败 -> dead_letter (校验 id+processing+gen)。"""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE sync_outbox SET status='dead_letter', last_error=:err
                WHERE id=:id AND status='processing' AND worker_id=:me AND gen=:gen
                """,
                {"id": outbox_id, "me": worker_id, "gen": gen, "err": last_error},
            )
            return cur.rowcount > 0

    def outbox_requeue(self, outbox_id: int, *, reason: str = "manual", now_ms: int | None = None):
        """人工重入: dead_letter -> pending; 保留原失败历史(追加 requeued 标记), 重置重试计数。"""
        now = now_ms if now_ms is not None else self._utc_now_ms()
        with self.with_transaction() as tx:
            row = tx.execute(
                "SELECT last_error FROM sync_outbox WHERE id=? AND status='dead_letter'",
                (outbox_id,),
            ).fetchone()
            if row is None:
                return False
            hist = (row["last_error"] or "") + f"|requeued@{now}:{reason}"
            cur = tx.execute(
                """
                UPDATE sync_outbox
                SET status='pending', gen=gen+1, retry_count=0, next_attempt_at=NULL,
                    worker_id=NULL, lease_until=NULL, last_error=:hist
                WHERE id=:id AND status='dead_letter'
                """,
                {"hist": hist, "id": outbox_id},
            )
            return cur.rowcount > 0

    def get_outbox(self, outbox_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sync_outbox WHERE id=?",
                (outbox_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_outbox(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status is not None and status not in _OUTBOX_STATUS:
            raise ValueError(f"非法 outbox status: {status!r}")
        params: list[Any] = []
        where = "WHERE 1=1"
        if status is not None:
            where += " AND status=?"
            params.append(status)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM sync_outbox {where} ORDER BY id LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]



    # ------------------------------------------------------------------
    # V4.4 微信用户认证 DAO (login / me / logout)
    # - users: openid 唯一; 并发重复 openid → INSERT OR IGNORE 保证一 openid 一用户
    # - sessions: 只存 session_token 的 SHA-256 哈希; revoked_at 非空=已撤销
    # ------------------------------------------------------------------
    @staticmethod
    def _user_id() -> str:
        """内部用户 ID: U-<YYYYMMDD>-<uuid8>（沿用既有业务 ID 风格）。"""
        return ("U-" + datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8].upper())

    @staticmethod
    def _session_id() -> str:
        """会话 ID: S-<YYYYMMDD>-<uuid8>。"""
        return ("S-" + datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8].upper())

    def get_user_by_openid(self, wechat_openid: str) -> dict | None:
        """按微信 OpenID 查询内部用户（不存在返回 None）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, wechat_openid, created_at FROM users WHERE wechat_openid = ?",
                (wechat_openid,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_user_by_id(self, user_id: str) -> dict | None:
        """按内部 user_id 查询用户（不存在返回 None）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, wechat_openid, created_at FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def create_user(self, wechat_openid: str, *, user_id: str | None = None) -> str:
        """按 OpenID 创建内部用户，返回 user_id。

        幂等/并发安全：对同一 OpenID 用 INSERT OR IGNORE + 唯一约束，两个并发登录
        也只能落一个内部用户；已存在时返回既有 user_id。
        """
        uid = user_id if user_id else self._user_id()
        now = self._now_iso()
        with self.with_transaction() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO users (user_id, wechat_openid, created_at) VALUES (?, ?, ?)",
                (uid, wechat_openid, now),
            )
            row = tx.execute(
                "SELECT user_id FROM users WHERE wechat_openid = ?",
                (wechat_openid,),
            ).fetchone()
        return row["user_id"]

    def create_session(self, user_id: str, session_token_hash: str, *,
                       expires_at: str, session_id: str | None = None) -> str:
        """创建会话并返回 session_id。

        expires_at 由调用方计算（登录时 = 创建时刻 + 86400s，带时区 ISO 8601）；
        只存 session_token 的 SHA-256 哈希，不存明文 Token。
        """
        sid = session_id if session_id else self._session_id()
        now = self._now_iso()
        with self.with_transaction() as tx:
            tx.execute(
                "INSERT INTO sessions "
                "(session_id, user_id, session_token_hash, created_at, expires_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (sid, user_id, session_token_hash, now, expires_at),
            )
        return sid

    def get_session_by_hash(self, session_token_hash: str) -> dict | None:
        """按 Token 哈希查询会话（含用户信息 join；不存在返回 None）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT s.session_id, s.user_id, s.created_at, s.expires_at, s.revoked_at "
                "FROM sessions s WHERE s.session_token_hash = ?",
                (session_token_hash,),
            ).fetchone()
        return dict(row) if row is not None else None

    def revoke_session(self, session_id: str) -> bool:
        """撤销指定会话（幂等：已撤销/不存在也安全返回）。返回是否实际更新。"""
        now = self._now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
                (now, session_id),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------
    # V4.4 E7-07: 传感器设备状态与故障历史 DAO（A3）
    # - sensor_status: 每传感器当前状态；重启后据此恢复。
    # - sensor_fault_events: 故障/恢复历史。
    # - 参数化 SQL；更新用 ON CONFLICT upsert；时间统一带时区 ISO。
    # ------------------------------------------------------------------
    def get_sensor_status(self, sensor_id: str) -> dict | None:
        """查询传感器当前状态（不存在返回 None）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sensor_status WHERE sensor_id = ?",
                (sensor_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def save_sensor_status(
        self,
        *,
        sensor_id: str,
        status: str,
        fault_type: str | None = None,
        last_error: str | None = None,
        first_fault_at: str | None = None,
        last_fault_at: str | None = None,
        recovered_at: str | None = None,
        consecutive_failures: int = 0,
        quality_failures: int = 0,
        updated_at: str | None = None,
    ) -> None:
        """写入/更新传感器当前状态（幂等 upsert，按 sensor_id 主键）。"""
        if status not in ("online", "degraded", "offline"):
            raise ValueError(f"非法传感器状态: {status!r}")
        now = updated_at or self._now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sensor_status
                  (sensor_id, status, fault_type, last_error,
                   first_fault_at, last_fault_at, recovered_at,
                   consecutive_failures, quality_failures, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sensor_id) DO UPDATE SET
                  status=excluded.status,
                  fault_type=excluded.fault_type,
                  last_error=excluded.last_error,
                  first_fault_at=excluded.first_fault_at,
                  last_fault_at=excluded.last_fault_at,
                  recovered_at=excluded.recovered_at,
                  consecutive_failures=excluded.consecutive_failures,
                  quality_failures=excluded.quality_failures,
                  updated_at=excluded.updated_at
                """,
                (sensor_id, status, fault_type, last_error,
                 first_fault_at, last_fault_at, recovered_at,
                 consecutive_failures, quality_failures, now),
            )

    def record_sensor_fault_event(
        self,
        *,
        sensor_id: str,
        status: str,
        fault_type: str | None = None,
        detail: str | None = None,
        occurred_at: str | None = None,
        recovered_at: str | None = None,
    ) -> int:
        """追加一条传感器故障/恢复历史，返回事件 id。"""
        if status not in ("online", "degraded", "offline"):
            raise ValueError(f"非法传感器状态: {status!r}")
        ts = occurred_at or self._now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO sensor_fault_events
                  (sensor_id, status, fault_type, detail, occurred_at, recovered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sensor_id, status, fault_type, detail, ts, recovered_at),
            )
            return int(cur.lastrowid)

    def apply_sensor_online(
        self,
        *,
        sensor_id: str,
        status: str = "online",
        fault_type: str | None = None,
        last_error: str | None = None,
        first_fault_at: str | None = None,
        last_fault_at: str | None = None,
        recovered_at: str | None = None,
        consecutive_failures: int = 0,
        quality_failures: int = 0,
        updated_at: str | None = None,
        backfill_event_id: int | None = None,
        rec_occurred_at: str | None = None,
    ) -> None:
        """原子地把 sensor 置为 online：同一事务内完成
        ① sensor_status 状态更新 ② 未恢复故障事件 recovered_at 回填 ③ online/recovered 历史事件写入。
        任一失败整体回滚（不出现"状态已 online 但恢复事件丢失/半写"）。
        仅用于"从 degraded/offline 真实恢复"的转移路径；正常采样用 save_sensor_status 维护状态即可。
        backfill_event_id 为 None 时，在事务内自动选取该 sensor 最新一条未恢复的
        offline/degraded 故障事件进行回填（保证选场与其余两写同一事务）。
        """
        if status != "online":
            raise ValueError(f"apply_sensor_online 仅用于恢复 online, got {status!r}")
        now = updated_at or self._now_iso()
        ts = rec_occurred_at or self._now_iso()
        with self.with_transaction() as tx:
            tx.execute(
                """
                INSERT INTO sensor_status
                  (sensor_id, status, fault_type, last_error,
                   first_fault_at, last_fault_at, recovered_at,
                   consecutive_failures, quality_failures, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sensor_id) DO UPDATE SET
                  status=excluded.status,
                  fault_type=excluded.fault_type,
                  last_error=excluded.last_error,
                  first_fault_at=excluded.first_fault_at,
                  last_fault_at=excluded.last_fault_at,
                  recovered_at=excluded.recovered_at,
                  consecutive_failures=excluded.consecutive_failures,
                  quality_failures=excluded.quality_failures,
                  updated_at=excluded.updated_at
                """,
                (sensor_id, status, fault_type, last_error,
                 first_fault_at, last_fault_at, recovered_at,
                 consecutive_failures, quality_failures, now),
            )
            if backfill_event_id is None:
                row = tx.execute(
                    "SELECT id FROM sensor_fault_events "
                    "WHERE sensor_id=? AND status IN ('offline','degraded') "
                    "AND (recovered_at IS NULL OR recovered_at = '') "
                    "ORDER BY occurred_at DESC, id DESC LIMIT 1",
                    (sensor_id,),
                ).fetchone()
                backfill_event_id = int(row["id"]) if row else None
            if backfill_event_id is not None:
                tx.execute(
                    "UPDATE sensor_fault_events SET recovered_at = ? "
                    "WHERE id = ? AND (recovered_at IS NULL OR recovered_at = '')",
                    (ts, int(backfill_event_id)),
                )
            tx.execute(
                """
                INSERT INTO sensor_fault_events
                  (sensor_id, status, fault_type, detail, occurred_at, recovered_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sensor_id, "online", fault_type, "recovered", ts, None),
            )

    def list_sensor_fault_events(self, sensor_id: str, limit: int = 100) -> list[dict]:
        """查询传感器故障历史（按 occurred_at 递减）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sensor_fault_events WHERE sensor_id = ? "
                "ORDER BY occurred_at DESC, id DESC LIMIT ?",
                (sensor_id, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_sensor_fault_event_recovered(
        self, event_id: int, recovered_at: str | None = None,
    ) -> bool:
        """给一条未恢复的故障历史回填恢复时间。返回是否实际更新。"""
        ts = recovered_at or self._now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sensor_fault_events SET recovered_at = ? "
                "WHERE id = ? AND (recovered_at IS NULL OR recovered_at = '')",
                (ts, int(event_id)),
            )
            return cur.rowcount > 0



# ====================================================================
# Standalone test
# ====================================================================

if __name__ == "__main__":

    print(
        "Testing Database...\n"
    )

    db = Database(
        db_path="data/drrm.db"
    )

    # ---------------------------------------------------------------
    # 1. save_event
    # ---------------------------------------------------------------

    print(
        "1. save_event:"
    )

    event_id = db.save_event(
        type="test_event",
        severity="LOW",
        person_id=1,
        payload={
            "source":
                "database_test",
        },
    )

    print(
        {
            "event_id":
                event_id
        }
    )

    # ---------------------------------------------------------------
    # 2. get_recent_events
    # ---------------------------------------------------------------

    print(
        "\n2. get_recent_events:"
    )

    events = db.get_recent_events(
        hours=1
    )

    print(
        {
            "count":
                len(events),
            "events":
                events[:3],
        }
    )

    # ---------------------------------------------------------------
    # 3. upsert_person
    # ---------------------------------------------------------------

    print(
        "\n3. upsert_person:"
    )

    db.upsert_person(
        person_id=999,
        state="NORMAL",
        risk=0.1,
    )

    print(
        db.get_person(
            999
        )
    )

    # ---------------------------------------------------------------
    # 4. save_task
    # ---------------------------------------------------------------

    print(
        "\n4. save_task:"
    )

    test_task_id = (
        "DB-TEST-UPSERT-001"
    )

    task_id = db.save_task(
        {
            "task_id":
                test_task_id,

            "trigger_event":
                "TEST",

            "plan_trace":
                [],

            "status":
                "running",

            "created":
                db._now(),

            "finished":
                None,
        }
    )

    print(
        {
            "task_id":
                task_id,
            "status":
                "running",
        }
    )

    # ---------------------------------------------------------------
    # 5. Update the same task
    # ---------------------------------------------------------------

    print(
        "\n5. update existing task:"
    )

    db.save_task(
        {
            "task_id":
                test_task_id,

            "trigger_event":
                "TEST",

            "plan_trace":
                [
                    {
                        "step":
                            1,

                        "action":
                            "test",

                        "observation":
                            {
                                "ok":
                                    True
                            },
                    }
                ],

            "status":
                "completed",

            "finished":
                db._now(),
        }
    )

    updated_task = db.get_task(
        test_task_id
    )

    print(
        updated_task
    )

    # ---------------------------------------------------------------
    # 6. save_agent_log
    # ---------------------------------------------------------------

    print(
        "\n6. save_agent_log:"
    )

    log_id = db.save_agent_log(
        task_id=test_task_id,
        tool_name="vision_tool",
        input={
            "action":
                "get_snapshot",
        },
        output={
            "ok":
                True,
        },
        latency_ms=5,
    )

    print(
        {
            "log_id":
                log_id
        }
    )

    # ---------------------------------------------------------------
    # Final
    # ---------------------------------------------------------------

    print(
        "\nDatabase test completed successfully."
    )