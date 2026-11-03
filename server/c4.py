"""server/c4.py — C4 服务端接收接口（POST /api/v1/c4/records）。

Pi 侧 Outbox worker 将待同步记录投递至此; 服务端做幂等接收(c4_idem)与同键内容核验。
- 认证: 环境变量 C4_API_TOKEN(Bearer); 缺 token/错误 token → 401; 未配置 → 500(配置错误), 不静默放行。
- 返回沿用四字段信封 {code, message, data, meta}; data 为逐条 (record_type, source_record_id, status, error_code?)。
- 逐条 status: synced / already_synced / content_mismatch。
- 复用现有数据库与 C4 表(c4_idem / c4_content_conflict), 不改 schema; 单一事务内登记与核验。
- 本仓库为单库模型("服务端读取的只读副本 == 权威库"); 真实双主机部署需在接收侧另存只读副本(超出本仓库范围, 记录为限制)。
"""

from __future__ import annotations

import hmac
import json as _json
import math
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from src.database import canonical as _canonical
from src.database.db import Database
from src.sync import bridge as _bridge

router = APIRouter(prefix="/api/v1")

_CODES = {
    "INVALID_PARAMETER": "参数非法",
    "UNAUTHORIZED": "认证缺失或无效",
    "INTERNAL_ERROR": "服务端内部错误",
}

_C4_RECORD_TYPES = frozenset({"person_event", "reading", "sensor_alert"})

# 需携带业务本体的记录类型（writing 业务只读副本到服务端库 readings/sensor_alerts）
_BUSINESS_CARRIERS = frozenset({"reading", "sensor_alert", "person_event"})

# 各类型的必填业务字段（缺失 → 整批 400，不产生半写入）
_BUSINESS_REQUIRED = {
    "reading": frozenset({"sensor_id", "temperature", "device_captured_at"}),
    "sensor_alert": frozenset({"type", "occurred_at"}),
    "person_event": frozenset({"type", "ts"}),
}


def get_db() -> Database:
    return Database(db_path=os.environ.get("DRRM_DB", "data/drrm.db"))


def _require_c4_token(authorization: Optional[str]) -> None:
    token = (os.environ.get("C4_API_TOKEN") or "").strip()
    if not token:
        raise HTTPException(status_code=500, detail="service misconfigured: C4_API_TOKEN unset")
    expected = f"Bearer {token}".encode("utf-8")
    provided = (authorization or "").encode("utf-8")
    if not authorization or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def _ok_json(data: Any, meta: Optional[Dict[str, Any]] = None) -> JSONResponse:
    return JSONResponse(content={"code": "OK", "message": "ok",
                                 "data": data, "meta": meta if meta is not None else {}})


