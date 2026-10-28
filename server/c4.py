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
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from src.database.db import Database

router = APIRouter(prefix="/api/v1")

_CODES = {
    "INVALID_PARAMETER": "参数非法",
    "UNAUTHORIZED": "认证缺失或无效",
    "INTERNAL_ERROR": "服务端内部错误",
}

_C4_RECORD_TYPES = frozenset({"person_event", "reading", "sensor_alert"})


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


def _ingest(db: Database, rec: Dict[str, Any]) -> str:
    """单条幂等接收: 无登记→synced; 同hash→already_synced; 异hash→content_mismatch(记冲突审计, 不覆盖)。"""
    _validate_record(rec)
    rt = rec.get("record_type")
    sid = rec.get("source_record_id")
    ch = rec.get("content_hash")
    edge = rec.get("edge_id")
    now = int(__import__("time").time() * 1000)
    with db.with_transaction() as tx:
        row = tx.execute(
            "SELECT content_hash FROM c4_idem WHERE record_type=? AND source_record_id=?",
            (rt, sid),
        ).fetchone()
        if row is None:
            tx.execute(
                "INSERT INTO c4_idem (record_type, source_record_id, content_hash, received_at, edge_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (rt, sid, ch, now, edge),
            )
            return "synced"
        if row["content_hash"] == ch:
            return "already_synced"
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