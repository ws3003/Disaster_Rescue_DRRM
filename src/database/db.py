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
from datetime import datetime
from pathlib import Path
from typing import Any


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

        self._initialize_database()

    # ================================================================
    # Connection
    # ================================================================

    def _connect(self) -> sqlite3.Connection:
        """
        Create a SQLite connection.

        Row factory is enabled so database rows can be converted
        directly into dictionaries.
        """

        conn = sqlite3.connect(
            self.db_path
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

        event_id = self._event_id()

        payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
        )

        ts = self._now()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (
                    event_id,
                    ts,
                    type,
                    severity,
                    person_id,
                    payload
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    ts,
                    type,
                    severity,
                    person_id,
                    payload_json,
                ),
            )

        return event_id

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
        latency_ms: int,
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

        with self._connect() as conn:

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
                    int(latency_ms),
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

        reading_id = self._reading_id()

        now_iso = self._now_iso()

        captured = (
            device_captured_at
            or now_iso
        )

        edge_ts = (
            edge_received_at
            or now_iso
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO readings (
                    reading_id,
                    sensor_id,
                    temperature,
                    humidity,
                    device_captured_at,
                    edge_received_at,
                    received_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    reading_id,
                    sensor_id,
                    float(temperature),
                    (
                        None
                        if humidity is None
                        else float(humidity)
                    ),
                    captured,
                    edge_ts,
                ),
            )

        return reading_id

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

        alert_id = self._sensor_alert_id()

        now_iso = self._now_iso()

        captured = (
            device_captured_at
            or now_iso
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sensor_alerts (
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
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, NULL, ?, NULL, ?)
                """,
                (
                    alert_id,
                    type,
                    severity,
                    (
                        None
                        if temperature is None
                        else float(temperature)
                    ),
                    (
                        None
                        if humidity is None
                        else float(humidity)
                    ),
                    (
                        None
                        if threshold is None
                        else float(threshold)
                    ),
                    now_iso,
                    captured,
                    reading_id,
                ),
            )

        return alert_id

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
                ORDER BY occurred_at DESC
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