def _fail_json(code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse(status_code=http_status,
                        content={"code": code, "message": message, "data": None, "meta": {}})


def _is_finite_number(value: Any) -> bool:
    """数值必须为 int/float/Decimal/十进制字符串、非 bool、有限（不允许 NaN/±Inf）。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, str):
        try:
            import decimal as _dec
            d = _dec.Decimal(value.strip())
            d_float = float(d)
        except Exception:  # noqa: BLE001
            return False
        return math.isfinite(d_float)
    return False


def _validate_business(rt: str, business: Any) -> None:
    """业务本体校验：
    - person_event 不要求 business（延续旧协议）。
    - reading/sensor_alert：business 缺失视为"旧客户端"（只登记 c4_idem，不写业务副本，向后兼容）；
      若提供则必须为 dict 且含必填字段、数值合法（否则 400，不产生写入）。
    """
    if rt not in _BUSINESS_CARRIERS:
        return
    if business is None:
        return  # 旧客户端：无业务本体 → 仅幂等登记，兼容
    if not isinstance(business, dict):
        raise HTTPException(status_code=400, detail=f"{rt} business 必须为对象")
    missing = [k for k in _BUSINESS_REQUIRED[rt] if k not in business]
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"{rt} business 缺少字段: {sorted(missing)}")
    # 数值/时间合法性
    num_fields = {"temperature"} | ({"humidity", "threshold"} if rt == "sensor_alert" else set())
    for k in num_fields:
        v = business.get(k)
        if v is None:
            continue
        if not _is_finite_number(v):
            raise HTTPException(status_code=400, detail=f"{rt}.business.{k} 非法数值")
    for k in ("device_captured_at", "occurred_at"):
        v = business.get(k)
        if isinstance(v, str) and not v.strip():
            raise HTTPException(status_code=400, detail=f"{rt}.business.{k} 为空")


def _validate_record(rec: Dict[str, Any]) -> None:
    """单条记录字段级校验; 非法抛 HTTP 400, 不产生任何写入。

    整批接收在 ingest 前先对全部记录执行本校验, 使非法请求整体被拒绝,
    不留下"已同步若干条 + 失败若干条"的半写入成功路径。
    """
    rt = rec.get("record_type")
    sid = rec.get("source_record_id")
    ch = rec.get("content_hash")
    if rt not in _C4_RECORD_TYPES or not isinstance(sid, str) or not sid or not isinstance(ch, str) or not ch:
        raise HTTPException(status_code=400, detail="invalid record fields")
    _validate_business(rt, rec.get("business"))


def _hash_of_business(rt: str, business: Dict[str, Any]):
    """用与 Pi 相同的 canonical 规则，从业务本体重算 hash（校验与 content_hash 一致）。"""
    if not isinstance(business, dict):
        return None, {"invalid"}
    view = _bridge._canonical_view(rt, business)
    return _canonical.classify(rt, view, version=_bridge.SYNC_NORM_VERSION)


def _write_business_copy(tx, rt: str, sid: str, business: Dict[str, Any], recv_iso: str) -> None:
    """在调用方事务内写 Windows 只读业务副本（readings / sensor_alerts）。

    同一事务与 c4_idem 登记一起提交；失败则整体回滚（不出现"已登记但无副本/有副本未登记"）。
    """
    if rt == "reading":
        tx.execute(
            """
            INSERT INTO readings
              (reading_id, sensor_id, temperature, humidity, device_captured_at,
               edge_received_at, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, business.get("sensor_id"), business.get("temperature"),
             business.get("humidity"), business.get("device_captured_at"),
             business.get("edge_received_at"), recv_iso),
        )
    elif rt == "sensor_alert":
        tx.execute(
            """
            INSERT INTO sensor_alerts
              (alert_id, type, severity, temperature, humidity, threshold, status,
               occurred_at, resolved_at, device_captured_at, received_at, reading_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, business.get("type"), business.get("severity"),
             business.get("temperature"), business.get("humidity"), business.get("threshold"),
             business.get("status", "active"), business.get("occurred_at"),
             business.get("resolved_at"), business.get("device_captured_at"),
             recv_iso, business.get("reading_id")),
        )
    elif rt == "person_event":
        payload = business.get("payload")
        payload_json = _json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None
        tx.execute(
            """
            INSERT INTO events
              (event_id, ts, type, severity, person_id, payload)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sid, business.get("ts"), business.get("type"), business.get("severity"),
             business.get("person_id"), payload_json),
        )


def _ingest(db: Database, rec: Dict[str, Any]) -> str:
    """单条幂等接收+业务副本落地。

    - 无登记 → (业务合法且 hash 复核一致) 同一事务登记 c4_idem 并写业务副本 → synced；
      业务缺失(旧客户端/person_event) → 仅登记 → synced（兼容，不写副本）。
    - 同hash → already_synced（不重复入库）。
    - 异hash / hash 复核不一致 → content_mismatch（冲突审计，不覆盖首收，不写副本）。
    """
    _validate_record(rec)
    rt = rec.get("record_type")
    sid = rec.get("source_record_id")
    ch = rec.get("content_hash")
    edge = rec.get("edge_id")
    business = rec.get("business")
    now = int(__import__("time").time() * 1000)
    recv_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    with db.with_transaction() as tx:
        row = tx.execute(
            "SELECT content_hash FROM c4_idem WHERE record_type=? AND source_record_id=?",
            (rt, sid),
        ).fetchone()
        if row is None:
            # 首收：若携带业务本体，则复核 hash；不一致 → 记冲突、不写副本
            if isinstance(business, dict):
                _h, _why = _hash_of_business(rt, business)
                if not _h or _h != ch:
                    tx.execute(
                        """
                        INSERT INTO c4_content_conflict
                          (record_type, source_record_id, content_hash, conflict_hash,
                           detected_at, edge_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (rt, sid, ch, _h or "", now, edge),
                    )
                    return "content_mismatch"
            tx.execute(
                "INSERT INTO c4_idem (record_type, source_record_id, content_hash, received_at, edge_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (rt, sid, ch, now, edge),
            )
            # 同事务写 Windows 只读业务副本（合法业务本体；person_event 不写）
            if rt in _BUSINESS_CARRIERS and isinstance(business, dict):
                _write_business_copy(tx, rt, sid, business, recv_iso)
            return "synced"
        if row["content_hash"] == ch:
            return "already_synced"
        # sensor_alert 生命周期：active -> resolved 等死亡量使 hash 变化，应更新既有业务副本
        # （单调推进，不回退；幂等 hash 同步前移以保证重复发送转 already_synced）。
        if rt == "sensor_alert" and isinstance(business, dict):
            _h, _why = _hash_of_business(rt, business)
            if not _h or _h != ch:
                tx.execute(
                    """
                    INSERT INTO c4_content_conflict
                        (record_type, source_record_id, content_hash, conflict_hash, detected_at, edge_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (rt, sid, row["content_hash"], ch, now, edge),
                )
                return "content_mismatch"
            cur = tx.execute(
                "SELECT status FROM sensor_alerts WHERE alert_id = ?", (sid,)
            ).fetchone()
            new_status = str(business.get("status") or "active").lower()
            # 单调守卫：已 resolved 的副本不允许被旧 active 回退。
            if cur is not None and cur["status"] == "resolved" and new_status != "resolved":
                tx.execute(
                    """
                    INSERT INTO c4_content_conflict
                        (record_type, source_record_id, content_hash, conflict_hash, detected_at, edge_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (rt, sid, row["content_hash"], ch, now, edge),
                )
                return "content_mismatch"
            tx.execute(
                """
                UPDATE sensor_alerts
                SET status = ?, resolved_at = ?
                WHERE alert_id = ?
                """,
                (new_status, business.get("resolved_at"), sid),
            )
            tx.execute(
                "UPDATE c4_idem SET content_hash = ? WHERE record_type = ? AND source_record_id = ?",
                (ch, rt, sid),
            )
            return "synced"
        tx.execute(
            """
            INSERT INTO c4_content_conflict
                (record_type, source_record_id, content_hash, conflict_hash, detected_at, edge_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (rt, sid, row["content_hash"], ch, now, edge),
        )
        return "content_mismatch"


@router.post("/c4/records")
def receive(
    payload: Dict[str, Any] = Body(...),
    authorization: Optional[str] = Header(default=None),
    db: Database = Depends(get_db),
) -> JSONResponse:
    try:
        _require_c4_token(authorization)
    except HTTPException as exc:
        code = "UNAUTHORIZED" if exc.status_code == 401 else "INTERNAL_ERROR"
        return _fail_json(code, exc.detail, exc.status_code)

    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list) or not records:
        return _fail_json("INVALID_PARAMETER", "records must be non-empty list", 400)

    # 整批预校验: 任一条非法即整体 400, 不产生任何半写入 (不留下"部分已同步"路径)
    try:
        for rec in records:
            if not isinstance(rec, dict):
                raise HTTPException(status_code=400, detail="invalid record fields")
            _validate_record(rec)
    except HTTPException as exc:
        return _fail_json("INVALID_PARAMETER", exc.detail, 400)

    per: List[Dict[str, Any]] = []
    for rec in records:
        try:
            status = _ingest(db, rec)
            per.append({
                "record_type": rec.get("record_type"),
                "source_record_id": rec.get("source_record_id"),
                "status": status,
                "error_code": None,
            })
        except HTTPException as exc:  # noqa: BLE001
            return _fail_json("INVALID_PARAMETER", exc.detail, 400)
        except Exception:  # noqa: BLE001
            return _fail_json("INTERNAL_ERROR", "ingest failed", 500)

    # 诚实统计: content_mismatch 属可辨认冲突结果, 不计入"成功同步"
    succeeded = sum(1 for p in per if p["status"] in ("synced", "already_synced"))
    conflicted = sum(1 for p in per if p["status"] == "content_mismatch")
    return _ok_json(per, {"total": len(per), "succeeded": succeeded,
                          "conflicted": conflicted, "failed": len(per) - succeeded})